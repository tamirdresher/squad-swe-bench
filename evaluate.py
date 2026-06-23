#!/usr/bin/env python3
"""Evaluate Squad predictions using the SWE-bench harness.

Wraps `swebench.harness.run_evaluation` with config-driven defaults and
supports both local Docker and Modal cloud backends.

Usage:
    python evaluate.py
    python evaluate.py --predictions output/predictions.json --backend modal
    python evaluate.py --instance-ids django__django-11999 sympy__sympy-20590
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("squad_swebench_eval")


def load_config(config_path: Path) -> dict:
    """Load runner config for evaluation defaults."""
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def run_evaluation(
    predictions_path: Path,
    *,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    backend: str = "docker",
    max_workers: int = 8,
    run_id: str = "squad_eval",
    instance_ids: list[str] | None = None,
    parallelism: int = 10,
) -> int:
    """Run the SWE-bench evaluation harness.

    Args:
        predictions_path: Path to predictions.json.
        dataset: HuggingFace dataset name.
        backend: 'docker' for local or 'modal' for cloud.
        max_workers: Concurrent Docker containers (local only).
        run_id: Identifier for this evaluation run.
        instance_ids: Optional subset of instances to evaluate.
        parallelism: Modal parallelism (modal only).

    Returns:
        Exit code (0 = success).
    """
    if not predictions_path.exists():
        log.error(f"Predictions file not found: {predictions_path}")
        return 1

    # Validate predictions format
    with open(predictions_path) as f:
        predictions = json.load(f)

    if not isinstance(predictions, list):
        log.error("predictions.json must be a JSON array")
        return 1

    non_empty = sum(1 for p in predictions if p.get("model_patch"))
    log.info(
        f"Evaluating {len(predictions)} predictions "
        f"({non_empty} non-empty patches)"
    )

    # Build command
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset,
        "--predictions_path", str(predictions_path),
        "--run_id", run_id,
    ]

    if backend == "modal":
        cmd.extend(["--modal", "true", "--parallelism", str(parallelism)])
    else:
        cmd.extend(["--max_workers", str(max_workers)])

    if instance_ids:
        cmd.extend(["--instance_ids"] + instance_ids)

    log.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def print_results(run_id: str) -> None:
    """Print evaluation results summary if available."""
    results_dir = Path("evaluation_results")
    if not results_dir.exists():
        # Try the logs path
        results_dir = Path(f"logs/run_evaluation/{run_id}")

    if not results_dir.exists():
        log.info("No evaluation results directory found yet.")
        return

    # Look for results JSON
    for json_file in results_dir.rglob("*.json"):
        log.info(f"Results file: {json_file}")
        with open(json_file) as f:
            data = json.load(f)
        if isinstance(data, dict):
            resolved = data.get("resolved", [])
            total = data.get("total", len(data))
            if resolved is not None:
                log.info(f"  Resolved: {len(resolved) if isinstance(resolved, list) else resolved}")
            if isinstance(total, int):
                log.info(f"  Total: {total}")


def main() -> int:
    """CLI entry point for evaluation."""
    parser = argparse.ArgumentParser(
        description="Evaluate Squad SWE-bench predictions",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        default=None,
        help="Path to predictions.json (default: from config)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--backend",
        choices=["docker", "modal"],
        default=None,
        help="Evaluation backend (default: from config)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Max parallel eval workers",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Run ID for the evaluation",
    )
    parser.add_argument(
        "--instance-ids",
        nargs="+",
        default=None,
        help="Evaluate specific instance IDs only",
    )
    args = parser.parse_args()

    # Load config
    cfg = load_config(args.config.resolve())

    # Resolve parameters (CLI > config > defaults)
    predictions_path = args.predictions or Path(
        cfg.get("predictions_file", "output/predictions.json")
    )
    if not predictions_path.is_absolute():
        predictions_path = args.config.parent / predictions_path

    backend = args.backend or cfg.get("eval_backend", "docker")
    max_workers = args.max_workers or cfg.get("eval_max_workers", 8)
    run_id = args.run_id or cfg.get("run_id", "squad_eval")
    dataset = cfg.get("dataset", "princeton-nlp/SWE-bench_Lite")

    # Run evaluation
    exit_code = run_evaluation(
        predictions_path,
        dataset=dataset,
        backend=backend,
        max_workers=max_workers,
        run_id=run_id,
        instance_ids=args.instance_ids,
    )

    if exit_code == 0:
        print_results(run_id)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
