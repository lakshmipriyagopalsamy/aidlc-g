"""M4 end to end: provenance, release record, staged canary deployment and rollback.

Completion evidence (section 26 M4 / section 16 M4): a governed deployable change
and rollback path; the release links the exact requirement, tests, commit,
artifact and documentation.
"""

import pytest

from aidlc_g.canonical import file_digest
from aidlc_g.examples import notifications as ex
from aidlc_g.orchestrator import ScriptedDesk, SliceEnvironment, SliceOrchestrator
from aidlc_g.registry import deterministic_tar
from aidlc_g.testkit import expect_error


def release(model=None, desk=None, telemetry=None, patch=None):
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES, telemetry)
    orchestrator = SliceOrchestrator(env, model or ex.model(), desk or ex.desk())
    if patch:
        patch(env, orchestrator)
    return env, orchestrator, orchestrator.release(ex.REQUEST)


@pytest.fixture(scope="module")
def shipped():
    return release()


# --- Happy path ---------------------------------------------------------------------


def test_release_reaches_production(shipped):
    env, _, report = shipped
    assert report.completed and report.final_stage == "RELEASED"
    assert [d["outcome"] for d in report.deployments] == ["PROMOTED", "PROMOTED"]
    artifact = report.release_manifest["artifact_digest"]
    assert env.deployer.running == {"staging": artifact, "production": artifact}


def test_artifact_is_exactly_the_merged_source(shipped):
    env, _, report = shipped
    m = report.release_manifest
    assert m["merged_revision"] == env.workspace.head
    tree = env.workspace.snapshot(env.tokens["ci-runner-1"], m["merged_revision"])
    expected = deterministic_tar({p: c for p, c in tree.items() if p.startswith("src/")})
    assert file_digest(expected) == m["artifact_digest"] == file_digest(env.registry.get(m["artifact_digest"]))


def test_release_links_requirement_tests_commit_artifact_docs(shipped):
    _, _, report = shipped
    m = report.release_manifest
    assert m["requirement_versions"]["C1"]["artifact_id"] == "BR-NOTIFY"
    assert m["acceptance_manifest"]["artifact_id"] == "AC-NOTIFY" and m["test_attestations"]
    assert m["source_revision"] != m["merged_revision"]  # tested commit vs merged (+docs) revision, both recorded
    assert m["documentation"]["runbook"]["uri"] == "repo://docs/notifications/runbook.md"
    assert m["provenance"]["builder_id"] == "ci-runner-1"


def test_every_deployment_had_a_rollback_target(shipped):
    env, _, report = shipped
    assert {d["rollback_target"] for d in report.deployments} == {env.baseline_digest}


def test_release_manager_saw_health_checks_and_rollback(shipped):
    _, orchestrator, _ = shipped
    packet = [p for p in orchestrator.desk.packets if p["artifact_id"] == "REL-NOTIFY-1"][0]
    assert any(s.startswith("Health: critical_alert_delivery_pct GTE 100") for s in packet["summary"])
    assert any(s.startswith("Rollback:") for s in packet["summary"])


def test_nine_agents_left_traces(shipped):
    _, _, report = shipped
    assert len({t.role for t in report.traces}) == 9


def test_audit_chain_holds_after_release(shipped):
    assert shipped[0].gate.verify_audit_chain()["ok"]


def test_report_renders(shipped):
    md = shipped[2].to_markdown()
    assert "released `notifications-v1.4.0`" in md and "Template J" in md and "## Deployments" in md


# --- Rollback -------------------------------------------------------------------------


def test_unhealthy_production_canary_rolls_back():
    env, _, report = release(telemetry=ex.degraded_in_production)
    assert report.halted["stage"] == "DEPLOY:production" and report.halted["outcome"] == "ROLLED_BACK"
    assert any("critical_alert_delivery_pct=92" in f for f in report.halted["failed_checks"])
    staging, production = report.deployments
    assert staging["outcome"] == "PROMOTED" and production["outcome"] == "ROLLED_BACK"
    assert env.deployer.running["production"] == env.baseline_digest
    assert env.gate.get_environment(env.admin, "production")["current_digest"] == env.baseline_digest


def test_missing_telemetry_never_counts_as_healthy():
    env, _, report = release(telemetry=ex.silent_telemetry)
    assert report.deployments[0]["outcome"] == "ROLLED_BACK"
    assert report.halted["failed_checks"] == ["critical_alert_delivery_pct: not reported"]


def test_corrupted_artifact_is_refused_before_deploying():
    def corrupt(env, orchestrator):
        original = env.deployer.deploy

        def deploy(work_item, rel, name, key):
            env.registry._corrupt_for_test(rel["body"]["artifact_digest"], b"swapped bytes")
            return original(work_item, rel, name, key)

        env.deployer.deploy = deploy

    env, _, report = release(patch=corrupt)
    assert report.deployments[0]["outcome"] == "REFUSED" and report.halted["error"] == "ARTIFACT_CORRUPT"
    assert env.deployer.running == {}


# --- Release record integrity ------------------------------------------------------------


def test_health_checks_must_be_declared_in_approved_docs():
    env, _, report = release(ex.model().with_override("release_evidence_agent", "draft_release", ex.ungrounded_release))
    assert report.halted["error"] == "UNGROUNDED_HEALTH_CHECK"
    assert expect_error(lambda: env.gate.get_artifact(env.admin, "REL-NOTIFY-1")) == "UNKNOWN_ARTIFACT"


def test_model_cannot_supply_artifact_digest():
    _, _, report = release(ex.model().with_override("release_evidence_agent", "draft_release", ex.digest_forging_release))
    assert report.halted["error"] == "INVALID_MODEL_OUTPUT"


def test_release_manager_can_refuse():
    desk = ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS, decisions={("rel_mgr", "REL-NOTIFY-1"): "REJECT"})
    env, _, report = release(desk=desk)
    assert report.halted["stage"] == "RELEASE_READY"
    assert [r["code"] for r in report.halted["reasons"]] == ["ARTIFACT_RETURNED"]
    assert env.deployer.history == []


def test_release_agent_has_no_deploy_or_approve_tool():
    env = SliceEnvironment.create(ex.SEED)
    agent = SliceOrchestrator(env, ex.model(), ex.desk()).release_agent
    for tool in ("gate.approve", "deploy", "gate.register_build:CANDIDATE"):
        assert expect_error(lambda: agent.broker.call(tool)) == "TOOL_NOT_PERMITTED"
