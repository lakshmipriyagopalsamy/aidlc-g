"""Gate rules for Stage 7 quality evidence and protected-branch merge decisions (M3)."""

import pytest

from aidlc_g.canonical import file_digest
from aidlc_g.crypto import sign_payload
from aidlc_g.lifecycle import Stage
from aidlc_g.policy import Policy
from aidlc_g.service import ALLOW, BLOCK, PAUSE
from aidlc_g.testkit import World, expect_error, ref


def codes(decision):
    return [r["code"] for r in decision["reasons"]]


@pytest.fixture
def at_coverage():
    w = World()
    w.advance_to(Stage.TEST_COVERAGE)
    w.make_review()
    return w


def check(w, kind, **changes):
    payload = w.quality_attestation()
    payload["checks"] = [dict(c, **changes) if c["kind"] == kind else c for c in payload["checks"]]
    return payload


def ingest(w, payload):
    from aidlc_g.crypto import QUALITY_DOMAIN

    return w.svc.ingest_quality_attestation(payload, sign_payload(w.runner_key, payload, QUALITY_DOMAIN))


# --- Quality evidence -------------------------------------------------------------


def test_documentation_requires_quality_evidence(at_coverage):
    assert codes(at_coverage.transition(Stage.DOCUMENTATION)) == ["QUALITY_EVIDENCE_MISSING"]


def test_valid_quality_evidence_allows(at_coverage):
    at_coverage.make_quality()
    assert at_coverage.transition(Stage.DOCUMENTATION)["decision"] == ALLOW


def test_failed_scan_blocks(at_coverage):
    ingest(at_coverage, check(at_coverage, "SECRET_SCAN", status="FAIL", findings=["src/a.py:3 aws-access-key"]))
    d = at_coverage.transition(Stage.DOCUMENTATION)
    assert codes(d) == ["QUALITY_CHECK_FAILED"] and "aws-access-key" in d["reasons"][0]["message"]


@pytest.mark.parametrize("killed,ok", [(7, False), (8, True)])
def test_mutation_score_threshold_is_policy(at_coverage, killed, ok):
    ingest(at_coverage, check(at_coverage, "MUTATION", metrics={"mutants": 10, "killed": killed}))
    d = at_coverage.transition(Stage.DOCUMENTATION)
    assert (d["decision"] == ALLOW) is ok
    if not ok:
        assert codes(d) == ["MUTATION_SCORE_LOW"]


def test_gate_computes_score_itself(at_coverage):
    ingest(at_coverage, check(at_coverage, "MUTATION", metrics={"mutants": 10, "killed": 5, "score_pct": 100}))
    assert codes(at_coverage.transition(Stage.DOCUMENTATION)) == ["MUTATION_SCORE_LOW"]


def test_missing_check_kind_blocks(at_coverage):
    payload = at_coverage.quality_attestation()
    payload["checks"] = [c for c in payload["checks"] if c["kind"] != "DEPENDENCY_SCAN"]
    ingest(at_coverage, payload)
    assert codes(at_coverage.transition(Stage.DOCUMENTATION)) == ["QUALITY_EVIDENCE_MISSING"]


def test_quality_run_with_altered_uat_blocks(at_coverage):
    payload = at_coverage.quality_attestation()
    payload["uat_digests"][0]["test_digest"] = file_digest(b"weakened test")
    ingest(at_coverage, payload)
    assert codes(at_coverage.transition(Stage.DOCUMENTATION)) == ["TEST_INTEGRITY_VIOLATION"]


def test_signature_domains_are_separated(at_coverage):
    payload = at_coverage.quality_attestation()
    ci_signature = sign_payload(at_coverage.runner_key, payload)  # CI-attestation domain
    assert expect_error(lambda: at_coverage.svc.ingest_quality_attestation(payload, ci_signature)) == "INVALID_SIGNATURE"


def test_quality_must_build_identical_code(at_coverage):
    payload = at_coverage.quality_attestation(build_digest=file_digest(b"different code"))
    assert expect_error(lambda: ingest(at_coverage, payload)) == "BUILD_BINDING_MISMATCH"


def test_quality_replay_rejected(at_coverage):
    payload = at_coverage.quality_attestation()
    ingest(at_coverage, payload)
    assert expect_error(lambda: ingest(at_coverage, payload)) == "REPLAY_DETECTED"


def test_quality_requirement_is_a_policy_switch():
    w = World(policy=Policy(require_quality_evidence=False))
    w.advance_to(Stage.TEST_COVERAGE)
    w.make_review()
    assert w.transition(Stage.DOCUMENTATION)["decision"] == ALLOW


def test_new_build_invalidates_quality_evidence(at_coverage):
    at_coverage.make_quality()
    body = at_coverage.c3_body()
    body["tests"][0] = dict(body["tests"][0], content_digest="sha256:" + "1" * 64)
    view = at_coverage.submit("uat_agent", "C3", "AC-NOTIFY", body)
    assert "build:BLD-1" in view["invalidated"]
    assert expect_error(at_coverage.make_quality) == "EVIDENCE_INVALIDATED"


# --- Merge decisions ----------------------------------------------------------------


@pytest.fixture
def mergeable():
    w = World()
    w.advance_to(Stage.DOCUMENTATION)
    w.make_docs()
    return w


def merge(w, who="merge-bot", changed=(), tested=None, refs=None, key=None, head="f" * 40):
    tested = tested or w.builds["BLD-1"]["source_revision"]
    return w.svc.evaluate_merge(w.tok(who), w.work_item, "work/x", head, tested, list(changed),
                                {"DOCS": w.refs["DOCS"]} if refs is None else refs, key or w.next_id("MRG"))


def test_merge_allowed_with_complete_evidence(mergeable):
    d = merge(mergeable, changed=["docs/notifications/user.md"])
    assert d["decision"] == ALLOW and d["to_stage"] == "MERGE"
    assert mergeable.svc.get_work_item(mergeable.admin, mergeable.work_item)["stage"] == "DOCUMENTATION"


def test_only_merge_service_requests_merges(mergeable):
    assert codes(merge(mergeable, who="orchestrator")) == ["ACTOR_NOT_AUTHORIZED"]


def test_merge_before_review_gate_is_invalid():
    w = World()
    w.advance_to(Stage.TEST_COVERAGE)
    w.refs["DOCS"] = {"artifact_id": "X", "version": 1}
    assert codes(merge(w)) == ["INVALID_TRANSITION"]


@pytest.mark.parametrize("path", ["src/notifications/__init__.py", "uat/test_critical_delivered.py",
                                  "tests/coverage/test_preferences.py"])
def test_code_or_test_change_after_testing_is_stale(mergeable, path):
    d = merge(mergeable, changed=["docs/notifications/user.md", path])
    assert d["decision"] == BLOCK and codes(d) == ["STALE_HEAD"]


def test_diff_must_start_from_last_tested_revision(mergeable):
    assert codes(merge(mergeable, tested="0" * 40)) == ["STALE_HEAD"]


def test_docs_are_required(mergeable):
    assert codes(merge(mergeable, refs={})) == ["MISSING_EVIDENCE"]


def test_unapproved_docs_pause(mergeable):
    view = mergeable.submit("docs_agent", "DOCS", "DOCS-NOTIFY", mergeable.docs_body(monitoring_baseline="revised"))
    mergeable.approve("po", view)
    d = merge(mergeable, refs={"DOCS": ref(view)})
    assert d["decision"] == PAUSE and codes(d) == ["APPROVAL_MISSING"]


def test_upstream_drift_blocks_merge(mergeable):
    mergeable.svc.submit_contract(mergeable.tok("requirements_agent"), "C1", "BR-NOTIFY",
                                  mergeable.c1_body(normal_flow="changed after review"))
    assert merge(mergeable)["decision"] == BLOCK


def test_merge_decisions_are_idempotent(mergeable):
    first = merge(mergeable, key="MRG-KEY")
    assert merge(mergeable, key="MRG-KEY")["transition_id"] == first["transition_id"]
    assert expect_error(lambda: merge(mergeable, key="MRG-KEY", changed=["docs/x.md"])) == "IDEMPOTENCY_KEY_REUSED"
