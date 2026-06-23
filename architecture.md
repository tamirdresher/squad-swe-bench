# SWE-bench Runner Architecture

**Author:** Picard (Lead Architect)  
**Date:** 2026-06-21  
**Status:** Design — ready for implementation  
**Issue:** #3855

---

## 1. Design Principles

1. **Squad is a black box.** The runner invokes `copilot --agent squad --yolo -p "..."` and captures `git diff`. It does not import Squad internals.
2. **Fail-safe per task.** Any single task can crash, timeout, or produce garbage. The runner continues, writes an empty patch, and moves on.
3. **Incremental output.** `predictions.json` is written after every completed task, not only at the end. A killed run still has partial results.
4. **Budget circuit breaker.** Estimated cost is tracked. If cumulative spend projection exceeds `budget_cap_usd`, remaining tasks are skipped.
5. **Deterministic ordering.** Tasks are sorted by `instance_id` before execution. Results are reproducible given the same config.

---

## 2. Directory Structure

```
benchmarks/swe-bench/
│
├── runner.py                  # Entry point — CLI arg parsing, orchestration loop
├── eval.py                    # Evaluation wrapper (calls swebench harness)
├── submit.py                  # sb-cli submission helper
├── config.yaml                # Default configuration (all tunables)
├── requirements.txt           # Python deps
│
├── lib/
│   ├── __init__.py
│   ├── config.py              # Config loading, validation, CLI override merge
│   ├── dataset.py             # Load HuggingFace dataset, group by repo
│   ├── worktree.py            # Git clone + worktree lifecycle
│   ├── team_manager.py        # Squad team init per repo, reuse tracking
│   ├── squad_invoker.py       # Subprocess wrapper for copilot CLI
│   ├── patch_extractor.py     # git diff capture, sanitization, size check
│   ├── budget.py              # Cost accumulator + circuit breaker
│   ├── predictions.py         # predictions.json incremental writer
│   └── reporting.py           # Run summary, timing stats, cost report
│
├── prompts/
│   ├── team_init.txt          # Per-repo team learning prompt
│   └── task.txt               # Per-task issue-solving prompt
│
├── workdir/                   # Created at runtime (gitignored)
│   ├── django/                # Bare clone of django/django
│   │   ├── worker-0/          # Worktree for worker 0
│   │   ├── worker-1/          # Worktree for worker 1
│   │   └── ...
│   ├── sympy/
│   └── ...
│
└── runs/                      # Created at runtime
    └── {run_id}/
        ├── predictions.json   # Incrementally assembled
        ├── run_meta.json      # Config snapshot + final stats
        ├── runner.log         # Full runner log
        ├── logs/              # Per-task copilot stdout/stderr
        └── patches/           # Individual .patch files
```

---

## 3. Execution Flow

### 3.1 High-Level Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                         runner.py main()                             │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Phase 1: SETUP                                                     │
│  ├── Load config.yaml + CLI overrides                               │
│  ├── Load dataset from HuggingFace (300 instances)                  │
│  ├── Group instances by repo (12 groups)                            │
│  ├── Sort deterministically by instance_id                          │
│  ├── Create run output directory                                    │
│  └── Initialize budget tracker                                      │
│                                                                     │
│  Phase 2: REPO PREPARATION (sequential, one-time per repo)          │
│  ├── For each unique repo:                                          │
│  │   ├── git clone {repo_url} workdir/{repo_slug}/main              │
│  │   ├── Create N worktrees: workdir/{repo_slug}/worker-{0..N-1}    │
│  │   ├── cd workdir/{repo_slug}/worker-0                            │
│  │   ├── squad init                                                 │
│  │   └── copilot --agent squad --yolo -p "{team_init_prompt}"       │
│  │       (one-time team learning — reused for all tasks in repo)    │
│  └── Done                                                           │
│                                                                     │
│  Phase 3: TASK EXECUTION (parallel within repo group)               │
│  ├── For each repo group:                                           │
│  │   └── ProcessPoolExecutor(max_workers=N):                        │
│  │       ├── Worker picks next task from queue                      │
│  │       ├── cd worktree, git checkout {base_commit}, git clean -fd │
│  │       ├── copilot --agent squad --yolo -p "{task_prompt}"        │
│  │       │   (timeout: 10 min)                                      │
│  │       ├── Capture: git diff HEAD > patch                         │
│  │       ├── Sanitize patch (strip tests, fencing, size check)      │
│  │       ├── Append to predictions.json                             │
│  │       ├── Update budget tracker                                  │
│  │       └── Log result (success/timeout/crash/empty)               │
│  └── Check budget circuit breaker between repo groups               │
│                                                                     │
│  Phase 4: FINALIZATION                                              │
│  ├── Write final predictions.json (sorted by instance_id)           │
│  ├── Generate run_meta.json (stats, cost, timing)                   │
│  ├── Clean up worktrees (if configured)                             │
│  └── Print summary to stdout                                        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Phase 2 Detail: Repo Preparation

The 300 SWE-bench Lite tasks span 12 repos. Tasks within the same repo share the same codebase (different commits). Strategy:

```python
repos = {
    "django/django": [task1, task2, ..., task_N],
    "sympy/sympy":   [...],
    ...  # 12 total
}

for repo, tasks in repos.items():
    # 1. Clone once (full depth — need arbitrary base_commit checkouts)
    git clone https://github.com/{repo}.git workdir/{slug}/main

    # 2. Create worker worktrees
    for i in range(max_workers):
        git worktree add workdir/{slug}/worker-{i} --detach

    # 3. Initialize Squad team in worker-0
    cd workdir/{slug}/worker-0
    squad init
    copilot --agent squad --yolo -p "{team_init_prompt}"
    
    # 4. Copy .squad/ to other workers (or symlink)
    for i in range(1, max_workers):
        cp -r workdir/{slug}/worker-0/.squad workdir/{slug}/worker-{i}/.squad
```

**Why full clone:** SWE-bench tasks reference arbitrary historical commits. Shallow clones cannot `git checkout {base_commit}` reliably.

**Why copy .squad/:** Squad's team knowledge lives in `.squad/`. Copying ensures all workers share the same learned team context without re-running team init.

### 3.3 Phase 3 Detail: Task Execution

Each worker runs this loop:

```python
def execute_task(task, worktree_path, timeout_min):
    os.chdir(worktree_path)
    
    # 1. Reset to task's base commit
    subprocess.run(["git", "checkout", task["base_commit"], "--force"])
    subprocess.run(["git", "clean", "-fdx"])
    
    # 2. Build the task prompt
    prompt = TASK_TEMPLATE.format(
        problem_statement=task["problem_statement"],
        repo=task["repo"],
        instance_id=task["instance_id"],
        base_commit=task["base_commit"]
    )
    
    # 3. Invoke Squad
    result = subprocess.run(
        ["copilot", "--agent", "squad", "--yolo", "-p", prompt],
        capture_output=True, text=True,
        timeout=timeout_min * 60,
        cwd=worktree_path
    )
    
    # 4. Capture patch
    diff = subprocess.run(
        ["git", "diff", "HEAD"],
        capture_output=True, text=True,
        cwd=worktree_path
    )
    
    return sanitize_patch(diff.stdout)
```

---

## 4. Parallelism Model

### 4.1 Why Worktrees

Each `copilot --agent squad --yolo` session modifies files in the working directory. Parallel tasks in the same directory would corrupt each other. Git worktrees give each worker an independent working copy backed by a shared object store (single clone, multiple checkouts).

### 4.2 Worker Assignment

```
                    ┌─────────────────────────┐
                    │      Task Queue          │
                    │  (sorted by instance_id) │
                    └────────────┬────────────┘
                                 │
            ┌────────────────────┼────────────────────┐
            │                    │                    │
     ┌──────▼──────┐     ┌──────▼──────┐     ┌──────▼──────┐
     │  Worker 0   │     │  Worker 1   │     │  Worker 2   │
     │ worktree-0  │     │ worktree-1  │     │ worktree-2  │
     └─────────────┘     └─────────────┘     └─────────────┘
```

Workers pull from a thread-safe queue. Each worker owns exactly one worktree. When a worker finishes a task, it resets its worktree and picks the next task.

### 4.3 Process vs Thread

Use **processes** (not threads):
- `copilot` CLI is a subprocess anyway — no GIL concern
- Process isolation prevents one crashed Squad session from affecting others
- `concurrent.futures.ProcessPoolExecutor` with `max_workers` from config

### 4.4 Repo-Sequential, Task-Parallel

Tasks are grouped by repo. Within a repo group, workers run in parallel. Between repos, we do a synchronization point:

```
django/django: [task1, task2, ..., task89]  → 4 workers parallel
  ↓ sync point (all workers finish django)
sympy/sympy: [task90, ..., task140]         → 4 workers parallel
  ↓ sync point
...
```

**Why:** Each repo needs its own clone + worktrees + team init. Processing one repo at a time avoids multiplying disk usage by 12× and keeps team init overhead minimal.

---

## 5. Team Init & Reuse

### 5.1 The Problem

Squad works best when it "knows" a repo — its structure, testing conventions, coding style. Without team init, every task starts cold.

### 5.2 The Solution

Per repo, one time:

```bash
# In worker-0's worktree
squad init

copilot --agent squad --yolo -p "
Read this repository and learn it. Create a team that can handle bug fixes.
Focus on: directory structure, test framework, coding conventions, import patterns.
Do NOT make any code changes. Just learn the codebase.
"
```

This produces a `.squad/` directory with team knowledge. That directory is copied to all other worktrees for the same repo.

### 5.3 Reuse Across Tasks

After team init, the `.squad/` directory persists across tasks in the same repo. Between tasks, only the git working tree is reset (`git checkout --force` + `git clean`). The `.squad/` directory is preserved (it's not tracked by git).

### 5.4 No Reuse Across Repos

Each repo gets a fresh team init. A Django team wouldn't help with SymPy. The 12 repos mean 12 team inits — ~5 min each = ~60 min total overhead (run once, amortized across 300 tasks).

---

## 6. Timeout Handling

### 6.1 Per-Task Timeout

```python
try:
    result = subprocess.run(
        ["copilot", "--agent", "squad", "--yolo", "-p", prompt],
        timeout=timeout_minutes * 60,  # default: 600s
        ...
    )
except subprocess.TimeoutExpired:
    # Kill the process tree
    kill_process_tree(result.pid)
    
    # Still try to capture any partial work
    diff = subprocess.run(["git", "diff", "HEAD"], ...)
    
    # If no diff, write empty patch
    patch = diff.stdout.strip() if diff.stdout.strip() else ""
    
    log_timeout(task["instance_id"])
```

### 6.2 Partial Work on Timeout

If Squad made partial progress before timeout (committed or staged changes), we still capture the diff. A partial fix might pass some tests. Only if there's truly no diff do we write an empty patch.

### 6.3 Team Init Timeout

Separate, shorter timeout (default 5 min). If team init times out, retry once. If it fails again, proceed without team knowledge (cold start for that repo's tasks).

---

## 7. Error Recovery

### 7.1 Error Categories

| Error | Detection | Recovery |
|---|---|---|
| Copilot process crash (non-zero exit) | `returncode != 0` | Retry once (if `retry_on_crash: true`) |
| Copilot process timeout | `TimeoutExpired` exception | Kill tree, capture partial, write empty |
| Git checkout fails | `returncode != 0` on git | Skip task, log error, empty patch |
| Disk full | OSError or git failure | Abort run, write what we have |
| Budget exceeded | Budget tracker check | Skip remaining, finalize predictions |
| Invalid patch (too large) | Size > `max_patch_size_kb` | Discard, write empty patch |
| Worker process dies | ProcessPoolExecutor exception | Log, mark task failed, continue |

### 7.2 Retry Logic

```python
for attempt in range(1 + int(config.retry_on_crash)):
    try:
        patch = execute_task(task, worktree, timeout)
        if patch:
            break
    except (subprocess.CalledProcessError, OSError) as e:
        if attempt == 0 and config.retry_on_crash:
            time.sleep(config.retry_delay_seconds)
            reset_worktree(worktree, task["base_commit"])
            continue
        patch = ""
        break
```

### 7.3 Crash Recovery (Run-Level)

If the entire runner process is killed (Ctrl+C, OOM, machine restart):
- `predictions.json` was written incrementally — partial results exist
- `run_meta.json` tracks which `instance_id`s completed
- Re-run with `--resume` flag: loads existing predictions, skips completed tasks

```bash
# Resume a killed run
python runner.py --config config.yaml --resume runs/squad_v1_lite_20260621/
```

---

## 8. Output Format

### 8.1 predictions.json

The SWE-bench required format — one entry per task:

```json
[
  {
    "instance_id": "django__django-11999",
    "model_name_or_path": "squad-v1",
    "model_patch": "diff --git a/django/db/models/sql/query.py b/...\n--- a/...\n+++ b/...\n@@ ...\n"
  },
  {
    "instance_id": "django__django-12286",
    "model_name_or_path": "squad-v1",
    "model_patch": ""
  }
]
```

- Empty `model_patch` = task unresolved (timeout, crash, no changes)
- Sorted by `instance_id` in final output
- Written incrementally during run (append + re-sort on finalize)

### 8.2 run_meta.json

```json
{
  "run_id": "squad_v1_lite_20260621",
  "config": { "...snapshot of config..." },
  "started_at": "2026-06-21T09:00:00Z",
  "finished_at": "2026-06-21T12:30:00Z",
  "duration_minutes": 210,
  "tasks_total": 300,
  "tasks_completed": 300,
  "tasks_with_patch": 245,
  "tasks_empty_patch": 55,
  "tasks_timeout": 12,
  "tasks_crash": 3,
  "estimated_cost_usd": 105.00,
  "repos_processed": 12,
  "team_inits_succeeded": 12,
  "budget_exceeded": false
}
```

### 8.3 Per-Task Artifacts

Each task produces:
- `logs/{instance_id}.log` — Full copilot stdout + stderr
- `patches/{instance_id}.patch` — Raw git diff output (before sanitization)

---

## 9. Patch Sanitization

Raw `git diff` output may need cleaning before it's a valid SWE-bench prediction:

### 9.1 Strip Test File Changes

SWE-bench evaluates by running existing tests. If Squad modifies test files, those changes must be stripped:

```python
def strip_test_changes(patch: str) -> str:
    """Remove hunks that modify test files."""
    # Split on 'diff --git' boundaries
    # Filter out files matching: test_*, tests/*, */tests/*, conftest.py
    ...
```

### 9.2 Strip Markdown Fencing

If Squad wraps output in code fences:

```python
def strip_fencing(patch: str) -> str:
    patch = re.sub(r'^```(?:diff|patch)?\s*\n', '', patch)
    patch = re.sub(r'\n```\s*$', '', patch)
    return patch
```

### 9.3 Validate Patch Format

```python
def is_valid_patch(patch: str) -> bool:
    if not patch.strip():
        return False  # empty is valid (means "no prediction")
    if not patch.startswith("diff --git"):
        return False
    if len(patch.encode()) > max_patch_size_kb * 1024:
        return False
    return True
```

---

## 10. Dev Run vs Official Test Run

| Aspect | Dev Run (`--mode dev`) | Official Run (`--mode test`) |
|---|---|---|
| Dataset split | `dev` (not scored) | `test` (300 official tasks) |
| Workers | 2 | 4–8 |
| Timeout | 5 min | 10 min |
| Budget cap | $20 | $200 |
| Eval backend | Local Docker | Modal cloud |
| Submit | Never | After manual review |
| Purpose | Tune prompts, validate pipeline | Produce leaderboard submission |

### 10.1 Iterating on Prompts

```bash
# Run 5 specific instances in dev mode
python runner.py --mode dev --instance-ids \
    "django__django-11999,sympy__sympy-20590,pytest__pytest-5103,flask__flask-4045,requests__requests-3362"

# Check results
python eval.py --predictions runs/dev_latest/predictions.json --backend docker

# Tweak prompts/task.txt, re-run
```

### 10.2 Graduating to Official

1. Run full dev split → confirm resolve rate is reasonable (>15%)
2. Review `run_meta.json` — no excessive timeouts or crashes
3. Confirm budget projection for 300 test tasks is within cap
4. Run official: `python runner.py --mode test --run-id squad_v1_lite_YYYYMMDD`
5. Evaluate: `python eval.py --predictions ... --backend modal`
6. Submit: `python submit.py --predictions ... --run-id ...`

---

## 11. Cost Controls

### 11.1 Budget Tracking

```python
class BudgetTracker:
    def __init__(self, cap_usd, cost_per_task, warn_pct):
        self.cap = cap_usd
        self.per_task = cost_per_task
        self.warn_pct = warn_pct
        self.tasks_completed = 0
    
    @property
    def estimated_spend(self):
        return self.tasks_completed * self.per_task
    
    @property
    def should_abort(self):
        return self.estimated_spend >= self.cap
    
    def record_task(self):
        self.tasks_completed += 1
        if self.estimated_spend >= self.cap * self.warn_pct / 100:
            log.warning(f"Budget at {self.pct_used}%")
```

### 11.2 Circuit Breaker

Checked between repo groups (sync points):

```python
for repo, tasks in repo_groups.items():
    if budget.should_abort:
        log.error(f"Budget exceeded (${budget.estimated_spend:.2f} >= ${budget.cap}). Aborting.")
        break
    
    run_repo_tasks(repo, tasks)
    budget.record_batch(len(tasks))
```

### 11.3 Early Abort CLI Flag

```bash
# Stop after 50 tasks regardless of budget
python runner.py --config config.yaml --limit 50

# Abort if more than 20 consecutive empty patches (model is broken)
python runner.py --config config.yaml --max-consecutive-empty 20
```

---

## 12. Prompt Design

### 12.1 Team Init Prompt (`prompts/team_init.txt`)

```
Read this repository and learn it. Create a team that can handle Python bug fixes.

Focus on understanding:
1. Directory structure and module organization
2. How tests are structured and run (pytest, unittest, tox)
3. Coding conventions (imports, docstrings, type hints)
4. Build/install process

Do NOT make any code changes. Do NOT commit anything. Just learn.
When done, say "Team ready."
```

### 12.2 Task Prompt (`prompts/task.txt`)

```
Fix this GitHub issue:

{problem_statement}

Repository: {repo}
You are on commit: {base_commit}

Rules:
- Make minimal changes to fix the issue
- Do NOT modify test files (tests/, test_*, conftest.py)
- Do NOT add new test files
- Ensure the fix handles edge cases mentioned in the issue
- Commit your changes when done

ralph, go
```

**Why `ralph, go`:** This triggers Squad's task execution through the Ralph coordinator agent, which is Squad's standard way to dispatch work to the team.

---

## 13. Logging & Observability

### 13.1 Log Levels

| Level | What |
|---|---|
| DEBUG | Git commands, subprocess args, worktree operations |
| INFO | Task start/complete, repo transitions, budget status |
| WARNING | Timeouts, retries, budget at warn threshold |
| ERROR | Crashes, budget exceeded, invalid patches |

### 13.2 Progress Reporting

During execution, stdout shows:

```
[12:03:15] django/django: 15/89 tasks | 3 patched, 1 timeout | budget: $5.25/$200
[12:03:45] django/django: 16/89 tasks | 4 patched, 1 timeout | budget: $5.60/$200
```

### 13.3 Post-Run Summary

```
═══════════════════════════════════════════════
  SWE-bench Lite Run Complete: squad_v1_lite_20260621
═══════════════════════════════════════════════
  Total tasks:    300
  With patch:     237 (79.0%)
  Empty patch:     63 (21.0%)
  Timeouts:        12 ( 4.0%)
  Crashes:          3 ( 1.0%)
  Duration:      3h 27m
  Est. cost:     $105.00
  Predictions:   runs/squad_v1_lite_20260621/predictions.json
═══════════════════════════════════════════════
```

---

## 14. Evaluation Phase

### 14.1 Docker (Local)

```python
# eval.py wraps:
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Lite \
    --predictions_path {predictions_json} \
    --max_workers {eval_max_workers} \
    --run_id {run_id}
```

Requires ~120 GB disk for Docker images. Each instance runs in its own container.

### 14.2 Modal (Cloud, Recommended)

```python
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Lite \
    --predictions_path {predictions_json} \
    --parallelism {modal_parallelism} \
    --modal true
```

No local Docker. ~$30 compute cost for 300 tasks.

### 14.3 Results Interpretation

The harness produces:
- Per-instance: RESOLVED / UNRESOLVED / ERROR
- Aggregate: `resolve_rate = resolved / total`

Competitive range for GPT-4o-based agents: 20–35%.

---

## 15. Future Enhancements (Out of Scope for v1)

1. **Trajectory logging** — Record Squad's step-by-step reasoning (required for some leaderboard categories)
2. **SWE-bench Verified** — 500 tasks, human-confirmed solvable, harder but fairer
3. **Model comparison** — Run same tasks with different models (GPT-4o, Claude, o3-mini)
4. **Prompt optimization** — A/B test prompt variants on dev split
5. **Real-time cost tracking** — Parse Copilot API headers for actual token usage
6. **Caching** — Skip tasks whose base_commit + prompt hash match a previous run

---

## 16. Sequence Diagram: Single Task

```
Runner              Worker              Git              Copilot CLI          Squad
  │                   │                  │                    │                 │
  │──dispatch task──▶│                  │                    │                 │
  │                   │──checkout────────▶│                    │                 │
  │                   │◀──── ok ─────────│                    │                 │
  │                   │──clean -fdx─────▶│                    │                 │
  │                   │◀──── ok ─────────│                    │                 │
  │                   │                  │                    │                 │
  │                   │──copilot --yolo──────────────────────▶│                 │
  │                   │                  │                    │──invoke squad──▶│
  │                   │                  │                    │                 │
  │                   │                  │      (Squad works: reads code,      │
  │                   │                  │       edits files, commits)         │
  │                   │                  │                    │                 │
  │                   │                  │                    │◀─── done ──────│
  │                   │◀────── stdout/exit ──────────────────│                 │
  │                   │                  │                    │                 │
  │                   │──git diff HEAD──▶│                    │                 │
  │                   │◀──── patch ──────│                    │                 │
  │                   │                  │                    │                 │
  │◀──return patch───│                  │                    │                 │
  │                   │                  │                    │                 │
  │──write predictions.json             │                    │                 │
  │──update budget                      │                    │                 │
```

---

## 17. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | `copilot` CLI not available in headless/CI environment | Medium | Blocking | Test in target env early; document auth requirements |
| 2 | Squad modifies test files (disqualifies patch) | High | Medium | `strip_test_changes` post-processing |
| 3 | Squad produces non-diff output (prose, markdown) | Medium | Low | `strip_fencing` + `is_valid_patch` check |
| 4 | Rate limiting on Copilot API at scale | Medium | High | `max_workers ≤ 4` default; exponential backoff on 429 |
| 5 | Budget overrun (model does expensive retries) | Low | Medium | Hard cap + circuit breaker |
| 6 | Team init produces inconsistent state across workers | Low | Medium | Copy `.squad/` from worker-0 after init completes |
| 7 | Disk exhaustion from 12 full repo clones | Low | High | Monitor disk; abort early; clone one repo at a time |
| 8 | SWE-bench dataset format changes | Low | Low | Pin `swebench` package version |

---

## 18. Dependencies

```
# requirements.txt
datasets>=2.14.0          # HuggingFace dataset loading
swebench>=1.0.0           # Evaluation harness
pyyaml>=6.0               # Config parsing
tqdm>=4.65                 # Progress bars
modal>=0.60.0             # Cloud evaluation (optional)
sb-cli>=0.1.0             # Leaderboard submission (optional)
```

---

*Architecture designed by Picard. Implementation tracked in issue #3855.*
