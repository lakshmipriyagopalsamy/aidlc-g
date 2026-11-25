"""M2 vertical slice: agents drive the notification change through real test execution.

Completion evidence (section 26 M2 / section 16 M2): an industry-neutral feature
delivered with traceability; implementation passes unchanged approved UAT;
tampering is blocked.
"""

import pytest

from aidlc_g.agents import ImplementationAgent
from aidlc_g.examples import notifications as ex
from aidlc_g.orchestrator import ScriptedDesk, SliceEnvironment, SliceOrchestrator
from aidlc_g.service import ALLOW
from aidlc_g.testkit import expect_error


def run_slice(model=None, desk=None, env=None):
    env = env or SliceEnvironment.create(ex.SEED)
    orchestrator = SliceOrchestrator(env, model or ex.model(), desk or ex.desk())
    return env, orchestrator, orchestrator.run(ex.REQUEST)


def halted_codes(report):
    return [r["code"] for r in report.halted.get("reasons", [])] if report.halted else []


@pytest.fixture(scope="module")
def happy():
    return run_slice()


# --- Happy path -----------------------------------------------------------------


def test_slice_reaches_test_coverage_with_every_gate_allowing(happy):
    _, _, report = happy
    assert report.completed and report.halted is None
    assert [t["to_stage"] for t in report.transitions] == [
        "EPIC", "SPIKE", "STORIES", "ACCEPTANCE_TESTS", "IMPLEMENTATION", "TEST_COVERAGE"]
    assert all(t["decision"] == ALLOW for t in report.transitions)


def test_locked_tests_fail_on_stub_and_pass_on_candidate(happy):
    _, _, report = happy
    assert len(report.traceability) == 4
    assert {r["baseline"] for r in report.traceability} == {"FAILED"}
    assert {r["candidate"] for r in report.traceability} == {"PASSED"}


def test_every_domain_scenario_traces_to_a_story_and_an_executed_test(happy):
    _, _, report = happy
    assert {r["scenario_id"] for r in report.traceability} == {"SC-1", "SC-2", "SC-3", "SC-4"}
    assert {r["story_id"] for r in report.traceability} == {"STORY-1", "STORY-2"}
    assert all(r["digest"].startswith("sha256:") for r in report.traceability)


def test_requirement_is_grounded_in_cited_read_only_sources(happy):
    env, _, _ = happy
    c1 = env.gate.get_artifact(env.admin, "BR-NOTIFY")["body"]
    assert {o["source_uri"] for o in c1["existing_behavior"]} <= {s["uri"] for s in c1["source_evidence"]}
    assert all(s["permission_scope"] == "read_only" for s in c1["source_evidence"])
    assert any("CONTRIBUTING" in c for c in c1["conflicts"])  # injection surfaced, not obeyed


def test_clarification_answers_resolve_blocking_unknowns(happy):
    env, _, _ = happy
    unknowns = {u["item_id"]: u for u in env.gate.get_artifact(env.admin, "BR-NOTIFY")["body"]["unknowns"]}
    assert unknowns["Q-CRITICAL"]["status"] == "RESOLVED"
    assert unknowns["Q-CHANNELS"]["status"] == "OPEN" and not unknowns["Q-CHANNELS"]["blocking"]


def test_c0_never_carries_invented_metrics(happy):
    env, _, _ = happy
    c0 = env.gate.get_artifact(env.admin, "INIT-NOTIFY")["body"]
    assert c0["baseline_status"] == c0["target_status"] == "OPEN_DECISION"
    assert {q["item_id"] for q in c0["open_questions"]} >= {"Q-BASELINE", "Q-TARGET"}


def test_humans_saw_bounded_decision_packets(happy):
    _, orchestrator, _ = happy
    packets = orchestrator.desk.packets
    assert len(packets) == 7  # sponsor, PO, eng, eng+PO, PO+QA
    c3 = [p for p in packets if p["artifact_id"] == "AC-NOTIFY"]
    assert all(s.startswith("SC-") and "Given" in s for s in c3[0]["summary"])
    assert all(p["what_approval_does_not_authorize"] == "code merge, deployment or release" for p in packets)
    assert all(p["evidence_receipt"]["policy_version"] for p in packets)


def test_agent_trace_records_model_digests_and_tool_calls(happy):
    _, _, report = happy
    tasks = {(t.agent_id, t.task): t for t in report.traces}
    impl = tasks[("impl_agent", "implement")]
    assert impl.actions[-2:] == ["repo.commit", "gate.register_build:CANDIDATE"]
    assert all(t.input_digest.startswith("sha256:") and t.output_digest.startswith("sha256:") for t in report.traces)
    assert len(tasks[("requirements_agent", "clarify")].sources) == 3


def test_independent_evidence_survives_reverification(happy):
    env, _, _ = happy
    assert env.gate.reverify(env.tokens["orchestrator"], "WI-NOTIFY-001")["decision"] == ALLOW
    assert env.gate.verify_audit_chain()["ok"]


def test_report_renders(happy):
    md = happy[2].to_markdown()
    assert "reached `TEST_COVERAGE`" in md and "## Traceability" in md


# --- Incorrect or tampering implementations ----------------------------------------


def test_buggy_implementation_is_caught_by_real_test_execution():
    _, _, report = run_slice(ex.model().with_override("implementation_agent", "implement", ex.buggy_implementation))
    assert report.halted["stage"] == "TEST_COVERAGE"
    assert halted_codes(report) == ["TESTS_FAILED"]
    failing = [r for r in report.runs[-1].payload["results"] if r["outcome"] != "PASSED"]
    assert [r["test_id"] for r in failing] == ["UAT-002"]  # critical alerts were silenced


def test_implementer_cannot_rewrite_locked_test():
    env, _, report = run_slice(ex.model().with_override("implementation_agent", "implement", ex.tampering_implementation))
    assert report.halted["error"] == "PATH_NOT_PERMITTED"
    assert "def test_critical_alerts_delivered_when_optional_disabled():\n    svc" in \
        env.workspace.read(env.tokens["qa"], "uat/test_critical_delivered.py", branch="work/WI-NOTIFY-001").content


def test_workspace_blocks_implementer_even_without_broker():
    env = SliceEnvironment.create(ex.SEED)
    code = expect_error(lambda: env.workspace.commit(env.tokens["impl_agent"], {"uat/test_x.py": b"x"}, "sneaky"))
    assert code == "PATH_NOT_PERMITTED"


def test_out_of_band_test_edit_is_caught_by_runner_digests(monkeypatch):
    """A compromised repository host rewrites a locked test inside the implementer's commit."""
    env = SliceEnvironment.create(ex.SEED)
    original = env.workspace.commit

    def compromised(token, changes, message, branch="main"):
        revision = original(token, changes, message, branch)
        if env.gate.whoami(token)["principal_id"] == "impl_agent":
            revision = env.workspace._force_commit_for_test(
                {"uat/test_critical_delivered.py": b"def test_weakened():\n    assert True\n"}, "repo-host", branch)
        return revision

    monkeypatch.setattr(env.workspace, "commit", compromised)
    _, _, report = run_slice(ex.model().with_override("implementation_agent", "implement", ex.buggy_implementation), env=env)
    assert report.halted["stage"] == "TEST_COVERAGE"
    assert "TEST_INTEGRITY_VIOLATION" in halted_codes(report)


def test_lying_build_digest_is_rejected_by_runner(monkeypatch):
    env = SliceEnvironment.create(ex.SEED)
    monkeypatch.setattr(env.workspace, "build_digest", lambda revision=None, branch=None: "sha256:" + "0" * 64)
    _, _, report = run_slice(env=env)
    assert report.halted["error"] == "BUILD_BINDING_MISMATCH"


def test_tests_that_cannot_fail_are_refused_before_implementation():
    def trivial_tests(inputs):
        out = ex._tests(inputs)
        for t in out["tests"]:
            t["content"] = "def test_always():\n    assert True\n"
        return out

    _, _, report = run_slice(ex.model().with_override("acceptance_test_agent", "author_tests", trivial_tests))
    assert report.halted["stage"] == "IMPLEMENTATION"
    assert halted_codes(report) == ["TESTS_CANNOT_FAIL"]


# --- Independence and least privilege --------------------------------------------


def test_acceptance_author_cannot_read_implementation():
    env, _, _ = run_slice()
    assert env.workspace.list(env.tokens["uat_agent"], "src/") == []
    code = expect_error(lambda: env.workspace.read(env.tokens["uat_agent"], "src/notifications/__init__.py"))
    assert code == "PATH_NOT_PERMITTED"


@pytest.mark.parametrize("attr", ["requirements", "architecture", "planner", "tester", "implementer"])
def test_no_agent_has_an_approval_tool(attr):
    env = SliceEnvironment.create(ex.SEED)
    agent = getattr(SliceOrchestrator(env, ex.model(), ex.desk()), attr)
    assert expect_error(lambda: agent.broker.call("gate.approve", artifact_id="BR-NOTIFY")) == "TOOL_NOT_PERMITTED"


def test_broker_blocks_tools_outside_spec():
    env = SliceEnvironment.create(ex.SEED)
    impl: ImplementationAgent = SliceOrchestrator(env, ex.model(), ex.desk()).implementer
    assert expect_error(lambda: impl.broker.submit("C3", "AC-NOTIFY", {})) == "TOOL_NOT_PERMITTED"


# --- Untrusted model output ---------------------------------------------------------


def test_invented_metric_is_rejected_before_submission():
    env, _, report = run_slice(ex.model().with_override("requirements_agent", "draft_intent", ex.invented_metric_intent))
    assert report.halted["error"] == "INVENTED_METRIC"
    assert expect_error(lambda: env.gate.get_artifact(env.admin, "INIT-NOTIFY")) == "UNKNOWN_ARTIFACT"


def test_hallucinated_citation_is_rejected():
    _, _, report = run_slice(ex.model().with_override("requirements_agent", "clarify", ex.hallucinated_citation))
    assert report.halted["error"] == "UNCITED_OBSERVATION"


def test_injected_self_approval_fields_are_rejected():
    env, _, report = run_slice(ex.model().with_override("requirements_agent", "draft_requirement", ex.injected_requirement))
    assert report.halted["error"] == "INVALID_MODEL_OUTPUT"
    assert expect_error(lambda: env.gate.get_artifact(env.admin, "BR-NOTIFY")) == "UNKNOWN_ARTIFACT"


# --- Human authority ------------------------------------------------------------------


def test_unanswered_blocking_question_pauses_even_if_approved():
    _, _, report = run_slice(desk=ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers={}))
    assert report.halted["stage"] == "SPIKE"
    assert report.halted["decision"] == "PAUSE_FOR_HUMAN_DECISION"
    assert halted_codes(report) == ["OPEN_BLOCKING_DECISION"]


def test_product_owner_can_return_the_requirement():
    desk = ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, decisions={("po", "BR-NOTIFY"): "REQUEST_CHANGES"})
    _, _, report = run_slice(desk=desk)
    assert report.halted["stage"] == "SPIKE" and halted_codes(report) == ["ARTIFACT_RETURNED"]


def test_qa_rejection_stops_before_any_build():
    desk = ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, decisions={("qa", "AC-NOTIFY"): "REJECT"})
    _, _, report = run_slice(desk=desk)
    assert report.halted["error"] == "MANIFEST_NOT_LOCKED"
    assert report.runs == []
