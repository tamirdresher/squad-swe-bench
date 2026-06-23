# Squad v0.9.6 — Multi-Agent Orchestration for SWE-bench

Squad is a multi-agent orchestration framework built on GitHub Copilot CLI. It uses a coordinator agent (Picard/Lead) that routes software engineering tasks to specialist sub-agents (Data/Code Expert) with full context — charters, history, and team decisions.

## Results

```
Resolved: 198 / 300 (66.0%)
No generation: 20
Patch apply errors: 38
Unresolved (tests fail): 44
```

### Per-repo breakdown

| Repository | Resolved | Total | Rate |
|-----------|----------|-------|------|
| django__django | 84 | 114 | 73.7% |
| sympy__sympy | 48 | 77 | 62.3% |
| matplotlib__matplotlib | 15 | 23 | 65.2% |
| scikit-learn__scikit-learn | 14 | 23 | 60.9% |
| pytest-dev__pytest | 12 | 17 | 70.6% |
| sphinx-doc__sphinx | 11 | 16 | 68.8% |
| astropy__astropy | 4 | 6 | 66.7% |
| mwaskom__seaborn | 3 | 4 | 75.0% |
| pylint-dev__pylint | 3 | 6 | 50.0% |
| pallets__flask | 2 | 3 | 66.7% |
| pydata__xarray | 2 | 5 | 40.0% |
| psf__requests | 0 | 6 | 0.0% |

## System Description

Squad operates as a multi-agent team:

1. **Coordinator (Squad)** — Receives the task, reads routing rules, and dispatches to the appropriate specialist agent
2. **Lead (Picard)** — Architecture decisions and code review
3. **Code Expert (Data)** — C#, Go, .NET, Python — the primary implementation agent for SWE-bench tasks

For each SWE-bench task:
- The coordinator receives the problem statement
- Routes to Data (Code Expert) with full context (charter, project decisions, history)
- Data analyzes the issue, identifies root cause, and generates a patch
- Single attempt per instance (pass@1)

### Configuration
- Model: `gpt-4o` (for all agents)
- Mode: `copilot --agent squad --yolo --autopilot`
- Max autopilot continues: 50
- Timeout: 1800s per task
- Workers: 4 parallel
- Total runtime: ~21 hours

## Checklist

- [x] Is a pass@1 submission (does not attempt the same task instance more than once)
- [x] Does not use SWE-bench test knowledge (`PASS_TO_PASS`, `FAIL_TO_PASS`)
- [x] Does not use the `hints` field in SWE-bench
- [x] Does not have web-browsing OR has taken steps to prevent lookup of SWE-bench solutions via web-browsing

### Web browsing note
Squad agents do not have web browsing capabilities. The system operates purely on the local repository checkout and the problem statement provided by SWE-bench.

## Integrity Verification

- All 300 SWE-bench Lite instances evaluated
- No scaffold contamination in patches (no `.squad/` or `.github/agents/` files in diffs)
- 100% of worker logs confirm Squad Team Mode active
- Data subagent confirmed spawned for every task
- Evaluation via official `swebench.harness.run_evaluation` Docker harness

## Authors

- **Tamir Dresher** — Microsoft, Creator of Squad
