#!/usr/bin/env python3
"""SWE-bench Lite benchmark runner for Squad.

Loads the SWE-bench Lite dataset, groups tasks by repo, initializes Squad
once per repo, then runs each task via the Copilot CLI and captures patches.

Usage:
    python squad_swebench_runner.py [--config config.yaml]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from datasets import load_dataset
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

console = Console(stderr=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)
log = logging.getLogger("squad_swebench")


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    """Runner configuration loaded from YAML."""

    model_name: str = "squad-v1"
    llm_model: str = "gpt-4o"
    dataset: str = "princeton-nlp/SWE-bench_Lite"
    split: str = "test"
    timeout_seconds: int = 600
    max_workers: int = 4
    max_retries: int = 1
    budget_cap: int = 0
    repos_cache_dir: str = ".cache/repos"
    worktrees_dir: str = ".cache/worktrees"
    output_dir: str = "output"
    predictions_file: str = "output/predictions.json"
    resume: bool = True
    eval_backend: str = "modal"
    eval_max_workers: int = 8
    run_id: str = "squad_lite_run_001"

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load config from a YAML file, falling back to defaults for missing keys."""
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskResult:
    """Result of a single SWE-bench task execution."""

    instance_id: str
    patch: str = ""
    status: str = "pending"  # pending | success | timeout | error
    elapsed_seconds: float = 0.0
    error_message: str = ""


@dataclass
class RunStats:
    """Aggregate statistics for the benchmark run."""

    total: int = 0
    completed: int = 0
    success: int = 0
    timeout: int = 0
    error: int = 0
    skipped: int = 0

    def summary(self) -> str:
        return (
            f"Total: {self.total} | Completed: {self.completed} | "
            f"Success: {self.success} | Timeout: {self.timeout} | "
            f"Error: {self.error} | Skipped: {self.skipped}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────────────────────────────────────


def _run_git(
    args: list[str], cwd: Path, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Run a git command, raising on failure."""
    cmd = ["git", "--no-pager"] + args
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def clone_repo(repo: str, cache_dir: Path) -> Path:
    """Clone a GitHub repo into cache_dir if not already present.

    Args:
        repo: GitHub repo in 'owner/name' format (e.g. 'django/django').
        cache_dir: Parent directory for cloned repos.

    Returns:
        Path to the cloned repo directory.
    """
    # Use short directory names to avoid Windows path length issues
    repo_short = repo.split("/")[-1]
    repo_dir = cache_dir / repo_short
    if repo_dir.exists() and (repo_dir / ".git").exists():
        log.debug(f"Repo already cached: {repo}")
        return repo_dir

    repo_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    log.info(f"Cloning {url} -> {repo_dir}")
    subprocess.run(
        ["git", "clone", "--quiet", url, str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=1200,  # 20 minutes for large repos like django/astropy
    )
    return repo_dir


def create_worktree(repo_dir: Path, worktrees_dir: Path, worker_id: int) -> Path:
    """Create a git worktree for a given worker.

    Args:
        repo_dir: Path to the main repo clone.
        worktrees_dir: Parent directory for worktrees.
        worker_id: Unique worker identifier.

    Returns:
        Path to the worktree directory.
    """
    wt_dir = worktrees_dir / f"worker-{worker_id}"

    # Clean up any stale state
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )

    if wt_dir.exists():
        # Remove existing worktree properly
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)

    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    branch_name = f"swebench-worker-{worker_id}"
    # Remove stale branch if exists
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )

    _run_git(["worktree", "add", "-b", branch_name, str(wt_dir), "HEAD"], cwd=repo_dir)
    return wt_dir


def reset_to_commit(worktree: Path, commit: str) -> None:
    """Hard-reset a worktree to a specific commit."""
    _run_git(["checkout", "--force", commit], cwd=worktree)
    _run_git(["clean", "-fdx"], cwd=worktree)


def get_patch(worktree: Path) -> str:
    """Capture the diff between HEAD and the working tree + index.

    Excludes Squad scaffold files (.squad/, .github/agents/) from the diff
    so only the actual code fix is captured.
    """
    result = _run_git(
        ["diff", "HEAD", "--", ".", ":(exclude).squad", ":(exclude).github/agents"],
        cwd=worktree, timeout=30,
    )
    return result.stdout.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Squad invocation
# ─────────────────────────────────────────────────────────────────────────────


# Source repo for Squad agent files
SQUAD_AGENT_SOURCE = Path(__file__).resolve().parent.parent.parent / ".github" / "agents"

# Agent files needed for Squad to work in target repos
SQUAD_AGENT_FILES = ["squad.agent.md", "data.agent.md", "picard.agent.md"]

# Pre-built .squad/ scaffold with team config so Squad routes to subagents
SQUAD_SCAFFOLD_SOURCE = Path(__file__).resolve().parent / "squad-scaffold" / ".squad"


def inject_squad_agents(worktree: Path) -> None:
    """Copy Squad agent definitions and team scaffold into a target worktree.

    This allows `copilot --agent squad` to work in repos that don't
    have Squad installed. Injects both:
    - .github/agents/*.agent.md (agent definitions)
    - .squad/ (team config so Squad spawns subagents)
    """
    # Agent definition files
    target_agents = worktree / ".github" / "agents"
    target_agents.mkdir(parents=True, exist_ok=True)
    for filename in SQUAD_AGENT_FILES:
        src = SQUAD_AGENT_SOURCE / filename
        dst = target_agents / filename
        if src.exists():
            shutil.copy2(src, dst)

    # Squad team scaffold (team.md, routing.md, charters, etc.)
    target_squad = worktree / ".squad"
    if SQUAD_SCAFFOLD_SOURCE.exists():
        _copy_tree(SQUAD_SCAFFOLD_SOURCE, target_squad)


def _copy_tree(src: Path, dst: Path) -> None:
    """Recursively copy a directory tree, overwriting existing files."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def init_squad(worktree: Path, timeout: int = 300) -> bool:
    """Initialize Squad agent files in the worktree.

    Copies the Squad agent definitions (.github/agents/*.agent.md) from
    the benchmark repo into the target repo worktree so that
    `copilot --agent squad` works.

    Returns:
        True if agent files were injected successfully.
    """
    try:
        inject_squad_agents(worktree)
        log.info(f"Squad agents injected into {worktree}")
        return True
    except Exception as e:
        log.error(f"Failed to inject Squad agents: {e}")
        return False


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children on Windows."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        # Fallback: just kill the main process
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def run_squad_task(
    worktree: Path,
    problem_statement: str,
    timeout_seconds: int,
) -> tuple[str, str]:
    """Invoke Squad (multi-agent) on a single SWE-bench task.

    Uses `copilot --agent squad` in autopilot mode. The Squad coordinator
    routes work to specialist agents (Data, Picard, etc.) who collaborate
    to fix the issue.

    Args:
        worktree: Path to the git worktree.
        problem_statement: The GitHub issue text.
        timeout_seconds: Max time allowed for the task.

    Returns:
        Tuple of (patch_content, status) where status is
        'success', 'timeout', or 'error'.
    """
    # Ensure agent files are present (idempotent)
    inject_squad_agents(worktree)

    prompt = (
        f"Fix this GitHub issue:\n\n{problem_statement}\n\n"
        "ralph, go"
    )

    # Write output to temp log files to avoid encoding issues on Windows
    log_dir = Path(worktree).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = log_dir / f"{worktree.name}.stdout.log"
    stderr_path = log_dir / f"{worktree.name}.stderr.log"

    try:
        fout = open(stdout_path, "w", encoding="utf-8", errors="replace")
        ferr = open(stderr_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            [
                "copilot",
                "--agent", "squad",
                "--yolo",
                "--autopilot",
                "--max-autopilot-continues", "50",
                "-p", prompt,
            ],
            cwd=worktree,
            stdout=fout,
            stderr=ferr,
        )
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            log.warning(f"Task timed out after {timeout_seconds}s — killing process tree")
            _kill_process_tree(proc.pid)
            proc.wait(timeout=30)
            fout.close()
            ferr.close()
            try:
                patch = get_patch(worktree)
                return patch, "timeout"
            except Exception:
                return "", "timeout"

        fout.close()
        ferr.close()

        if proc.returncode != 0:
            log.warning(f"Squad returned non-zero: {proc.returncode}")
            # Still try to capture patch — partial work may exist
    except Exception as e:
        log.error(f"Subprocess error: {e}")
        try:
            fout.close()
        except Exception:
            pass
        try:
            ferr.close()
        except Exception:
            pass

    # Capture patch
    try:
        patch = get_patch(worktree)
        status = "success" if patch else "error"
        return patch, status
    except Exception as e:
        log.error(f"Failed to capture patch: {e}")
        return "", "error"


# ─────────────────────────────────────────────────────────────────────────────
# Task execution (worker entry point)
# ─────────────────────────────────────────────────────────────────────────────


def execute_task(
    instance_id: str,
    repo: str,
    base_commit: str,
    problem_statement: str,
    repos_cache_dir: str,
    worktrees_dir: str,
    worker_id: int,
    timeout_seconds: int,
    max_retries: int,
) -> TaskResult:
    """Execute a single SWE-bench task in a dedicated worktree.

    This function is designed to be called in a subprocess worker.

    Args:
        instance_id: SWE-bench instance identifier.
        repo: GitHub repo (e.g. 'django/django').
        base_commit: Git commit SHA to reset to.
        problem_statement: The issue description.
        repos_cache_dir: Path to cached repo clones.
        worktrees_dir: Path for worktree creation.
        worker_id: Worker index for worktree isolation.
        timeout_seconds: Max seconds per task.
        max_retries: Number of retry attempts.

    Returns:
        TaskResult with patch and status.
    """
    start_time = time.monotonic()
    cache_dir = Path(repos_cache_dir)
    wt_base = Path(worktrees_dir)

    try:
        # Ensure repo is cloned
        repo_dir = clone_repo(repo, cache_dir)

        # Create isolated worktree
        repo_short = repo.split("/")[-1]
        worktree = create_worktree(repo_dir, wt_base / repo_short, worker_id)

        # Reset to base commit
        reset_to_commit(worktree, base_commit)

        # Run Squad with retries
        patch = ""
        status = "error"
        attempts = 1 + max_retries

        for attempt in range(attempts):
            if attempt > 0:
                log.info(f"Retry {attempt}/{max_retries} for {instance_id}")
                reset_to_commit(worktree, base_commit)

            patch, status = run_squad_task(worktree, problem_statement, timeout_seconds)

            if patch or status == "timeout":
                break

        elapsed = time.monotonic() - start_time
        return TaskResult(
            instance_id=instance_id,
            patch=patch,
            status=status,
            elapsed_seconds=elapsed,
        )

    except Exception as e:
        elapsed = time.monotonic() - start_time
        log.error(f"Task {instance_id} failed: {e}")
        return TaskResult(
            instance_id=instance_id,
            patch="",
            status="error",
            elapsed_seconds=elapsed,
            error_message=str(e),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Predictions I/O
# ─────────────────────────────────────────────────────────────────────────────


def load_predictions(path: Path) -> dict[str, dict[str, Any]]:
    """Load existing predictions file for resume support.

    Returns:
        Dict mapping instance_id → prediction entry.
    """
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    return {entry["instance_id"]: entry for entry in data}


def save_predictions(predictions: dict[str, dict[str, Any]], path: Path) -> None:
    """Write predictions to disk in SWE-bench format.

    Args:
        predictions: Dict mapping instance_id → prediction entry.
        path: Output file path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = list(predictions.values())
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)
    log.info(f"Saved {len(entries)} predictions to {path}")


def result_to_prediction(result: TaskResult, model_name: str) -> dict[str, Any]:
    """Convert a TaskResult to a SWE-bench prediction entry."""
    return {
        "instance_id": result.instance_id,
        "model_name_or_path": model_name,
        "model_patch": result.patch,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────


def load_and_group_tasks(
    dataset_name: str, split: str
) -> dict[str, list[dict[str, Any]]]:
    """Load SWE-bench dataset and group tasks by repository.

    Args:
        dataset_name: HuggingFace dataset identifier.
        split: Dataset split to load.

    Returns:
        Dict mapping repo name → list of task instances.
    """
    log.info(f"Loading dataset: {dataset_name} (split={split})")
    ds = load_dataset(dataset_name, split=split)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in ds:
        grouped[item["repo"]].append(dict(item))
    log.info(f"Loaded {len(ds)} tasks across {len(grouped)} repos")
    return grouped


def run_benchmark(config: Config, base_dir: Path) -> RunStats:
    """Execute the full SWE-bench Lite benchmark.

    Args:
        config: Runner configuration.
        base_dir: Base directory for resolving relative paths.

    Returns:
        RunStats with final counts.
    """
    stats = RunStats()

    # Resolve paths
    repos_cache = base_dir / config.repos_cache_dir
    worktrees = base_dir / config.worktrees_dir
    predictions_path = base_dir / config.predictions_file
    output_dir = base_dir / config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load existing predictions for resume
    predictions: dict[str, dict[str, Any]] = {}
    if config.resume:
        predictions = load_predictions(predictions_path)
        if predictions:
            log.info(f"Resuming: {len(predictions)} tasks already completed")

    # Load dataset
    grouped_tasks = load_and_group_tasks(config.dataset, config.split)

    # Flatten all tasks, respecting budget cap
    all_tasks: list[dict[str, Any]] = []
    for repo, tasks in sorted(grouped_tasks.items()):
        all_tasks.extend(tasks)
    stats.total = len(all_tasks)

    # Filter out already-completed
    pending_tasks = [
        t for t in all_tasks if t["instance_id"] not in predictions
    ]
    stats.skipped = stats.total - len(pending_tasks)

    if config.budget_cap > 0:
        pending_tasks = pending_tasks[: config.budget_cap]
        log.info(f"Budget cap: processing {len(pending_tasks)} tasks")

    if not pending_tasks:
        log.info("All tasks already completed. Nothing to do.")
        return stats

    log.info(
        f"Running {len(pending_tasks)} tasks "
        f"({stats.skipped} skipped, {config.max_workers} workers)"
    )

    # Per-repo initialization (sequential)
    repos_to_init = {t["repo"] for t in pending_tasks}
    for repo in sorted(repos_to_init):
        repo_dir = clone_repo(repo, repos_cache)
        repo_short = repo.split("/")[-1]
        init_wt = create_worktree(repo_dir, worktrees / repo_short, 99)
        init_squad(init_wt, timeout=300)

    # Execute tasks in parallel
    progress = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    with progress:
        task_progress = progress.add_task(
            "SWE-bench", total=len(pending_tasks)
        )

        with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
            futures = {}
            for i, task_item in enumerate(pending_tasks):
                worker_id = i % config.max_workers
                future = executor.submit(
                    execute_task,
                    instance_id=task_item["instance_id"],
                    repo=task_item["repo"],
                    base_commit=task_item["base_commit"],
                    problem_statement=task_item["problem_statement"],
                    repos_cache_dir=str(repos_cache),
                    worktrees_dir=str(worktrees),
                    worker_id=worker_id,
                    timeout_seconds=config.timeout_seconds,
                    max_retries=config.max_retries,
                )
                futures[future] = task_item["instance_id"]

            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    log.error(f"Worker crashed for {instance_id}: {e}")
                    result = TaskResult(
                        instance_id=instance_id,
                        status="error",
                        error_message=str(e),
                    )

                # Update stats
                stats.completed += 1
                if result.status == "success":
                    stats.success += 1
                elif result.status == "timeout":
                    stats.timeout += 1
                else:
                    stats.error += 1

                # Store prediction
                predictions[result.instance_id] = result_to_prediction(
                    result, config.model_name
                )

                # Persist after each task (crash-safe resume)
                save_predictions(predictions, predictions_path)

                # Progress
                progress.update(task_progress, advance=1)
                log.info(
                    f"[{stats.completed}/{len(pending_tasks)}] "
                    f"{instance_id}: {result.status} "
                    f"({result.elapsed_seconds:.1f}s, "
                    f"patch={len(result.patch)} chars)"
                )

    # Final save
    save_predictions(predictions, predictions_path)

    # Write run metadata
    meta_path = output_dir / "run_metadata.json"
    with open(meta_path, "w") as f:
        json.dump(
            {
                "run_id": config.run_id,
                "model_name": config.model_name,
                "llm_model": config.llm_model,
                "dataset": config.dataset,
                "split": config.split,
                "total_tasks": stats.total,
                "completed": stats.completed,
                "success": stats.success,
                "timeout": stats.timeout,
                "error": stats.error,
                "skipped": stats.skipped,
                "max_workers": config.max_workers,
                "timeout_seconds": config.timeout_seconds,
            },
            f,
            indent=2,
        )

    log.info(f"Run complete. {stats.summary()}")
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """Entry point for the SWE-bench benchmark runner."""
    parser = argparse.ArgumentParser(
        description="Run SWE-bench Lite benchmark with Squad",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python squad_swebench_runner.py\n"
            "  python squad_swebench_runner.py --config my_config.yaml\n"
            "  python squad_swebench_runner.py --workers 8 --timeout 900\n"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Override max_workers from config",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override timeout_seconds from config",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="Override budget_cap from config (0 = no limit)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start fresh, ignoring existing predictions",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    config_path = args.config.resolve()
    if config_path.exists():
        config = Config.from_yaml(config_path)
        log.info(f"Loaded config from {config_path}")
    else:
        config = Config()
        log.warning(f"Config not found at {config_path}, using defaults")

    # Apply CLI overrides
    if args.workers is not None:
        config.max_workers = args.workers
    if args.timeout is not None:
        config.timeout_seconds = args.timeout
    if args.budget is not None:
        config.budget_cap = args.budget
    if args.no_resume:
        config.resume = False

    base_dir = config_path.parent
    stats = run_benchmark(config, base_dir)

    # Non-zero exit if all tasks errored
    if stats.completed > 0 and stats.success == 0 and stats.error == stats.completed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
