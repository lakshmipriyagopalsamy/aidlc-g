"""M5 operational support: read-only retrieval, grounded packets, human-only incident authority."""

import sqlite3

import pytest

from aidlc_g.agents import parse_model_output
from aidlc_g.incident_study import incident_drill, released_environment
from aidlc_g.ops import OPS_TOOLS, OperationalSupportAgent, OpsStore, ops_broker, simulate
from aidlc_g.service import BLOCK
from aidlc_g.testkit import expect_error
from aidlc_g.triage import ReferenceTriageModel


@pytest.fixture(scope="module")
def world():
    return released_environment()


class Tweaked:
    """Reference reasoner with a post-edit, to simulate a misbehaving model."""

    model_id = "tweaked"

    def __init__(self, edit):
        self._edit = edit

    def generate(self, role, task, inputs, output_model):
        out = ReferenceTriageModel().triage(inputs)
        self._edit(out)
        return parse_model_output(output_model, out)


_counter = iter(range(10_000))


def decision(env, incident_id, **fields):
    """A responder decision bound to the latest packet the responder was shown."""
    shown = [e for e in env.gate.get_incident(env.tokens["sre"], incident_id)["events"] if e["event"] == "PACKET"][-1]
    return {"packet_digest": shown["payload_digest"], "outcome": "ISSUE_UNDERSTOOD", "hypothesis_id": "H1",
            "action": "NONE", "rationale": "x", **fields}


def triage(world, label="RELEASE_REGRESSION", model=None, tools=OPS_TOOLS, seed=1):
    env, release = world
    scenario = simulate(label, seed, release, env.baseline_digest)
    store = OpsStore(env.gate, scenario)
    agent = OperationalSupportAgent(model or ReferenceTriageModel(),
                                    ops_broker(tools, "ops_agent", env.tokens["ops_agent"], env.gate, env.workspace, store))
    incident_id = f"INC-T-{next(_counter)}"
    return agent, incident_id, (lambda: agent.triage(incident_id))


# --- Simulator ------------------------------------------------------------------------


def test_simulation_is_reproducible(world):
    env, release = world
    a = simulate("PROVIDER_OUTAGE", 5, release, env.baseline_digest)
    b = simulate("PROVIDER_OUTAGE", 5, release, env.baseline_digest)
    assert (a.metrics, a.logs, a.deployments) == (b.metrics, b.logs, b.deployments)
    assert simulate("PROVIDER_OUTAGE", 6, release, env.baseline_digest).metrics != a.metrics


def test_red_herring_has_a_fresh_deploy_but_a_provider_cause(world):
    env, release = world
    s = simulate("RED_HERRING", 3, release, env.baseline_digest)
    onset = s.alert["fired_at_minute"] - 3
    assert s.truth.cause_class == "PROVIDER_OUTAGE" and s.truth.subject == "mailcorp"
    assert onset - s.deployments[-1]["minute"] == 1


# --- Read-only connector ------------------------------------------------------------------


def test_store_is_limited_to_operations_roles(world):
    env, release = world
    store = OpsStore(env.gate, simulate("CAPACITY", 1, release, env.baseline_digest))
    assert expect_error(lambda: store.alert(env.tokens["impl_agent"])) == "ROLE_NOT_PERMITTED"
    assert store.alert(env.tokens["sre"])["evidence_id"].startswith("alert:")


def test_store_has_no_write_surface():
    public = [n for n in dir(OpsStore) if not n.startswith("_")]
    assert sorted(public) == ["OWNERS", "alert", "config_changes", "conversations", "deployments", "incidents", "logs",
                              "metrics", "owner"]


# --- Packets ------------------------------------------------------------------------------


def test_packet_is_filed_and_links_release_evidence(world):
    env, _ = world
    _, incident_id, run = triage(world)
    packet = run()
    stored = env.gate.get_incident(env.tokens["sre"], incident_id)["events"][0]
    assert stored["event"] == "PACKET" and stored["recorded_by"] == "ops_agent"
    change = next(c for c in packet["recent_changes"] if c["change_id"] == "REL-NOTIFY-1")
    assert change["linked_evidence"]["requirement"] == "BR-NOTIFY@v1"
    assert change["linked_evidence"]["acceptance"] == "AC-NOTIFY@v1"
    assert packet["escalation_owner"] == "sre"
    assert all("authorized human" in a for a in packet["restricted_actions"])


def test_red_herring_release_is_not_blamed(world):
    _, _, run = triage(world, "RED_HERRING")
    hyps = sorted(run()["working_hypotheses"], key=lambda h: h["rank"])
    assert hyps[0]["cause_class"] == "PROVIDER_OUTAGE"
    release = next(h for h in hyps if h["cause_class"] == "RELEASE_REGRESSION")
    assert release["contradicting"] and release["confidence"] != "HIGH"


def test_uncited_evidence_is_rejected(world):
    model = Tweaked(lambda o: o["working_hypotheses"][0]["supporting"].append("log:L-9999"))
    _, _, run = triage(world, model=model)
    assert expect_error(run) == "UNCITED_EVIDENCE"


def test_root_cause_claims_are_rejected(world):
    model = Tweaked(lambda o: o["working_hypotheses"][0].update(statement="This is definitely the root cause"))
    _, _, run = triage(world, model=model)
    assert expect_error(run) == "OVERCONFIDENT_CLAIM"


def test_high_confidence_needs_independent_evidence(world):
    def edit(o):
        h = o["working_hypotheses"][0]
        h["supporting"], h["confidence"] = [h["supporting"][0]], "HIGH"

    _, _, run = triage(world, model=Tweaked(edit))
    assert expect_error(run) == "CONFIDENCE_NOT_SUPPORTED"


def test_remediation_steps_are_rejected(world):
    model = Tweaked(lambda o: o["first_diagnostic_steps"].insert(0, {
        "step_id": "S0", "action": "rollback", "target": "production", "rationale": "fastest fix"}))
    _, _, run = triage(world, model=model)
    assert expect_error(run) == "INVALID_MODEL_OUTPUT"


def test_missing_sources_become_unknowns(world):
    _, _, run = triage(world, tools=OPS_TOOLS - {"ops.deployments", "gate.release", "repo.read"})
    packet = run()
    assert "deployment history unavailable to this agent" in packet["unknowns"]
    assert "runbooks unavailable to this agent" in packet["unknowns"]
    assert packet["first_diagnostic_steps"][0]["action"] == "search_logs"


# --- Authority stays with humans ------------------------------------------------------------


def test_agent_cannot_decide_resolve_or_roll_back(world):
    env, _ = world
    agent, incident_id, run = triage(world)
    packet = run()
    ops = env.tokens["ops_agent"]
    decision_ = decision(env, incident_id, outcome="NEEDS_DEEPER_ACTION", action="ROLLBACK")
    assert expect_error(lambda: env.gate.record_incident(ops, incident_id, "DECISION", decision_)) == "HUMAN_APPROVAL_REQUIRED"
    env.gate.record_incident(env.tokens["sre"], incident_id, "DECISION", decision_)
    rb = env.gate.evaluate_incident_rollback(ops, incident_id, "production", f"{incident_id}:agent-rb")
    assert rb["decision"] == BLOCK and rb["reasons"][0]["code"] == "ACTOR_NOT_AUTHORIZED"
    for tool in ("deploy", "gate.approve", "ops.restart"):
        assert expect_error(lambda: agent.broker.call(tool)) == "TOOL_NOT_PERMITTED"
    assert packet["incident_id"] == incident_id


def test_incident_steps_are_ordered_and_append_only(world):
    env, _ = world
    sre = env.tokens["sre"]
    early = {"packet_digest": "sha256:" + "0" * 64, "outcome": "ISSUE_UNDERSTOOD", "hypothesis_id": "H1",
             "action": "NONE", "rationale": "x"}
    assert expect_error(lambda: env.gate.record_incident(sre, "INC-ORDER-1", "DECISION", early)) == "OUT_OF_ORDER"
    _, incident_id, run = triage(world)
    run()
    early = decision(env, incident_id)
    assert expect_error(lambda: env.gate.record_incident(sre, incident_id, "DECISION",
                                                         dict(early, hypothesis_id="H9"))) == "UNKNOWN_HYPOTHESIS"
    env.gate.record_incident(sre, incident_id, "DECISION", early)
    assert expect_error(lambda: env.gate.record_incident(sre, incident_id, "DECISION", early)) == "EVENT_ALREADY_RECORDED"
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.gate._db.execute("DELETE FROM incidents")


def test_rollback_needs_a_rollback_decision(world):
    env, _ = world
    _, incident_id, run = triage(world)
    run()
    env.gate.record_incident(env.tokens["sre"], incident_id, "DECISION", decision(env, incident_id))
    rb = env.gate.evaluate_incident_rollback(env.tokens["sre"], incident_id, "production", f"{incident_id}:rb")
    assert [r["code"] for r in rb["reasons"]] == ["MISSING_EVIDENCE"]


def test_drill_runs_the_full_incident_workflow():
    env, release = released_environment()
    before = env.gate.get_environment(env.admin, "production")["current_digest"]
    drill = incident_drill(env, release)
    events = drill["record"]["events"]
    assert [e["event"] for e in events] == ["PACKET", "DECISION", "RESOLVED", "POSTMORTEM"]
    assert [e["recorder_kind"] for e in events] == ["agent", "human", "human", "human"]
    assert before == release["artifact_digest"] and drill["production_now"] == env.baseline_digest
    assert events[3]["payload"]["linked_release"] == "REL-NOTIFY-1"
    code = expect_error(lambda: env.gate.record_deployment(env.tokens["deploy-bot"],
                                                           drill["rollback"].decision["transition_id"], "ROLLED_BACK"))
    assert code == "DEPLOYMENT_CLOSED"
    code = expect_error(lambda: env.gate.record_deployment(env.tokens["deploy-bot"],
                                                           drill["rollback"].decision["transition_id"], "PROMOTED"))
    assert code == "INVALID_EVENT"  # an incident rollback authorizes nothing else
    assert env.gate.verify_audit_chain()["ok"]
