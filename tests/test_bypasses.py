"""Regression pack for the six candidate-core weaknesses in compendium section 22.1,
plus the reported counterexample (normal C1 update invalidates downstream evidence)."""

import base64
import sqlite3

import pytest

from aidlc_g.errors import AuthenticationError, AuthorizationError
from aidlc_g.lifecycle import Stage
from aidlc_g.service import ALLOW, BLOCK
from aidlc_g.testkit import World, expect_error, ref


def codes(decision):
    return [r["code"] for r in decision["reasons"]]


# 6. Unauthorized promoter accepted ---------------------------------------------


@pytest.mark.parametrize("promoter", ["impl_agent", "po", "requirements_agent", "reviewer"])
def test_bypass6_promotion_requires_orchestrator_authority(world: World, promoter):
    world.make_c0()
    decision = world.transition(Stage.EPIC, requester=promoter)
    assert decision["decision"] == BLOCK
    assert codes(decision) == ["ACTOR_NOT_AUTHORIZED"]
    assert world.svc.get_work_item(world.tok("orchestrator"), world.work_item)["stage"] == "INITIATIVE"


def test_bypass6_release_manager_may_promote_only_to_release(world: World):
    world.make_c0()
    assert codes(world.transition(Stage.EPIC, requester="rel_mgr")) == ["ACTOR_NOT_AUTHORIZED"]


# 7. Forged runner signature accepted --------------------------------------------


def test_bypass7_forged_signature_rejected_and_not_stored(world: World):
    world.advance_to(Stage.IMPLEMENTATION)
    world.register_build("BLD-1")
    payload = world.attestation("BLD-1")
    forged = base64.b64encode(b"\x00" * 64).decode()
    with pytest.raises(AuthenticationError) as exc:
        world.svc.ingest_attestation(payload, forged)
    assert exc.value.code == "INVALID_SIGNATURE"
    world.refs["BUILD"] = "BLD-1"
    assert "MISSING_EVIDENCE" in codes(world.transition(Stage.TEST_COVERAGE))


# 8. Unregistered tester accepted ------------------------------------------------


def test_bypass8_only_registered_test_authorities_author_manifests(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    world.add_principal("random_dev", "human", ["engineer"])
    for author in ("random_dev", "impl_agent", "architecture_agent"):
        assert expect_error(lambda: world.submit(author, "C3", "AC-NOTIFY", world.c3_body())) == "ROLE_NOT_PERMITTED"


def test_bypass8_only_qa_satisfies_qa_approval(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    view = world.submit("uat_agent", "C3", "AC-NOTIFY", world.c3_body())
    world.approve("po", view)
    assert expect_error(lambda: world.approve("eng_lead", view)) == "ROLE_NOT_PERMITTED"
    assert expect_error(lambda: world.approve("reviewer", view)) == "ROLE_NOT_PERMITTED"
    world.refs["C3"] = ref(view)
    assert world.svc.get_artifact(world.admin, "AC-NOTIFY")["missing_approver_roles"] == ["qa"]


def test_bypass8_unregistered_tester_cannot_register_baseline(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    world.make_c3()
    with pytest.raises(AuthorizationError):
        world.register_build("BLD-STUB-X", kind="BASELINE_STUB", registrant="impl_agent")


# 9. Wrong requirement identifier accepted ---------------------------------------


def test_bypass9_manifest_bound_to_other_requirement_is_blocked(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    other = world.submit("requirements_agent", "C1", "BR-OTHER", world.c1_body(requirement_id="BR-OTHER"))
    world.approve("po", other)
    view = world.submit("uat_agent", "C3", "AC-NOTIFY", world.c3_body(business_requirement=ref(other)))
    world.approve("po", view)
    world.approve("qa", view)
    world.refs["C3"] = ref(view)
    world.make_baseline()
    decision = world.transition(Stage.IMPLEMENTATION)
    assert decision["decision"] == BLOCK and "REF_MISMATCH" in codes(decision)


def test_bypass9_work_item_binding_cannot_be_swapped(world: World):
    world.advance_to(Stage.SPIKE)
    other = world.submit("requirements_agent", "C1", "BR-OTHER", world.c1_body(requirement_id="BR-OTHER"))
    world.approve("po", other)
    world.make_c2()
    decision = world.transition(Stage.STORIES, refs={**world.refs, "C1": ref(other)})
    assert "BINDING_MISMATCH" in codes(decision)


def test_bypass9_reference_to_unknown_requirement_rejected(world: World):
    world.advance_to(Stage.ACCEPTANCE_TESTS)
    bogus = dict(world.refs["C1"], artifact_id="BR-DOES-NOT-EXIST")
    code = expect_error(lambda: world.submit("uat_agent", "C3", "AC-NOTIFY", world.c3_body(business_requirement=bogus)))
    assert code == "UNKNOWN_REFERENCE"


# 10. Approved test overwritten under the same identifier -------------------------


def test_bypass10_overwrite_creates_new_version_and_invalidates_evidence(world: World):
    world.advance_to(Stage.IMPLEMENTATION)
    world.make_candidate()
    body = world.c3_body()
    body["tests"][0] = dict(body["tests"][0], content_digest="sha256:" + "0" * 64)
    v2 = world.submit("uat_agent", "C3", "AC-NOTIFY", body)
    assert v2["version"] == 2 and v2["status"] == "DRAFT"
    assert "build:BLD-1" in v2["invalidated"] and "build:BLD-STUB-1" in v2["invalidated"]
    assert world.svc.get_artifact(world.admin, "AC-NOTIFY", 1)["status"] == "SUPERSEDED"
    # Old evidence cannot carry the work item forward under either manifest version.
    assert world.transition(Stage.TEST_COVERAGE)["decision"] == BLOCK
    assert world.transition(Stage.TEST_COVERAGE, refs={**world.refs, "C3": ref(v2)})["decision"] == BLOCK


def test_bypass10_versions_are_immutable_in_storage(world: World):
    world.make_c0()
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        world.svc._db.execute("UPDATE artifacts SET body_json = '{}' WHERE artifact_id = 'INIT-NOTIFY'")


def test_bypass10_identical_resubmission_is_a_no_op(world: World):
    world.make_c0()
    again = world.submit("requirements_agent", "C0", "INIT-NOTIFY", world.c0_body())
    assert again["unchanged"] is True and again["version"] == 1 and again["approval_state"] == "APPROVED"


# 11. Direct state and ledger mutation --------------------------------------------


def test_bypass11_returned_views_are_copies(world: World):
    world.make_c0()
    view = world.svc.get_artifact(world.admin, "INIT-NOTIFY")
    view["status"] = "APPROVED"
    view["body"]["objective"] = "something else"
    fresh = world.svc.get_artifact(world.admin, "INIT-NOTIFY")
    assert fresh["body"]["objective"] == "Give users control over optional notifications"


def test_bypass11_ledger_rejects_update_and_delete(world: World):
    world.make_c0()
    db = world.svc._db  # simulates a caller with raw database access
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit SET actor_id = 'nobody'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("DELETE FROM approvals")


def test_bypass11_stage_cannot_advance_except_through_gate(world: World):
    world.make_c0()
    decision = world.transition(Stage.SPIKE)  # skip EPIC
    assert codes(decision) == ["INVALID_TRANSITION"]


# Counterexample: ordinary requirement update DID invalidate (preserved behaviour) -


def test_counterexample_normal_c1_update_invalidates_downstream(world: World):
    world.advance_to(Stage.TEST_COVERAGE)
    v2 = world.submit("requirements_agent", "C1", "BR-NOTIFY",
                      world.c1_body(requested_behavior="Disable optional notifications per channel"))
    for node in ("TR-NOTIFY@v1", "STORIES-NOTIFY@v1", "AC-NOTIFY@v1", "build:BLD-1", "build:BLD-STUB-1"):
        assert node in v2["invalidated"]
    assert world.svc.get_artifact(world.admin, "INIT-NOTIFY")["status"] == "APPROVED"  # upstream untouched
    assert expect_error(world.make_review) == "EVIDENCE_INVALIDATED"
    assert world.svc.reverify(world.tok("orchestrator"), world.work_item)["decision"] == BLOCK
    assert world.transition(Stage.DOCUMENTATION)["decision"] != ALLOW
