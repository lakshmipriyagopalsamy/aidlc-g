"""Decision-latency sensitivity: the real approval schedule of each risk-tier case, with sampled response times.

    .venv/bin/python scripts/run_latency_sensitivity.py [--samples 2000]

Each case runs once through the real gate to record its decision schedule: which human waits happen,
in what order, and how many people answer each in parallel. Response times are then drawn from
log-normal distributions (median 30, 120 or 480 min; sigma 0.5 or 1.0), with common random numbers
across cases. Writes results/latency_sensitivity.{md,json}. Response times are assumed, not measured.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.latency import sensitivity  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument("--out", default="results")
    args = parser.parse_args()
    result = sensitivity(args.samples)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latency_sensitivity.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = ["# Decision-latency sensitivity", "",
             f"> Schedules from real gate runs; response times sampled ({result['samples']} draws per cell, log-normal, "
             "common random numbers). Assumed, not measured.", "",
             "| Case | Effective tier | Human decisions | Policy authorizations | Waits | People asked |",
             "|---|---|---|---|---|---|"]
    for c in result["cases"]:
        lines.append(f"| {c['case']} | {c['effective_tier']} | {c['human_decisions']} | {c['policy_authorizations']} | "
                     f"{c['waits']} | {c['people_asked']} |")
    lines += ["", "| Median response | Sigma | Case | Lead time p50 (h) | p90 (h) | p50 reduction vs HIGH |",
              "|---|---|---|---|---|---|"]
    for r in result["rows"]:
        lines.append(f"| {r['median_minutes']} min | {r['sigma']} | {r['case']} | {r['lead_p50_hours']} | "
                     f"{r['lead_p90_hours']} | {r['reduction_vs_high_pct']}% |")
    (out / "latency_sensitivity.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / 'latency_sensitivity.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
