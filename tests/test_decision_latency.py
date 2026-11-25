"""Risk tiers and decision latency: fewer approvals where evidence carries the decision, never less accountability."""

import sqlite3

import pytest

from aidlc_g.errors import GovernanceError
from aidlc_g.examples import notifications as ex
from aidlc_g.latency import SimClock, TimedDesk, comparison, run_at_tier
from aidlc_g.orchestrator import ScriptedDesk, SliceEnvironment, SliceOrchestrator

from test_product import keyboard


def human(report):
    return [a for a in report.approvals if a["principal"] != "policy"]


def policy(report):
    return [a for a in report.approvals if a["principal"] == "policy"]


def test_undeclared_work_keeps_every_approval():
    report = run_at_tier(None)
    assert report.completed
    assert len(human(report)) == 10 and not policy(report)
    assert report.latency["risk"] == {"declared": "HIGH", "effective": "HIGH",
                                      "signals": [{"tier": "MEDIUM", "code": "RISK_ACCEPTED",
                                                   "detail": "a reviewer accepted a known risk"}],
                                      "declarations": []}


def test_low_risk_clean_change_needs_only_the_decisions_evidence_cannot_make():
    report = run_at_tier("LOW", review_risk=False)
    assert report.completed, report.halted
    assert {a["artifact"].split("@")[0] for a in human(report)} == {"INIT-NOTIFY", "BR-NOTIFY", "AC-NOTIFY"}
    assert {(a["artifact"].split("@")[0], a["role"]) for a in policy(report)} == {
        ("TR-NOTIFY", "engineering_lead"), ("STORIES-NOTIFY", "engineering_lead"),
        ("STORIES-NOTIFY", "product_owner"), ("DOCS-NOTIFY", "product_owner"), ("DOCS-NOTIFY", "sre"),
        ("REL-NOTIFY-1", "release_manager")}
    manifest = report.release_manifest
    assert manifest["human_release_approvers"] == []
    assert manifest["policy_release_authorizations"] == [{"role": "release_manager", "rule": "tier:LOW:RELEASE"}]
    assert manifest["risk"]["tier"] == "LOW"
    assert "policy authorization under rule tier:LOW:RELEASE" in report.to_markdown()


def test_evidence_raises_the_tier_and_pulls_in_only_the_people_now_needed():
    report = run_at_tier("LOW")  # the reviewer risk-accepts F-1
    assert report.completed, report.halted
    assert report.latency["risk"]["effective"] == "MEDIUM"
    assert [(e["artifact"], e["role"]) for e in report.escalations] == [("TR-NOTIFY@v1", "engineering_lead")]
    paused = next(t for t in report.transitions if "RISK_TIER_RAISED" in t["reason_codes"])
    assert paused["to_stage"] == "DOCUMENTATION"
    assert {a["role"] for a in human(report) if a["artifact"].startswith("DOCS")} == {"sre"}
    assert {a["role"] for a in human(report) if a["artifact"].startswith("REL")} == {"release_manager"}


def test_security_relevant_design_forces_the_full_set_even_when_declared_low():
    report = run_at_tier("LOW", review_risk=False, threats=True)
    assert report.completed, report.halted
    assert report.latency["risk"]["effective"] == "HIGH"
    assert [s["code"] for s in report.latency["risk"]["signals"]] == ["SECURITY_RELEVANT_DESIGN"]
    assert [a for a in policy(report) if not a["artifact"].startswith("TR-")] == []
    assert ("TR-NOTIFY@v1", "engineering_lead") in [(e["artifact"], e["role"]) for e in report.escalations]


def test_only_named_people_may_declare_a_tier():
    env = SliceEnvironment.create(ex.SEED)
    env.gate.open_work_item(env.tokens["orchestrator"], "WI-X")
    for who, tier, why, code in (("requirements_agent", "LOW", "x", "HUMAN_APPROVAL_REQUIRED"),
                                 ("qa", "LOW", "x", "ROLE_NOT_PERMITTED"),
                                 ("eng_lead", "TRIVIAL", "x", "INVALID_RISK_TIER"),
                                 ("eng_lead", "LOW", " ", "RATIONALE_REQUIRED")):
        with pytest.raises(GovernanceError) as err:
            env.gate.declare_risk_tier(env.tokens[who], "WI-X", tier, why)
        assert err.value.code == code
    assert env.gate.declare_risk_tier(env.tokens["po"], "WI-X", "LOW", "copy change")["declared"] == "LOW"
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.gate._db.execute("UPDATE risk_declarations SET tier = 'LOW'")


def test_declaring_lower_after_the_evidence_does_not_lower_the_tier():
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
    model = ex.model().with_override("architecture_agent", "design",
                                     lambda i: {**ex._design(i), "threat_model": ["preference tampering"]})
    orch = SliceOrchestrator(env, model, ScriptedDesk(answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS,
                                                      scenarios=ex.DOMAIN_SCENARIOS))
    report = orch.run(ex.REQUEST)
    assert report.completed
    env.gate.declare_risk_tier(env.tokens["eng_lead"], "WI-NOTIFY-001", "LOW", "trying to skip approvals")
    needed = env.gate.required_approvers(env.tokens["orchestrator"], "WI-NOTIFY-001", "DOCS")
    assert needed["declared"] == "LOW" and needed["tier"] == "HIGH"
    assert needed["roles"] == ["product_owner", "sre"]


def test_latency_proxies_come_from_gate_records_on_a_simulated_clock():
    report = run_at_tier(None, minutes=120)
    L = report.latency
    assert L["approval_queue_minutes"]["critical_path"] == 840  # 7 artifact versions x 120 min, in parallel per version
    assert L["lead_time_minutes"] == 1080
    assert L["review_loops"] == 0 and L["escalations"] == 0 and L["rework_churn"] == 0
    assert L["over_target"] == []
    slow = run_at_tier(None, minutes=300).latency
    assert slow["over_target"] and all("waited" in line for line in slow["over_target"])


def test_returned_artifacts_count_as_review_loops_and_pauses_as_escalations():
    clock = SimClock()
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES, clock=clock)
    desk = ScriptedDesk(answers=ex.ANSWERS, scenarios=ex.DOMAIN_SCENARIOS,
                        decisions={("sponsor", "INIT-NOTIFY"): "REQUEST_CHANGES"})
    report = SliceOrchestrator(env, ex.model(), TimedDesk(desk, clock)).run(ex.REQUEST)
    assert not report.completed
    assert report.latency["review_loops"] == 1
    assert report.latency["escalations"] + report.latency["blocks"] >= 1


def test_comparison_orders_the_cases_by_how_much_people_must_do():
    rows = {r["case"]: r for r in comparison()}
    assert all(r["released"] for r in rows.values())
    assert rows["LOW, clean review"]["human_decisions"] < rows["MEDIUM"]["human_decisions"] \
        < rows["HIGH (default)"]["human_decisions"]
    assert rows["LOW, clean review"]["lead_time_minutes"] < rows["HIGH (default)"]["lead_time_minutes"]
    assert rows["LOW, design lists a threat"]["effective_tier"] == "HIGH"


def test_terminal_desk_declares_a_tier_with_a_reason_or_skips():
    proposal = {"tiers": {"LOW": {"C0": ["sponsor"], "C2": []}, "HIGH": {"C0": ["sponsor"], "C2": ["engineering_lead"]}},
                "default": "HIGH", "raised_by": ["RISK_ACCEPTED -> MEDIUM"]}
    kb = keyboard(["l", "copy-only change"])
    assert kb.desk().declare_risk("eng_lead", proposal) == {"tier": "LOW", "rationale": "copy-only change"}
    assert "policy approves C2" in kb.text
    assert keyboard(["s"]).desk().declare_risk("eng_lead", proposal) is None


def test_sensitivity_uses_real_schedules_and_common_random_numbers():
    from aidlc_g.latency import sensitivity
    r = sensitivity(samples=200)
    cases = {c["case"]: c for c in r["cases"]}
    assert cases["HIGH (default)"]["waits"] > cases["LOW, clean review"]["waits"]
    assert cases["LOW, design lists a threat"]["schedule"] == cases["HIGH (default)"]["schedule"] or \
        [s["people"] for s in cases["LOW, design lists a threat"]["schedule"]] == \
        [s["people"] for s in cases["HIGH (default)"]["schedule"]]
    for row in r["rows"]:
        if row["case"] == "LOW, clean review":
            assert row["reduction_vs_high_pct"] >= 40  # about half the waiting under every distribution
