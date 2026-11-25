"""Run the Template M trial matrix and report section 24.1 gate metrics.

    .venv/bin/python scripts/run_trials.py --repeats 20

Writes results/trial_report.json and results/trial_report.md. The trials are
deterministic synthetic scenarios: they measure whether the gate's predeclared
oracle holds, not lifecycle effectiveness (compendium section 24.2).
"""

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from aidlc_g import __version__
from aidlc_g.lifecycle import Stage
from aidlc_g.testkit import World
from aidlc_g.trials import TRIALS, TrialResult


def percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
    return ordered[index]


def describe(samples: List[float]) -> Dict[str, Any]:
    ms = [x * 1000 for x in samples]
    return {"samples": len(ms), "median": statistics.median(ms), "p95": percentile(samples, 95) * 1000,
            "p25": percentile(samples, 25) * 1000, "p75": percentile(samples, 75) * 1000,
            "min": min(ms), "max": max(ms), "mean": statistics.mean(ms), "stdev": statistics.stdev(ms)}


def environment() -> Dict[str, Any]:
    import sqlite3
    import subprocess
    cpu = platform.processor()
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip() or cpu
    except OSError:
        pass
    return {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(),
            "cpu": cpu, "sqlite": sqlite3.sqlite_version, "store": "sqlite in-memory"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=10, help="runs per trial")
    parser.add_argument("--out", default="results", help="output directory")
    parser.add_argument("--latency-runs", type=int, default=100, help="valid end-to-end runs timed for latency")
    parser.add_argument("--warmup", type=int, default=5, help="untimed runs before latency measurement")
    args = parser.parse_args()

    results: List[TrialResult] = []
    for trial_id, scenario, expected, violating, fn in TRIALS:
        for _ in range(args.repeats):
            observed, evidence = fn(World())
            results.append(TrialResult(trial_id, scenario, expected, observed, violating, evidence))

    # Latency of a single transition evaluation on the full valid path (all gates).
    # Each valid run times every transition evaluation (perf_counter around the gate call); the last one is
    # the full release gate, which re-checks all upstream evidence. Warm-up runs are discarded.
    latencies: List[float] = []
    full_gate: List[float] = []
    for i in range(args.warmup + args.latency_runs):
        w = World()
        w.advance_to(Stage.RELEASE_READY)
        if i >= args.warmup:
            latencies.extend(w.latencies)
            full_gate.append(w.latencies[-1])

    violating = [r for r in results if r.violating]
    valid = [r for r in results if not r.violating]
    detected = sum(1 for r in violating if r.passed)
    false_rejections = sum(1 for r in valid if not r.passed)

    per_trial: Dict[str, Dict[str, Any]] = {}
    for r in results:
        row = per_trial.setdefault(r.trial_id, {"scenario": r.scenario, "expected": r.expected,
                                               "observed": {}, "runs": 0, "passed": 0,
                                               "violating": r.violating, "evidence": r.evidence})
        row["runs"] += 1
        row["passed"] += int(r.passed)
        row["observed"][r.observed] = row["observed"].get(r.observed, 0) + 1

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "package_version": __version__,
        "data_provenance": "synthetic, author-created smoke scenarios (not empirical observations)",
        "environment": environment(),
        "latency_method": {"timer": "time.perf_counter around GovernanceService.evaluate_transition",
                           "timer_resolution_s": time.get_clock_info("perf_counter").resolution,
                           "warmup_runs_discarded": args.warmup, "timed_runs": args.latency_runs,
                           "load": "single process on an otherwise idle laptop; not isolated from OS scheduling"},
        "repeats_per_trial": args.repeats,
        "metrics": {
            "violation_detection_recall": {"detected": detected, "total": len(violating),
                                           "value": detected / len(violating) if violating else None},
            "false_rejection_rate": {"rejected": false_rejections, "total": len(valid),
                                     "value": false_rejections / len(valid) if valid else None},
            "transition_latency_ms": describe(latencies),
            "full_release_gate_latency_ms": describe(full_gate),
        },
        "trials": per_trial,
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "trial_report.json").write_text(json.dumps(report, indent=2))

    m = report["metrics"]
    lines = [
        "# AIDLC-G gate trial report",
        "",
        f"Generated {report['generated_at']} · aidlc-g {__version__} · Python {report['environment']['python']} "
        f"on {report['environment']['machine']} · {args.repeats} runs per trial",
        "",
        f"> Data provenance: {report['data_provenance']}. These numbers show the gate's predeclared "
        "oracle holds on seeded scenarios; they are not evidence of lifecycle effectiveness.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Violation detection recall | {detected}/{len(violating)} = {m['violation_detection_recall']['value']:.3f} |",
        f"| False rejection rate | {false_rejections}/{len(valid)} = {m['false_rejection_rate']['value']:.3f} |",
        f"| Transition latency, median / p95 (IQR) | {m['transition_latency_ms']['median']:.2f} ms / "
        f"{m['transition_latency_ms']['p95']:.2f} ms ({m['transition_latency_ms']['p25']:.2f}–"
        f"{m['transition_latency_ms']['p75']:.2f}; n={len(latencies)}) |",
        f"| Full release gate latency, median / p95 (IQR) | {m['full_release_gate_latency_ms']['median']:.2f} ms / "
        f"{m['full_release_gate_latency_ms']['p95']:.2f} ms ({m['full_release_gate_latency_ms']['p25']:.2f}–"
        f"{m['full_release_gate_latency_ms']['p75']:.2f}; n={len(full_gate)}) |",
        f"| Environment | {report['environment']['cpu']}, Python {report['environment']['python']}, "
        f"SQLite {report['environment']['sqlite']} (in memory); {args.warmup} warm-up runs discarded |",
        "",
        "| Trial | Scenario | Expected | Observed | Pass |",
        "|---|---|---|---|---|",
    ]
    for tid, row in per_trial.items():
        observed = ", ".join(f"{k}×{v}" for k, v in row["observed"].items())
        lines.append(f"| {tid} | {row['scenario']} | {row['expected']} | {observed} | {row['passed']}/{row['runs']} |")
    lines += ["", "## Evidence per trial (last run)", ""]
    for tid, row in per_trial.items():
        lines.append(f"**{tid}** — " + "; ".join(row["evidence"]))
        lines.append("")
    (out / "trial_report.md").write_text("\n".join(lines))

    print("\n".join(lines[:16]))
    print(f"\nwrote {out / 'trial_report.json'} and {out / 'trial_report.md'}")
    return 0 if detected == len(violating) and false_rejections == 0 else 1


if __name__ == "__main__":
    started = time.perf_counter()
    code = main()
    print(f"elapsed {time.perf_counter() - started:.1f}s")
    sys.exit(code)
