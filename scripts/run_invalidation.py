"""Dependency-aware invalidation study (RQ4): targeted invalidation and concurrent change.

    .venv/bin/python scripts/run_invalidation.py [--races 40]

Writes results/invalidation_study.{md,json}. Deterministic except for thread scheduling in the race
part, where the safety property (no admission on invalidated evidence) must hold in every run.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.invalidation_study import run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--races", type=int, default=40)
    parser.add_argument("--out", default="results")
    args = parser.parse_args()
    result = run(args.races)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "invalidation_study.json").write_text(json.dumps(result, indent=2) + "\n")
    s, c = result["summary"], result["concurrency"]
    lines = ["# Dependency-aware invalidation study", "",
             f"- Exact invalidation (no missed, no extra): {s['exact']}/{s['cases']} changes",
             f"- Stale approvals still counting after the change: {s['stale_aidlcg']} (AIDLC-G) vs "
             f"{s['stale_no_invalidation']} (no invalidation)",
             f"- Human re-approvals implied: {s['reapprovals_targeted']} (targeted) vs {s['reapprovals_restart_all']} (restart all)",
             f"- Races between a design change and a transition: {c['runs']} runs, transition first {c['transition_first']}, "
             f"change first {c['change_first']}; admitted on invalidated evidence {c['violations']}; "
             f"stage re-admitted afterwards {c['readmitted']}", "",
             "| Changed | Invalidated (gate) | Missed | Extra | Stale approvals: AIDLC-G / none | Re-approvals: targeted / restart | Rebuild |",
             "|---|---|---|---|---|---|---|"]
    for r in result["targeted"]:
        lines.append(f"| {r['changed']} | {', '.join(r['invalidated'])} | {', '.join(r['missed']) or '—'} | "
                     f"{', '.join(r['extra']) or '—'} | {r['stale_approvals_aidlcg']} / {r['stale_approvals_no_invalidation']} | "
                     f"{r['reapprovals_targeted']} / {r['reapprovals_restart_all']} | {'yes' if r['rebuild_needed'] else 'no'} |")
    (out / "invalidation_study.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / 'invalidation_study.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
