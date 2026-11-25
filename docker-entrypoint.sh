#!/bin/sh
# test: run the suite. reproduce: re-run every experiment that the papers report.
set -e
case "$1" in
  test) exec python -m pytest -q ;;
  reproduce)
    python scripts/run_trials.py --repeats 20
    python scripts/run_vertical_slice.py
    python scripts/run_baselines.py
    python scripts/run_invalidation.py
    python scripts/run_model_check.py
    python scripts/run_decision_latency.py
    python scripts/run_latency_sensitivity.py
    python scripts/run_scenario_binding.py
    python scripts/run_incident_study.py --per-class 30 --out results/incident30
    python scripts/run_incident_study.py --per-class 10
    python scripts/run_oncall_demo.py ;;
  *) exec "$@" ;;
esac
