# Reproducing the results

Every reported result comes from scripts in `scripts/`, which write the files in `results/`. All
data is synthetic and the runs are deterministic, except thread scheduling in two concurrency checks and
machine-dependent latencies.

## Setup

Requires Python 3.9 or later. The committed results were produced with Python 3.9.6 on macOS (Apple M5).

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip   # editable installs need pip 21.3 or later
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/python -m pytest -q
```

Or with Docker (untested by the author):

```bash
docker build -t aidlc-g . && docker run --rm aidlc-g reproduce
```

## Experiments

| Study | Command | Output | Time |
|---|---|---|---|
| Governance trial matrix and gate latency | `scripts/run_trials.py --repeats 20` | `results/trial_report.*` | seconds |
| End-to-end adversarial slice | `scripts/run_vertical_slice.py` | `results/vertical_slice.md` | ~1 min |
| Baseline comparison | `scripts/run_baselines.py` | `results/baseline_comparison.*` | ~2 min |
| Dependency invalidation and races | `scripts/run_invalidation.py` | `results/invalidation_study.*` | seconds |
| Bounded model checking | `scripts/run_model_check.py` | `results/model_check.*` | ~1 s |
| Decision counts by risk tier | `scripts/run_decision_latency.py` | `results/decision_latency.*` | ~30 s |
| Decision-latency sensitivity | `scripts/run_latency_sensitivity.py` | `results/latency_sensitivity.*` | ~30 s |
| Gate performance at scale | `scripts/benchmark.py` | `results/benchmark.*` | ~1 min |
| Scenario-binding study | `scripts/run_scenario_binding.py` | `results/scenario_binding.*` | seconds |
| Incident-triage evidence ablation | `scripts/run_incident_study.py --per-class 30 --out results/incident30` | `results/incident30/` | seconds |
| On-call chat transcript | `scripts/run_oncall_demo.py` | `results/oncall_demo.md` | seconds |

Prefix each command with `.venv/bin/python`. `docker-entrypoint.sh reproduce` re-runs them all.

## What to expect

- **Exact on any machine:** trial outcomes, slice outcomes, baseline and invalidation results, model-checking state counts, decision counts, binding-study results and incident scores.
- **Vary by machine:** latencies. The papers report the environment they were measured in.
