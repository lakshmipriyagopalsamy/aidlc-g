"""Bounded model checking of the abstract ECLT component models and their mutants.

    .venv/bin/python scripts/run_model_check.py

Writes results/model_check.{md,json}. Exhaustive within the bounds; takes about a second.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.model_check import run  # noqa: E402


def main() -> int:
    result = run()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
    out.mkdir(parents=True, exist_ok=True)
    (out / "model_check.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = ["# Bounded model checking", "", f"Bounds: {result['bounds']}", "",
             "| Model | Properties hold | Reachable states | Transitions |", "|---|---|---|---|"]
    for name, r in result["models"].items():
        lines.append(f"| {name} | {'yes' if r['holds'] else 'NO'} | {r['states']:,} | {r['transitions']:,} |")
    lines += ["", "| Mutant (one mechanism removed) | Counterexample found | Violation | Trace length |", "|---|---|---|---|"]
    for name, r in result["mutants"].items():
        lines.append(f"| {name} | {'yes' if r['caught'] else 'NO'} | {r['violation']} | {len(r['trace'])} |")
    (out / "model_check.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / 'model_check.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
