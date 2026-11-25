"""Baseline comparison: AIDLC-G against emulated conventional configurations on the same trials and slice.

    .venv/bin/python scripts/run_baselines.py [--no-slice]

Agents, repository permissions and the CI runner are held constant; only the transition evaluator
changes (see src/aidlc_g/baseline_study.py for each baseline's switched-off controls). Writes
results/baseline_comparison.{md,json}. Baselines emulate documented default capabilities; real tools
can be configured to enforce more.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.baseline_study import BASELINES, CONTROLS, run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-slice", action="store_true")
    parser.add_argument("--out", default="results")
    args = parser.parse_args()
    result = run(include_slice=not args.no_slice)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline_comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    b = result["baselines"]
    keys = list(BASELINES)
    lines = ["# Baseline comparison", "",
             "> Same agents, repository permissions and CI; only the transition evaluator differs. Baselines switch off "
             "the controls their documented default configuration lacks. Emulation, not the real tools.", "",
             "| Control | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys)]
    for c in CONTROLS:
        lines.append(f"| {c} | " + " | ".join("off" if c in b[k]["off"] else "on" for k in keys) + " |")
    lines += ["", "| Measure | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys),
              "| Trial violations stopped | " + " | ".join(f"{b[k]['trial_stopped']}/{b[k]['trial_violating']}" for k in keys) + " |",
              "| Valid runs falsely blocked | " + " | ".join(f"{b[k]['false_blocks']}/{b[k]['positives']}" for k in keys) + " |"]
    if "slice_stopped" in b[keys[0]]:
        lines += ["| Slice adversarial scenarios stopped | " + " | ".join(f"{b[k]['slice_stopped']}/{b[k]['slice_total']}" for k in keys) + " |",
                  "| Happy path released | " + " | ".join("yes" if b[k]["happy_path_released"] else "no" for k in keys) + " |"]
    lines += ["", "## Violations each baseline let through", ""]
    for k in keys:
        missed = [f"{t['trial']} {t['scenario']}" for t in b[k]["trials"] if t["violating"] and not t["stopped"]]
        missed += [s["scenario"] for s in b[k].get("slice", []) if s["violating"] and not s["stopped"]]
        lines.append(f"- **{k}** ({b[k]['label']}): " + ("; ".join(missed) if missed else "none"))
    (out / "baseline_comparison.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / 'baseline_comparison.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
