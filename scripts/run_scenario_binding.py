"""Scenario-binding study: which changes between the owner's scenarios and the tests the gate catches.

    .venv/bin/python scripts/run_scenario_binding.py

Writes results/scenario_binding.{md,json}. Deterministic; runs on the testkit fixture in seconds.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.binding_study import run  # noqa: E402


def main() -> int:
    result = run()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
    out.mkdir(parents=True, exist_ok=True)
    (out / "scenario_binding.json").write_text(json.dumps(result, indent=2) + "\n")
    s = result["summary"]
    lines = ["# Scenario-binding study", "",
             f"- Semantic changes caught: {s['semantic_caught']}/{s['semantic_total']}",
             f"- Presentation-only changes also blocked: {s['presentation_caught']}/{s['presentation_total']}",
             f"- Structural and ownership cases stopped as expected: {s['structural_as_expected']}/{s['structural_expected_total']}",
             f"- Not detected by the gate: {', '.join(s['undetected']) or 'none'}", "",
             "| Family | Change | Field | Caught (of 4 scenarios) |", "|---|---|---|---|"]
    seen = []
    for r in result["text_cases"]:
        key = (r["family"], r["perturbation"], r["field"])
        if key not in seen:
            seen.append(key)
    for fam, name, field in seen:
        rows = [r for r in result["text_cases"] if (r["family"], r["perturbation"], r["field"]) == (fam, name, field)]
        lines.append(f"| {fam} | {name} | {field} | {sum(r['caught'] for r in rows)}/{len(rows)} |")
    lines += ["", "| Structural case | Gate decision | Reason codes |", "|---|---|---|"]
    for r in result["structural_cases"]:
        lines.append(f"| {r['case']} | {r['decision']} | {', '.join(r['codes']) or '—'} |")
    (out / "scenario_binding.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / 'scenario_binding.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
