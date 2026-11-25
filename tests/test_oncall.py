"""On-call buddy: chat-first, read-only incident support with human-only, receipt-bound decisions."""

import itertools

import pytest

from aidlc_g.claude_model import render_inputs
from aidlc_g.incident_study import released_environment
from aidlc_g.lifecycle import Stage
from aidlc_g.oncall import ChatEvent, FakeChatChannel, IdentityDirectory, OnCallBuddy, simulated_investigator
from aidlc_g.ops import ONCALL_CHANNEL, OPS_TOOLS, REDACTED, OpsStore, simulate
from aidlc_g.service import BLOCK
from aidlc_g.testkit import World, expect_error

PRIVATE = "payments-incident-private"
_alerts = itertools.count(1)


@pytest.fixture(scope="module")
def world():
    return released_environment()


def buddy(world, label="RELEASE_REGRESSION", seed=1, scenario=None, tools=None):
    env, release = world
    scenario = scenario or simulate(label, seed, release, env.baseline_digest)
    scenario.alert = dict(scenario.alert, alert_id=f"A-{label}-{next(_alerts)}")  # one incident per test
    chat, directory = FakeChatChannel(), IdentityDirectory()
    directory.link("U-SRE", "sre", env.tokens["sre"], frozenset({ONCALL_CHANNEL}))
    directory.link("U-SRE-PAY", "sre", env.tokens["sre"], frozenset({ONCALL_CHANNEL, PRIVATE}))
    directory.link("U-PO", "po", env.tokens["po"], frozenset({ONCALL_CHANNEL}))
    bot = OnCallBuddy(env.gate, chat, directory,
                      simulated_investigator(env.gate, env.workspace, env.tokens["ops_agent"], scenario, tools=tools),
                      env.deployer)
    thread = bot.on_alert(ONCALL_CHANNEL, scenario.alert)
    return env, bot, chat, thread, scenario


def say(thread, text, user="U-SRE", verified=True, channel=ONCALL_CHANNEL, **kw):
    return ChatEvent(channel, thread, user, text, verified)


def receipt(reply):
    return reply.split("receipt ")[1].split()[0]


def incident_events(env, bot, thread):
    return [e["event"] for e in env.gate.get_incident(env.tokens["sre"], bot._threads[thread].incident_id)["events"]]


# --- The journey ---------------------------------------------------------------------


def test_alert_question_grounded_reply():
    world = released_environment()
    env, bot, chat, thread, _ = buddy(world)
    reply = bot.on_message(say(thread, "what is happening?"))
    posts = chat.thread(thread)
    assert posts[0].startswith(":rotating_light:") and posts[1].startswith("On it.")  # alert, acknowledgement
    for section in ("*Situation*", "*Likely cause", "*Release check*", "*Recent changes*", "*Next checks",
                    "*You decide*"):
        assert section in reply
    assert "H1 [HIGH] Degradation began" in reply and "REL-NOTIFY-1" in reply
    # Delivery evidence: the running artifact is the gated release, and the alert threatens SC-2.
    assert "MATCH: running artifact is the gated, tested release REL-NOTIFY-1" in reply
    assert 'At risk: SC-2 "critical alerts are never silenced" decided by po; locked tests UAT-002' in reply
    assert "NOT through a gated release" in reply  # the routine credential rotation is a config change
    assert incident_events(env, bot, thread) == ["PACKET"]


def test_engineer_decides_and_the_gate_rolls_back():
    world = released_environment()
    env, bot, _, thread, _ = buddy(world)
    reply = bot.on_message(say(thread, "what is happening?"))
    done = bot.on_message(say(thread, f"decide deeper H1 rollback receipt {receipt(reply)} errors match the release"))
    assert done.startswith("Recorded: NEEDS_DEEPER_ACTION by sre") and "Rollback ROLLED_BACK" in done
    assert env.gate.get_environment(env.admin, "production")["current_digest"] == env.baseline_digest
    decision = env.gate.get_incident(env.tokens["sre"], bot._threads[thread].incident_id)["events"][1]
    assert decision["recorded_by"] == "sre" and decision["recorder_kind"] == "human"
    assert decision["payload"]["outcome"] == "NEEDS_DEEPER_ACTION" and decision["payload"]["action"] == "ROLLBACK"


def test_noisy_alert_is_acknowledged_without_action(world):
    env, bot, _, thread, _ = buddy(world, "PROVIDER_OUTAGE")
    reply = bot.on_message(say(thread, "what is happening?"))
    done = bot.on_message(say(thread, f"decide noisy receipt {receipt(reply)}"))
    assert done.startswith("Recorded: FALSE_OR_NOISY") and "Rollback" not in done
    assert "re-investigate" in bot.on_message(say(thread, "and now?"))  # decided: no new packets


# --- Identity and authority --------------------------------------------------------


@pytest.mark.parametrize("user, verified", [("U-SRE", False), ("U-STRANGER", True)])
def test_only_verified_linked_users_are_served(world, user, verified):
    env, bot, _, thread, _ = buddy(world)
    reply = bot.on_message(say(thread, "what is happening?", user=user, verified=verified))
    assert "verified chat users linked" in reply
    assert expect_error(lambda: env.gate.get_incident(env.tokens["sre"], bot._threads[thread].incident_id)) == \
        "UNKNOWN_INCIDENT"


def test_a_linked_user_without_on_call_authority_cannot_decide(world):
    env, bot, _, thread, _ = buddy(world)
    reply = bot.on_message(say(thread, "what is happening?", user="U-PO"))  # anyone in scope may ask
    refused = bot.on_message(say(thread, f"decide deeper H1 rollback receipt {receipt(reply)}", user="U-PO"))
    assert "The gate refused the decision: ROLE_NOT_PERMITTED" in refused
    assert incident_events(env, bot, thread) == ["PACKET"]


def test_decision_must_name_the_latest_investigation(world):
    env, bot, _, thread, scenario = buddy(world)
    first = bot.on_message(say(thread, "what is happening?"), asked_at_minute=scenario.alert["fired_at_minute"])
    second = bot.on_message(say(thread, "still?"), asked_at_minute=scenario.alert["fired_at_minute"] + 10)
    assert receipt(first) != receipt(second)
    stale = bot.on_message(say(thread, f"decide understood H1 receipt {receipt(first)}"))
    assert "not the latest investigation" in stale and incident_events(env, bot, thread) == ["PACKET", "PACKET"]
    # The gate enforces it too, independently of the bot.
    events = env.gate.get_incident(env.tokens["sre"], bot._threads[thread].incident_id)["events"]
    old = {"packet_digest": events[0]["payload_digest"], "outcome": "ISSUE_UNDERSTOOD", "hypothesis_id": "H1",
           "action": "NONE", "rationale": "x"}
    assert expect_error(lambda: env.gate.record_incident(
        env.tokens["sre"], bot._threads[thread].incident_id, "DECISION", old)) == "STALE_PACKET"
    assert bot.on_message(say(thread, f"decide understood H1 receipt {receipt(second)}")).startswith("Recorded")


def test_a_noisy_alert_cannot_carry_a_production_action(world):
    _, bot, _, thread, _ = buddy(world)
    reply = bot.on_message(say(thread, "what is happening?"))
    refused = bot.on_message(say(thread, f"decide noisy H1 rollback receipt {receipt(reply)}"))
    assert "SCHEMA_VIOLATION" in refused


def test_the_bot_holds_no_production_capability():
    reads = {t for t in OPS_TOOLS if t.startswith("ops.") or t in ("gate.release", "gate.get", "repo.read")}
    assert OPS_TOOLS - reads == {"gate.incident:PACKET"}  # its only write: filing the investigation


# --- Evidence paths ----------------------------------------------------------------


def test_configuration_change_outside_a_gated_release_is_found(world):
    env, bot, _, thread, _ = buddy(world, "CONFIG_CHANGE")
    reply = bot.on_message(say(thread, "what is happening?"))
    packet = env.gate.get_incident(env.tokens["sre"], bot._threads[thread].incident_id)["events"][-1]["payload"]
    top = packet["working_hypotheses"][0]
    assert (top["cause_class"], top["subject"]) == ("CONFIG_CHANGE", "smtp.send_timeout_ms")
    assert packet["first_diagnostic_steps"][0]["action"] == "diff_configuration"
    change = next(c for c in packet["recent_changes"] if c["change_id"] == "CFG-3")
    assert change["kind"] == "CONFIGURATION" and change["through_gated_release"] is False
    assert "configuration CFG-3 at minute" in reply and "T-103" in reply  # history on the same setting


def test_conversation_history_is_scoped_to_what_the_asker_can_see(world):
    env, bot, _, thread, _ = buddy(world, "CONFIG_CHANGE")
    narrow = bot.on_message(say(thread, "what is happening?"))
    wide = bot.on_message(say(thread, "and with payments history?", user="U-SRE-PAY"))
    assert "T-201" not in narrow and "T-201" in wide


def test_credentials_in_configuration_never_reach_the_agent_or_the_thread(world):
    env, bot, chat, thread, scenario = buddy(world)
    store = OpsStore(env.gate, scenario)
    rotated = next(c for c in store.config_changes(env.tokens["ops_agent"], "production", 60) if c["key"] == "smtp.api_key")
    assert rotated["old"] == rotated["new"] == REDACTED
    bot.on_message(say(thread, "what is happening?"))
    assert not any("sk_live" in p["text"] for p in chat.posts)


def test_chat_text_is_data_not_instructions(world):
    env, bot, _, thread, scenario = buddy(world)
    reply = bot.on_message(say(thread, "what is happening?"))
    assert "T-104" not in reply and "roll back production now" not in reply  # not cited, not obeyed
    assert incident_events(env, bot, thread) == ["PACKET"]
    threads = OpsStore(env.gate, scenario).conversations(env.tokens["ops_agent"], frozenset({ONCALL_CHANNEL}), 60)
    prompt = render_inputs("triage", {"alert": scenario.alert, "conversations": threads})
    injected = prompt.index("roll back production now")
    assert prompt.rfind("<untrusted_source", 0, injected) > prompt.rfind("</untrusted_source>", 0, injected)


def test_evidence_age_is_reported_and_grows(world):
    env, bot, _, thread, scenario = buddy(world)
    fired = scenario.alert["fired_at_minute"]
    bot.on_message(say(thread, "now?"), asked_at_minute=fired)
    bot.on_message(say(thread, "and now?"), asked_at_minute=fired + 15)
    first, second = [e["payload"] for e in env.gate.get_incident(env.tokens["sre"], bot._threads[thread].incident_id)["events"]]
    common = sorted(set(first["evidence_age_minutes"]) & set(second["evidence_age_minutes"]) - {
        k for k in first["evidence_age_minutes"] if k.startswith("metric:")})
    assert common and all(second["evidence_age_minutes"][k] == first["evidence_age_minutes"][k] + 15 for k in common)
    assert second["investigated_at_minute"] == fired + 15


# --- Release check -----------------------------------------------------------------


def test_running_artifact_that_differs_from_the_gated_release_is_flagged(world):
    env, release = world
    scenario = simulate("PROVIDER_OUTAGE", 2, release, env.baseline_digest)
    scenario.deployments[-1]["artifact_digest"] = "sha256:" + "f" * 64
    _, bot, _, thread, _ = buddy(world, scenario=scenario)
    assert "MISMATCH: running artifact differs from the gated release REL-NOTIFY-1" in \
        bot.on_message(say(thread, "what is happening?"))


def test_a_hotfix_with_no_gated_release_is_flagged(world):
    env, release = world
    scenario = simulate("PROVIDER_OUTAGE", 3, release, env.baseline_digest)
    scenario.deployments.append({**scenario.deployments[-1], "deployment_id": "D-3", "minute": 25,
                                 "release_id": "HOTFIX-7", "tag": "notifications-v1.4.0-hotfix",
                                 "work_item_id": None})
    _, bot, _, thread, _ = buddy(world, scenario=scenario)
    assert "NOT_GATED: HOTFIX-7 (notifications-v1.4.0-hotfix) has no gated release record" in \
        bot.on_message(say(thread, "what is happening?"))


def test_without_delivery_evidence_the_release_check_says_so(world):
    _, bot, _, thread, _ = buddy(world, tools=OPS_TOOLS - {"gate.release"})
    assert "UNAVAILABLE: release evidence was not available" in bot.on_message(say(thread, "what is happening?"))


def test_health_checks_may_only_protect_decided_scenarios():
    w = World()
    w.prepare(Stage.RELEASE_READY)
    body = w.release_body(health_checks=[{"metric": "critical_alert_delivery_pct", "op": "GTE", "threshold": 100,
                                          "unit": "percent", "protects": ["SC-9"]}])
    view = w.submit("release_agent", "RELEASE", "REL-NOTIFY", body)
    w.approve("rel_mgr", view)
    w.refs["RELEASE"] = {"artifact_id": view["artifact_id"], "version": view["version"], "digest": view["digest"]}
    decision = w.transition(Stage.RELEASE_READY)
    assert decision["decision"] == BLOCK and "UNKNOWN_SCENARIO" in [r["code"] for r in decision["reasons"]]
