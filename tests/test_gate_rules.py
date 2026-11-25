"""Approval semantics, separation of duties and fail-closed gate invariants (sections 8, 10, 21.1)."""

import pytest

from aidlc_g.canonical import file_digest
from aidlc_g.errors import AuthenticationError
from aidlc_g.lifecycle import Stage
from aidlc_g.service import ALLOW, BLOCK, PAUSE
from aidlc_g.testkit import World, expect_error, ref


def codes(decision):
    return [r["code"] for r in decision["reasons"]]


# --- Approvals ------------------------------------------------------------------


def test_silence_is_a_pause_not_approval(world: World):
    world.advance_to(Stage.EPIC)
    world.refs["C1"] = ref(world.submit("requirements_agent", "C1", "BR-NOTIFY", world.c1_body()))
    decision = world.transition(Stage.SPIKE)
    assert decision["decision"] == PAUSE and codes(decision) == ["APPROVAL_MISSING"]


def test_author_cannot_approve_own_version(world: World):
    world.advance_to(Stage.EPIC)
    view = world.submit("po", "C1", "BR-NOTIFY", world.c1_body())
    assert expect_error(lambda: world.approve("po", view)) == "SELF_APPROVAL"


def test_approval_bound_to_exact_digest(world: World):
    world.advance_to(Stage.EPIC)
    view = world.submit("requirements_agent", "C1", "BR-NOTIFY", world.c1_body())
    wrong = dict(view, digest=file_digest(b"something else"))
    assert expect_error(lambda: world.approve("po", wrong)) == "DIGEST_MISMATCH"


def test_superseded_version_cannot_be_approved(world: World):
    world.advance_to(Stage.EPIC)
    v1 = world.submit("requirements_agent", "C1", "BR-NOTIFY", world.c1_body())
    world.submit("requirements_agent", "C1", "BR-NOTIFY", world.c1_body(normal_flow="changed"))
    assert expect_error(lambda: world.approve("po", v1)) == "STALE_VERSION"


def test_request_changes_blocks(world: World):
    world.advance_to(Stage.EPIC)
    view = world.submit("requirements_agent", "C1", "BR-NOTIFY", world.c1_body())
    world.approve("po", view, decision="REQUEST_CHANGES")
    world.refs["C1"] = ref(view)
    decision = world.transition(Stage.SPIKE)
    assert decision["decision"] == BLOCK and "ARTIFACT_RETURNED" in codes(decision)


def test_revoked_authority_stops_counting(world: World):
    world.make_c0()
    world.svc.set_principal_roles(world.admin, "sponsor", [])
    decision = world.transition(Stage.EPIC)
    assert decision["decision"] == PAUSE and codes(decision) == ["APPROVAL_MISSING"]


def test_deactivated_principal_cannot_authenticate(world: World):
    world.svc.set_principal_active(world.admin, "po", False)
    with pytest.raises(AuthenticationError):
        world.svc.get_work_item(world.tok("po"), world.work_item)


def test_unauthenticated_calls_rejected(world: World):
    for token in (None, "", "forged-token"):
        with pytest.raises(AuthenticationError):
            world.svc.evaluate_transition(token, world.work_item, "EPIC", {}, world.svc.policy_version, "K1")


def test_one_person_cannot_fill_two_required_roles(world: World):
    world.add_principal("po_qa", "human", ["product_owner", "qa"])
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    view = world.submit("uat_agent", "C3", "AC-NOTIFY", world.c3_body())
    world.approve("po_qa", view, as_role="product_owner")
    assert expect_error(lambda: world.approve("po_qa", view, as_role="qa")) == "DECISION_ALREADY_RECORDED"
    assert world.svc.get_artifact(world.admin, "AC-NOTIFY")["missing_approver_roles"] == ["qa"]


def test_agents_never_approve(world: World):
    world.make_c0()
    world.add_principal("sponsor_bot", "agent", ["sponsor"])
    view = world.submit("requirements_agent", "C0", "INIT-NOTIFY", world.c0_body(objective="changed"))
    assert expect_error(lambda: world.approve("sponsor_bot", view)) == "HUMAN_APPROVAL_REQUIRED"


# --- Contract content -------------------------------------------------------------


def test_open_blocking_business_decision_pauses(world: World):
    world.advance_to(Stage.EPIC)
    world.make_c1(unknowns=[{"item_id": "U1", "question": "Which alerts are critical?",
                             "blocking": True, "status": "OPEN"}])
    decision = world.transition(Stage.SPIKE)
    assert decision["decision"] == PAUSE and "OPEN_BLOCKING_DECISION" in codes(decision)


def test_design_needing_product_decision_returns_to_c1(world: World):
    world.advance_to(Stage.SPIKE)
    world.make_c2(new_product_decisions_required=["Should SMS count as optional?"])
    decision = world.transition(Stage.STORIES)
    assert decision["decision"] == BLOCK and "RETURN_TO_C1" in codes(decision)


def test_derivation_from_superseded_version_rejected(world: World):
    world.advance_to(Stage.SPIKE)
    old = dict(world.refs["C1"])
    world.make_c1(normal_flow="revised flow")
    code = expect_error(lambda: world.submit("architecture_agent", "C2", "TR-NOTIFY",
                                             world.c2_body(source_business_requirement=old)))
    assert code == "STALE_REFERENCE"


def test_targeted_invalidation_spares_unaffected_upstream(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    v2 = world.submit("architecture_agent", "C2", "TR-NOTIFY", world.c2_body(operability="revised"))
    assert set(v2["invalidated"]) == {"STORIES-NOTIFY@v1"}
    assert world.svc.get_artifact(world.admin, "BR-NOTIFY")["approval_state"] == "APPROVED"
    assert world.svc.get_artifact(world.admin, "INIT-NOTIFY")["approval_state"] == "APPROVED"


# --- Acceptance-first and independence --------------------------------------------


def test_negative_path_test_required():
    w = World()
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    w.make_c3(drop_scenarios=("SC-3", "SC-4"))
    w.make_baseline()
    decision = w.transition(Stage.IMPLEMENTATION)
    assert "NEGATIVE_PATH_MISSING" in codes(decision)


def test_suite_that_passes_on_stub_cannot_lock(world: World):
    world.prepare(Stage.IMPLEMENTATION)  # stub attested FAILED; a later run shows it passing
    world.attest("BLD-STUB-1", outcome="PASSED")
    decision = world.transition(Stage.IMPLEMENTATION)
    assert decision["decision"] == BLOCK and "TESTS_CANNOT_FAIL" in codes(decision)


def test_baseline_required_before_implementation(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    world.make_c3()
    decision = world.transition(Stage.IMPLEMENTATION)
    assert "MISSING_EVIDENCE" in codes(decision)


def test_build_requires_locked_manifest(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    view = world.submit("uat_agent", "C3", "AC-NOTIFY", world.c3_body())
    world.approve("po", view)  # QA has not approved: not locked
    world.refs["C3"] = ref(view)
    assert expect_error(lambda: world.register_build("BLD-STUB-1", "BASELINE_STUB", "uat_agent")) == "MANIFEST_NOT_LOCKED"


def test_manifest_author_cannot_implement(world: World):
    world.add_principal("tester_dev", "human", ["qa", "engineer"])
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    view = world.submit("tester_dev", "C3", "AC-NOTIFY", world.c3_body())
    world.approve("po", view)
    world.approve("qa", view)
    world.refs["C3"] = ref(view)
    world.make_baseline()
    decision = world.transition(Stage.IMPLEMENTATION)
    assert "INDEPENDENCE_VIOLATION" in codes(decision)
    assert expect_error(lambda: world.register_build("BLD-1", registrant="tester_dev")) == "INDEPENDENCE_VIOLATION"


def test_implementer_cannot_approve_manifest(world: World):
    world.add_principal("qa_dev", "human", ["qa", "engineer"])
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    view = world.submit("uat_agent", "C3", "AC-NOTIFY", world.c3_body())
    assert expect_error(lambda: world.approve("qa_dev", view)) == "INDEPENDENCE_VIOLATION"


# --- CI evidence ---------------------------------------------------------------------


def test_signed_failure_is_accepted_evidence_but_blocks(world: World):
    world.advance_to(Stage.IMPLEMENTATION)
    world.register_build("BLD-1")
    world.attest("BLD-1", outcome="FAILED")  # authentic, not tampered
    world.refs["BUILD"] = "BLD-1"
    decision = world.transition(Stage.TEST_COVERAGE)
    assert codes(decision) == ["TESTS_FAILED"]


def test_latest_attestation_wins(world: World):
    world.advance_to(Stage.IMPLEMENTATION)
    world.make_candidate()
    world.attest("BLD-1", outcome="FAILED")
    assert "TESTS_FAILED" in codes(world.transition(Stage.TEST_COVERAGE))


def test_revoked_runner_evidence_fails_closed(world: World):
    world.advance_to(Stage.IMPLEMENTATION)
    world.make_candidate()
    world.svc.revoke_runner(world.admin, world.runner_id)
    assert "ATTESTATION_UNTRUSTED" in codes(world.transition(Stage.TEST_COVERAGE))


def test_attestation_for_unregistered_test_rejected(world: World):
    world.advance_to(Stage.IMPLEMENTATION)
    world.register_build("BLD-1")
    payload = world.attestation("BLD-1")
    payload["results"].append({"test_id": "UAT-EXTRA", "test_digest": file_digest(b"x"), "outcome": "PASSED",
                               "project_imports": []})
    assert expect_error(lambda: world.svc.ingest_attestation(payload, world.sign(payload))) == "UNKNOWN_TEST_RESULT"


@pytest.mark.parametrize("revision", ["abcdef1\n", "ABCDEF1", "abc"])
def test_build_revision_must_be_exact_hex(world: World, revision):
    world.advance_to(Stage.IMPLEMENTATION)
    assert expect_error(lambda: world.register_build("BLD-1", revision=revision)) == "INVALID_REVISION"


def test_identifier_with_trailing_newline_rejected(world: World):
    world.advance_to(Stage.EPIC)
    code = expect_error(lambda: world.submit("requirements_agent", "C1", "BR-NOTIFY\n", world.c1_body()))
    assert code == "INVALID_IDENTIFIER"


def test_attestation_build_digest_must_match(world: World):
    world.advance_to(Stage.IMPLEMENTATION)
    world.register_build("BLD-1")
    payload = world.attestation("BLD-1", build_digest=file_digest(b"other artifact"))
    assert expect_error(lambda: world.svc.ingest_attestation(payload, world.sign(payload))) == "BUILD_BINDING_MISMATCH"


# --- Review -------------------------------------------------------------------------


def test_agent_cannot_clear_findings(world: World):
    world.advance_to(Stage.TEST_COVERAGE)
    finding = {"finding_id": "F1", "severity": "BLOCKING", "summary": "missing authz check", "status": "RESOLVED"}
    code = expect_error(lambda: world.svc.record_review(world.tok("review_agent"), "R1", "BLD-1", "FINDINGS_ONLY", [finding]))
    assert code == "AGENT_CANNOT_CLEAR_FINDINGS"


def test_agent_blocking_finding_needs_human_resolution(world: World):
    world.advance_to(Stage.TEST_COVERAGE)
    finding = {"finding_id": "F1", "severity": "BLOCKING", "summary": "missing authz check", "status": "OPEN"}
    world.svc.record_review(world.tok("review_agent"), "R1", "BLD-1", "FINDINGS_ONLY", [finding])
    world.make_quality()
    assert expect_error(lambda: world.make_review()) == "BLOCKING_FINDINGS_OPEN"
    assert "BLOCKING_FINDINGS_OPEN" in codes(world.transition(Stage.DOCUMENTATION))
    world.svc.record_review(world.tok("reviewer"), "R2", "BLD-1", "APPROVE", [dict(finding, status="RESOLVED")])
    assert world.transition(Stage.DOCUMENTATION)["decision"] == ALLOW


def test_review_missing_pauses(world: World):
    world.advance_to(Stage.TEST_COVERAGE)
    world.make_quality()
    decision = world.transition(Stage.DOCUMENTATION)
    assert decision["decision"] == PAUSE and codes(decision) == ["REVIEW_MISSING"]


# --- Release ----------------------------------------------------------------------


def test_release_must_point_at_tested_build(world: World):
    world.prepare(Stage.RELEASE_READY)
    view = world.submit("release_agent", "RELEASE", "REL-NOTIFY",
                        world.release_body(build_digest=file_digest(b"rebuilt elsewhere")))
    world.approve("rel_mgr", view)
    world.refs["RELEASE"] = ref(view)
    decision = world.transition(Stage.RELEASE_READY)
    assert decision["decision"] == BLOCK and "RELEASE_BUILD_MISMATCH" in codes(decision)


def test_release_manifest_links_full_chain(world: World):
    world.advance_to(Stage.RELEASE_READY)
    m = world.svc.release_manifest(world.tok("rel_mgr"), world.work_item)
    assert m["acceptance_manifest"]["artifact_id"] == "AC-NOTIFY"
    assert set(m["requirement_versions"]) == {"C0", "C1", "C2", "STORIES"}
    assert m["human_release_approvers"][0]["approver_id"] == "rel_mgr"
    assert m["documentation"]["runbook"]["uri"]


def test_reverify_detects_post_release_drift(world: World):
    world.advance_to(Stage.RELEASE_READY)
    assert world.svc.reverify(world.tok("orchestrator"), world.work_item)["decision"] == ALLOW
    world.svc.set_principal_roles(world.admin, "rel_mgr", [])
    assert world.svc.reverify(world.tok("orchestrator"), world.work_item)["decision"] == PAUSE


# --- Transition protocol -----------------------------------------------------------


def test_policy_version_must_match(world: World):
    world.make_c0()
    assert codes(world.transition(Stage.EPIC, policy_version="governance-policy-v0")) == ["POLICY_VERSION_MISMATCH"]


def test_idempotency_key_cannot_be_reused_for_other_request(world: World):
    world.make_c0()
    world.transition(Stage.EPIC, key="K-1")
    world.make_c1()
    assert expect_error(lambda: world.transition(Stage.SPIKE, key="K-1")) == "IDEMPOTENCY_KEY_REUSED"


def test_unknown_evidence_slot_is_malformed(world: World):
    world.make_c0()
    assert codes(world.transition(Stage.EPIC, refs={**world.refs, "SECRET_OVERRIDE": True})) == ["MALFORMED_REQUEST"]


def test_every_stage_is_reachable_in_order(world: World):
    world.advance_to(Stage.RELEASE_READY)
    assert world.svc.get_work_item(world.tok("orchestrator"), world.work_item)["stage"] == "RELEASE_READY"
    assert world.transition(Stage.RELEASE_READY)["decision"] == BLOCK  # no stage beyond
