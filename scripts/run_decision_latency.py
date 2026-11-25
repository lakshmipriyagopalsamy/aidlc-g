"""Decision-latency experiment: the same change at each risk tier, on a simulated clock.

    .venv/bin/python scripts/run_decision_latency.py [--minutes 120]

Every human step is assumed to wait --minutes before it is answered (approvals of
one artifact version in parallel). Writes results/decision_latency.{md,json}.
Assumed response times, not measured ones; the counts of decisions are exact.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.latency import comparison  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=int, default=120, help="assumed wait for each human step")
    parser.add_argument("--out", default="results")
    args = parser.parse_args()
    rows = comparison(args.minutes)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "decision_latency.json").write_text(json.dumps({"assumed_minutes_per_human_step": args.minutes,
                                                           "rows": rows}, indent=2) + "\n")
    lines = ["# Decision latency by risk tier", "",
             f"> Simulated clock: each human step waits {args.minutes} min; approvals of one artifact version are "
             "answered in parallel. Response times are assumed; the decision counts and the gate outcomes are exact.",
             "", "| Case | Released | Effective tier | Evidence signals | Human decisions | Policy authorizations | "
             "Approval critical path (min) | Lead time (min) | Catch-up decisions |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['case']} | {'yes' if r['released'] else 'no'} | {r['effective_tier']} | "
                     f"{', '.join(r['signals']) or '—'} | {r['human_decisions']} | {r['policy_decisions']} | "
                     f"{r['critical_path_minutes']} | {r['lead_time_minutes']} | {', '.join(r['catch_up']) or '—'} |")
    (out / "decision_latency.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
