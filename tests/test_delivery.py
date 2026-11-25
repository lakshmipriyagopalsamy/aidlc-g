"""M3 end to end: coverage, quality evidence, review, docs and a gated merge to protected main.

Completion evidence (section 26 M3 / section 16 M3): a human-approved PR with
complete release evidence; the protected branch rejects missing evidence and
stale tests.
"""

import pytest

from aidlc_g.examples import notifications as ex
from aidlc_g.orchestrator import ScriptedDesk, SliceEnvironment, SliceOrchestrator
from aidlc_g.service import ALLOW
from aidlc_g.testkit import expect_error

BRANCH = "work/WI-NOTIFY-001"


def deliver(model=None, desk=None, env=None, patch=None):
    env = env or SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
    orchestrator = SliceOrchestrator(env, model or ex.model(), desk or ex.desk())
    if patch:
        patch(env, orchestrator)
    return env, orchestrator, orchestrator.deliver(ex.REQUEST)


def halted_codes(report):
    return [r["code"] for r in report.halted.get("reasons", [])] if report.halted else []


@pytest.fixture(scope="module")
def merged():
    return deliver()


# --- Happy path ---------------------------------------------------------------------


def test_change_is_merged_into_protected_main(merged):
    env, _, report = merged
    assert report.completed and report.halted is None
    assert report.merge["decision"] == ALLOW
    assert env.workspace.head == env.workspace.branch_head(BRANCH) == report.merged_revision
    main = set(env.workspace.list(env.tokens["merge-bot"], ""))
    assert {"src/notifications/__init__.py", "uat/test_critical_delivered.py",
            "tests/coverage/test_preferences.py", "docs/notifications/runbook.md"} <= main


def test_quality_evidence_was_measured_on_the_branch(merged):
    _, _, report = merged
    quality = [r for r in report.runs if r.kind == "QUALITY"][-1].payload
    checks = {c["kind"]: c for c in quality["checks"]}
    assert checks["COVERAGE_TESTS"]["status"] == "PASS"
    assert checks["MUTATION"]["metrics"]["mutants"] > 0
    assert checks["MUTATION"]["metrics"]["killed"] == checks["MUTATION"]["metrics"]["mutants"]
    assert checks["SECRET_SCAN"]["status"] == checks["DEPENDENCY_SCAN"]["status"] == "PASS"


def test_pr_packet_carries_complete_evidence(merged):
    _, _, report = merged
    p = report.pr_packet
    assert p["business_requirement"]["approvers"] == ["po (product_owner)"]
    assert p["technical_requirement_and_adr"]["adr"] == "ADR-NOTIFY"
    assert set(p["independent_checks"]["acceptance"].values()) == {"PASSED"}
    assert len(p["docs_updated"]) == 5
    assert p["human_review_outcome"] == {"reviewer": "reviewer", "decision": "APPROVE"}
    assert p["outstanding_risks"] and p["outstanding_risks"][0]["risk_owner"] == "eng_lead"


def test_reviewer_decided_from_a_packet_before_any_human_outcome(merged):
    _, orchestrator, _ = merged
    seen = orchestrator.desk.review_packets
    assert len(seen) == 1 and seen[0]["human_review_outcome"] == "PENDING"
    assert seen[0]["ai_review_findings"][0]["status"] == "OPEN"


def test_docs_were_approved_by_product_and_sre(merged):
    _, _, report = merged
    docs = [a for a in report.approvals if a["artifact"].startswith("DOCS-NOTIFY")]
    assert {a["role"] for a in docs} == {"product_owner", "sre"}


def test_all_eight_agents_left_traces(merged):
    _, _, report = merged
    assert {t.role for t in report.traces} == {
        "requirements_agent", "architecture_agent", "story_planning_agent", "acceptance_test_agent",
        "implementation_agent", "test_coverage_agent", "code_review_agent", "documentation_agent"}


def test_merge_decision_is_audited(merged):
    env, _, report = merged
    stored = env.gate.get_transition(env.admin, report.merge["transition_id"])
    assert stored["decision"] == ALLOW and stored["head_revision"] == report.merged_revision
    assert env.gate.verify_audit_chain()["ok"]


def test_report_renders(merged):
    md = merged[2].to_markdown()
    assert "merged into protected `main`" in md and "Template I" in md and "MUTATION" in md


# --- Quality failures -----------------------------------------------------------------


def test_leaked_secret_blocks_documentation_without_echoing_it():
    _, _, report = deliver(ex.model().with_override("implementation_agent", "implement", ex.leaked_secret_implementation))
    assert report.halted["stage"] == "DOCUMENTATION" and halted_codes(report) == ["QUALITY_CHECK_FAILED"]
    message = report.halted["reasons"][0]["message"]
    assert "assigned-credential" in message and "sk_live" not in message


def test_vulnerable_or_unpinned_dependency_blocks():
    _, _, report = deliver(ex.model().with_override("implementation_agent", "implement",
                                                    ex.vulnerable_dependency_implementation))
    assert halted_codes(report) == ["QUALITY_CHECK_FAILED"]
    assert "ADV-SYN-0001" in report.halted["reasons"][0]["message"]
    assert "unpinned" in report.halted["reasons"][0]["message"]


def test_untested_logic_fails_mutation_threshold():
    _, _, report = deliver(ex.model().with_override("implementation_agent", "implement", ex.untested_logic_implementation))
    assert halted_codes(report) == ["MUTATION_SCORE_LOW"]
    assert "src/notifications/__init__.py:" in report.halted["reasons"][0]["message"]


def test_coverage_agent_cannot_touch_locked_tests():
    _, _, report = deliver(ex.model().with_override("test_coverage_agent", "add_tests", ex.uat_rewriting_coverage))
    assert report.halted["error"] == "PATH_NOT_PERMITTED"


# --- Review -----------------------------------------------------------------------------


def test_unresolved_blocking_finding_stops_the_pr():
    _, _, report = deliver(ex.model().with_override("code_review_agent", "review", ex.blocking_review))
    assert report.halted["error"] == "BLOCKING_FINDINGS_OPEN"


def test_human_can_resolve_a_blocking_finding():
    desk = ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, resolutions={"F-9": {"status": "RESOLVED"}})
    _, _, report = deliver(ex.model().with_override("code_review_agent", "review", ex.blocking_review), desk=desk)
    assert report.completed


def test_reviewer_can_request_changes():
    _, _, report = deliver(desk=ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS,
                                             review_decision="REQUEST_CHANGES"))
    assert report.halted["stage"] == "DOCUMENTATION" and halted_codes(report) == ["REVIEW_CHANGES_REQUESTED"]


def test_sre_rejection_blocks_the_merge():
    desk = ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS, decisions={("sre", "DOCS-NOTIFY"): "REJECT"})
    env, _, report = deliver(desk=desk)
    assert report.halted["stage"] == "MERGE" and halted_codes(report) == ["ARTIFACT_RETURNED"]
    assert env.workspace.head != env.workspace.branch_head(BRANCH)


# --- Protected branch -------------------------------------------------------------------


def test_code_pushed_after_testing_is_a_stale_head():
    def push_late_fix(env, orchestrator):
        original = orchestrator.merge_service.merge

        def merge(pr, docs_ref, key):
            env.workspace.commit(env.tokens["impl_agent"], {"src/notifications/hotfix.py": b"X = 1\n"}, "late", BRANCH)
            return original(pr, docs_ref, key)

        orchestrator.merge_service.merge = merge

    env, _, report = deliver(patch=push_late_fix)
    assert report.halted["stage"] == "MERGE" and halted_codes(report) == ["STALE_HEAD"]
    assert "src/notifications/hotfix.py" in report.halted["reasons"][0]["message"]
    assert "src/notifications/__init__.py" not in env.workspace.list(env.tokens["merge-bot"], "src/")


def test_docs_edited_after_approval_are_refused():
    def edit_docs(env, orchestrator):
        original = orchestrator.merge_service.merge

        def merge(pr, docs_ref, key):
            env.workspace.commit(env.tokens["docs_agent"], {"docs/notifications/runbook.md": b"# edited\n"}, "edit", BRANCH)
            return original(pr, docs_ref, key)

        orchestrator.merge_service.merge = merge

    _, _, report = deliver(patch=edit_docs)
    assert report.halted["error"] == "DOCS_DIGEST_MISMATCH"


def test_nobody_commits_to_main_directly():
    env = SliceEnvironment.create(ex.SEED)
    for who in ("impl_agent", "qa", "docs_agent"):
        path = {"impl_agent": "src/x.py", "qa": "uat/x.py", "docs_agent": "docs/x.md"}[who]
        assert expect_error(lambda: env.workspace.commit(env.tokens[who], {path: b"x"}, "direct")) == "PROTECTED_BRANCH"


def test_workspace_merge_requires_allow_decision_once(merged):
    env, _, report = merged
    bot = env.tokens["merge-bot"]
    assert expect_error(lambda: env.workspace.merge(bot, BRANCH, report.merge["transition_id"])) == "REPLAY_DETECTED"
    assert expect_error(lambda: env.workspace.merge(env.tokens["orchestrator"], BRANCH,
                                                    report.merge["transition_id"])) == "ROLE_NOT_PERMITTED"


def test_blocked_decision_cannot_be_used_to_merge():
    desk = ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS, decisions={("sre", "DOCS-NOTIFY"): "REJECT"})
    env, _, report = deliver(desk=desk)
    code = expect_error(lambda: env.workspace.merge(env.tokens["merge-bot"], BRANCH, report.merge["transition_id"]))
    assert code == "MERGE_NOT_ALLOWED"
