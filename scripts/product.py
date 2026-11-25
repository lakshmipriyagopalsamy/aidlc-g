"""Build a product with AIDLC-G: you decide at the keyboard, agents do the work, the gate checks both.

    # try it on the built-in example (free, scripted agents, you make every human decision)
    .venv/bin/python scripts/product.py run --example
    .venv/bin/python scripts/product.py run --example java     # same change on a Java/Spring stack

    # see the stacks a team can choose
    .venv/bin/python scripts/product.py stacks

    # start a pack for your own product, fill in product.json, instructions/ and repo/, then run
    .venv/bin/python scripts/product.py init products/invoices --key INVOICE --stack java-spring-maven
    .venv/bin/python scripts/product.py run products/invoices --claude

The run stops for you at each human step: clarifying questions, the domain
scenarios (Given/When/Then), the technology stack (as engineering lead), each
approval, and the pull-request review. It ends
released, or halted with the gate's reasons. Results go to results/<key>_delivery.md
and results/<key>_decisions.json.

--claude spends API credits and executes model-written code in a temp directory.
Everything after the merge (environments, telemetry) is simulated.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.desk import DeskClosed, TerminalDesk  # noqa: E402
from aidlc_g.orchestrator import SliceEnvironment, SliceOrchestrator  # noqa: E402
from aidlc_g.product import environment_for, load_pack, scaffold  # noqa: E402
from aidlc_g.stacks import BUILT_IN, unsupported_checks  # noqa: E402

EXAMPLE_NOTE = """You are running the built-in notifications example ({stack}) with scripted agents.
The scripted agents only know the example's four scenarios (SC-1..SC-4). Keep or reword them
and it can release; add or drop one and watch the gate stop the work, which is the point."""


def cmd_init(args: argparse.Namespace) -> int:
    try:
        directory = scaffold(Path(args.directory), args.key, args.stack)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"created {directory}/product.json and {directory}/repo/")
    print("next: replace every TODO in product.json, write your team's conventions in instructions/,")
    print("put the existing code in repo/, then run:")
    print(f"  .venv/bin/python scripts/product.py run {directory} --claude")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    engineering = None
    if args.example:
        from aidlc_g.examples import notifications as ex

        key, name, request, work_item = "NOTIFY", "Notification preferences (example)", ex.REQUEST, "WI-NOTIFY-001"
        if args.example == "java":
            from aidlc_g.examples import notifications_jvm as jvm

            engineering = jvm.engineering()
            env = SliceEnvironment.create(jvm.SEED, ex.ADVISORIES, stack=jvm.JAVA_SPRING_SIMULATED)
            model, key = jvm.model(), "NOTIFYJAVA"
            print(EXAMPLE_NOTE.format(stack="Java / Spring, simulated build"))
        else:
            env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
            model = ex.model()
            print(EXAMPLE_NOTE.format(stack="Python"))
    else:
        if not args.directory:
            print("error: give a product directory, or --example", file=sys.stderr)
            return 2
        try:
            pack = load_pack(Path(args.directory))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not args.claude:
            print("error: a product of your own needs live agents; add --claude (spends API credits)", file=sys.stderr)
            return 2
        from aidlc_g.claude_model import ClaudeModel

        key, name, request, work_item = pack.key, pack.name, pack.request, pack.work_item_id
        engineering = pack.engineering
        env = environment_for(pack)
        model = ClaudeModel(model=args.model) if args.model else ClaudeModel()
        print(f"{name}: {len(pack.seed)} files from {pack.source}; stack {pack.engineering.stack.stack_id}; "
              f"{len(pack.engineering.instructions)} instruction file(s)")

    print(f"\nChange request: {request.change_request}")
    print("You will act as every human role in turn. Ctrl-D or Ctrl-C stops; nothing is decided for you.")
    desk = TerminalDesk()
    try:
        report = SliceOrchestrator(env, model, desk, work_item, engineering).release(request)
    except DeskClosed:
        print("\nStopped. Nothing after this point was approved.")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{key.lower()}_delivery.md").write_text(report.to_markdown())
    (out / f"{key.lower()}_decisions.json").write_text(json.dumps(desk.log, indent=2) + "\n")
    print("")
    if report.completed:
        print(f"RELEASED {report.release_manifest['tag']} through "
              + " -> ".join(d["environment"] for d in report.deployments))
    else:
        halted = report.halted or {}
        reasons = [f"{r['code']}: {r['message']}" for r in halted.get("reasons", [])]
        print(f"HALTED at {halted.get('stage')}: {halted.get('decision') or halted.get('error') or halted.get('outcome')}")
        for line in reasons or [halted.get("message", "")]:
            print(f"  - {line}")
    print(f"report: {out / (key.lower() + '_delivery.md')}; your decisions: {out / (key.lower() + '_decisions.json')}")
    return 0 if report.completed else 1


def cmd_stacks(_: argparse.Namespace) -> int:
    print("Built-in stacks (put one in product.json as \"stack\", or give a full profile object):\n")
    for sid, p in BUILT_IN.items():
        gaps = ", ".join(unsupported_checks(p)) or "none"
        print(f"  {sid:24} {p.framework}, {p.build_tool} ({p.language}); runner cannot do: {gaps}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a product pack to fill in")
    init.add_argument("directory")
    init.add_argument("--key", default="MYPRODUCT", help="short id for artifact ids, e.g. INVOICE")
    init.add_argument("--stack", default="python-pytest", choices=sorted(BUILT_IN), help="the team's technology stack")
    sub.add_parser("stacks", help="list the technology stacks a team can choose")
    run = sub.add_parser("run", help="build the product, stopping for your decisions")
    run.add_argument("directory", nargs="?")
    run.add_argument("--example", nargs="?", const="python", choices=["python", "java"],
                     help="use the built-in notifications example (scripted, free); 'java' for the Java/Spring stack")
    run.add_argument("--claude", action="store_true", help="use live Claude agents (spends API credits)")
    run.add_argument("--model", help="Claude model id for the agents")
    run.add_argument("--out", default="results")
    args = parser.parse_args()
    return {"init": cmd_init, "stacks": cmd_stacks, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
