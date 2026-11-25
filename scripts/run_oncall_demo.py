"""On-call buddy demo: two simulated incidents handled in chat threads, written as a transcript.

    .venv/bin/python scripts/run_oncall_demo.py

Thread 1: a release regression. The engineer asks, reads the grounded reply and
decides to roll back; the gate authorizes it and production returns to the
previous artifact. Thread 2: a configuration change made outside any gated
release. The engineer asks twice (the second answer supersedes the first), tries
to decide on the stale one, is refused, then decides on the latest.

All data is synthetic. Chat, identity and the runtime platform are simulated.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g.incident_study import released_environment  # noqa: E402
from aidlc_g.oncall import ChatEvent, FakeChatChannel, IdentityDirectory, OnCallBuddy, simulated_investigator  # noqa: E402
from aidlc_g.ops import ONCALL_CHANNEL, simulate  # noqa: E402


def receipt(reply: str) -> str:
    return reply.split("receipt ")[1].split()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    lines = ["# On-call buddy demo", "",
             "> Synthetic data. Chat, identity and the runtime platform are simulated; the gate, the agent's "
             "evidence rules and the deployment service are the real implementation.", ""]
    for title, label, seed in (("Release regression", "RELEASE_REGRESSION", 7), ("Configuration change", "CONFIG_CHANGE", 4)):
        env, release = released_environment()  # each incident starts from the released state
        scenario = simulate(label, seed, release, env.baseline_digest)
        chat, directory = FakeChatChannel(), IdentityDirectory()
        directory.link("U-SRE", "sre", env.tokens["sre"], frozenset({ONCALL_CHANNEL}))
        bot = OnCallBuddy(env.gate, chat, directory,
                          simulated_investigator(env.gate, env.workspace, env.tokens["ops_agent"], scenario), env.deployer)
        thread = bot.on_alert(ONCALL_CHANNEL, scenario.alert)
        transcript = [("bot", chat.thread(thread)[0])]

        def ask(text: str, minute: object = None) -> str:
            before = len(chat.thread(thread))
            transcript.append(("sre", text))
            reply = bot.on_message(ChatEvent(ONCALL_CHANNEL, thread, "U-SRE", text, True), minute)
            transcript.extend(("bot", post) for post in chat.thread(thread)[before:])
            return reply

        fired = scenario.alert["fired_at_minute"]
        first = ask("what is happening?", fired)
        if label == "CONFIG_CHANGE":
            latest = ask("anything new?", fired + 10)
            ask(f"decide understood H1 receipt {receipt(first)}")
            ask(f"decide understood H1 receipt {receipt(latest)} revert smtp.send_timeout_ms via the change process")
        else:
            ask(f"decide deeper H1 rollback receipt {receipt(first)} errors match the new release")
        lines += [f"## {title}", ""]
        for who, text in transcript:
            lines += [f"**sre:** `{text}`", ""] if who == "sre" else ["**bot:**", "", "```", text, "```", ""]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "oncall_demo.md").write_text("\n".join(lines))
    print(f"wrote {out / 'oncall_demo.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
