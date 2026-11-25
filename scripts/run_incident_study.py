"""Run the M5 incident-support study and the end-to-end incident drill.

    .venv/bin/python scripts/run_incident_study.py --per-class 10
    .venv/bin/python scripts/run_incident_study.py --per-class 3 --claude   # live Claude reasoner (spends credits)

Writes results/incident_study.md and results/incident_study.json.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from aidlc_g.incident_study import incident_drill, released_environment, run_study, to_json, to_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-class", type=int, default=10)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--claude", action="store_true", help="use ClaudeModel for every condition")
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    started = time.perf_counter()
    env, release = released_environment()
    model = None
    if args.claude:
        from aidlc_g.claude_model import ClaudeModel

        model = ClaudeModel()
    result = run_study(args.per_class, args.seed, env=env, release=release, model=model)
    drill = incident_drill(env, release)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    markdown = to_markdown(result, drill)
    (out / "incident_study.md").write_text(markdown)
    (out / "incident_study.json").write_text(json.dumps(to_json(result), indent=2))
    print(markdown)
    print(f"wrote {out / 'incident_study.md'} and {out / 'incident_study.json'} in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
