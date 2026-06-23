# Squad — SWE-bench Lite Results

**198 / 300 resolved (66.0%)** — #1 on the [SWE-bench Lite leaderboard](https://www.swebench.com/index.html)

## Results

| Metric | Value |
|--------|-------|
| Resolved | 198 / 300 (66.0%) |
| Patches generated | 280 / 300 (93.3%) |
| Patch apply errors | 38 (12.7%) |
| Unresolved (tests fail) | 44 (14.7%) |
| No generation | 20 (6.7%) |

### Leaderboard Context (June 2026)

| Rank | System | Score |
|------|--------|-------|
| :1st_place_medal: | **Squad v0.9.6** | **66.0%** |
| :2nd_place_medal: | Claude Opus 4.6 | 62.7% |
| :3rd_place_medal: | MiniMax M2.5 | 56.3% |
| 4 | OpenAI GPT-5 | 54.3% |
| 5 | Claude Haiku 4.5 | 54.3% |

## What is Squad?

Squad is a multi-agent orchestration framework built on [GitHub Copilot CLI](https://github.com/features/copilot). Instead of giving one model the entire problem, Squad decomposes work through a team of specialized agents:

- **Coordinator** — Routes tasks, manages team state, never writes code
- **Data (Code Expert)** — Implementation specialist, navigates codebases, generates patches
- **Scribe** — Memory management, decisions, context sharing

Learn more: [bradygaster.github.io/squad](https://bradygaster.github.io/squad/)

## Configuration

| Parameter | Value |
|-----------|-------|
| Model | gpt-4o |
| Agent | squad |
| Mode | autopilot (--yolo) |
| Max continuations | 50 |
| Timeout | 1800s (30 min/task) |
| Workers | 4 (parallel) |
| Total runtime | ~21 hours |

- **Pass@1** — Each instance attempted exactly once
- **No test knowledge** — No PASS_TO_PASS, FAIL_TO_PASS, or hints_text
- **No web browsing** — Agents work only on local repo + issue description

## Repository Structure

`
.
├── squad_swebench_runner.py   # Main orchestrator
├── config.yaml                # Runner configuration
├── requirements.txt           # Python dependencies
├── eval_docker.sh             # Evaluation harness script
├── evaluate.py                # Evaluation runner
├── architecture.md            # System architecture notes
│
├── squad-scaffold/            # Agent team config
│   └── .squad/
│       ├── team.md
│       ├── routing.md
│       └── agents/
│
├── output/
│   ├── predictions.json       # All 300 patches
│   ├── squad-v1.squad_v1.json # Eval report
│   ├── run_metadata.json
│   └── logs/                  # 1284 worker logs
│
├── submission/
│   ├── metadata.yaml
│   ├── README.md
│   ├── blog-post-swe-bench-results.md
│   └── 20250623_squad_v0.9.6_gpt4o/
│       └── results/
│           ├── results.json
│           └── resolved_by_repo.json
│
└── evidence/
    └── swe-bench-lite-leaderboard-2026-06-23.png
`

## Reproducing the Results

### 1. Re-run evaluation (verify our numbers)

`ash
pip install swebench
python evaluate.py
`

This applies patches from output/predictions.json against the official SWE-bench Lite dataset and runs test suites.

### 2. Re-run Squad (generate new predictions)

`ash
pip install -r requirements.txt
# Requires: GitHub Copilot CLI with Squad agent installed
python squad_swebench_runner.py
`

## Per-Repository Breakdown

| Repository | Resolved | Total | Rate |
|-----------|----------|-------|------|
| mwaskom/seaborn | 3 | 4 | 75.0% |
| django/django | 84 | 114 | 73.7% |
| pytest-dev/pytest | 12 | 17 | 70.6% |
| sphinx-doc/sphinx | 11 | 16 | 68.8% |
| astropy/astropy | 4 | 6 | 66.7% |
| pallets/flask | 2 | 3 | 66.7% |
| matplotlib/matplotlib | 15 | 23 | 65.2% |
| sympy/sympy | 48 | 77 | 62.3% |
| scikit-learn/scikit-learn | 14 | 23 | 60.9% |
| pylint-dev/pylint | 3 | 6 | 50.0% |
| pydata/xarray | 2 | 5 | 40.0% |
| psf/requests | 0 | 6 | 0.0% |

## Technical Report

Full technical report: [submission/blog-post-swe-bench-results.md](submission/blog-post-swe-bench-results.md)

Also available as PR: [bradygaster/squad#1373](https://github.com/bradygaster/squad/pull/1373)

## License

MIT