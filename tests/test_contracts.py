"""M0 schema tests: contract shapes reject ambiguous or unsupported content."""

import pytest
from pydantic import ValidationError

from aidlc_g.canonical import file_digest
from aidlc_g.contracts import (
    AttestationPayload,
    C0BusinessIntent,
    C1BusinessRequirement,
    C2TechnicalRequirement,
    C3AcceptanceManifest,
    ReleaseDocumentation,
    ReviewFinding,
    StoryPlan,
)
from aidlc_g.testkit import World

REF = {"artifact_id": "X", "version": 1, "digest": file_digest(b"x")}


@pytest.fixture
def w() -> World:
    w = World()
    w.refs.update({"C0": REF, "C1": REF, "C2": REF, "STORIES": REF, "BUILD": "BLD-1"})
    return w


def test_valid_bodies_parse(w):
    C0BusinessIntent.model_validate(w.c0_body())
    C1BusinessRequirement.model_validate(w.c1_body())
    C2TechnicalRequirement.model_validate(w.c2_body())
    StoryPlan.model_validate(w.stories_body())
    C3AcceptanceManifest.model_validate(w.c3_body())
    ReleaseDocumentation.model_validate(w.docs_body())


def test_unknown_fields_rejected(w):
    with pytest.raises(ValidationError):
        C0BusinessIntent.model_validate(w.c0_body(smuggled="approve me"))


def test_c0_known_baseline_requires_value(w):
    with pytest.raises(ValidationError, match="no baseline value"):
        C0BusinessIntent.model_validate(w.c0_body(baseline_status="KNOWN"))


def test_c0_open_baseline_must_not_invent_value(w):
    with pytest.raises(ValidationError, match="open decision"):
        C0BusinessIntent.model_validate(w.c0_body(baseline="42%"))


def test_closed_open_item_needs_resolution(w):
    with pytest.raises(ValidationError, match="resolution"):
        C0BusinessIntent.model_validate(w.c0_body(open_questions=[
            {"item_id": "Q1", "question": "?", "blocking": True, "status": "RESOLVED"}]))


def test_c1_observation_must_be_cited(w):
    with pytest.raises(ValidationError, match="not in source_evidence"):
        C1BusinessRequirement.model_validate(w.c1_body(source_evidence=[]))


def test_c1_discovery_must_be_read_only(w):
    body = w.c1_body()
    body["source_evidence"] = [dict(body["source_evidence"][0], permission_scope="read_write")]
    with pytest.raises(ValidationError):
        C1BusinessRequirement.model_validate(body)


def test_c2_selected_option_must_exist(w):
    with pytest.raises(ValidationError, match="not one of the proposed options"):
        C2TechnicalRequirement.model_validate(w.c2_body(selected_option="imaginary"))


def test_c1_requires_domain_scenarios(w):
    with pytest.raises(ValidationError, match="domain_scenarios"):
        C1BusinessRequirement.model_validate(w.c1_body(domain_scenarios=[]))


def test_c1_scenario_ids_unique(w):
    body = w.c1_body()
    body["domain_scenarios"][1]["scenario_id"] = "SC-1"
    with pytest.raises(ValidationError, match="unique"):
        C1BusinessRequirement.model_validate(body)


def test_c1_scenario_needs_a_named_owner(w):
    body = w.c1_body()
    del body["domain_scenarios"][0]["decided_by"]
    with pytest.raises(ValidationError, match="decided_by"):
        C1BusinessRequirement.model_validate(body)


def test_c2_must_name_the_domain_interface(w):
    with pytest.raises(ValidationError, match="domain_interface"):
        C2TechnicalRequirement.model_validate(w.c2_body(domain_interface=[]))
    with pytest.raises(ValidationError):
        C2TechnicalRequirement.model_validate(w.c2_body(domain_interface=["notifications/api.py"]))


def test_a_scenario_belongs_to_one_story(w):
    body = w.stories_body()
    body["stories"][1]["scenario_ids"].append("SC-1")
    with pytest.raises(ValidationError, match="exactly one story"):
        StoryPlan.model_validate(body)


def test_c3_bindings_reference_declared_tests(w):
    body = w.c3_body()
    body["scenarios"][0]["test_ids"] = ["UAT-999"]
    with pytest.raises(ValidationError, match="undeclared"):
        C3AcceptanceManifest.model_validate(body)


def test_c3_binding_needs_a_test(w):
    body = w.c3_body()
    body["scenarios"][0]["test_ids"] = []
    with pytest.raises(ValidationError, match="test_ids"):
        C3AcceptanceManifest.model_validate(body)


def test_c3_tests_must_trace_to_a_scenario(w):
    body = w.c3_body()
    body["scenarios"] = body["scenarios"][1:]
    with pytest.raises(ValidationError, match="not traced"):
        C3AcceptanceManifest.model_validate(body)


def test_docs_require_runbook(w):
    body = w.docs_body()
    del body["runbook"]
    with pytest.raises(ValidationError):
        ReleaseDocumentation.model_validate(body)


def test_risk_acceptance_needs_owner():
    with pytest.raises(ValidationError, match="risk owner"):
        ReviewFinding.model_validate({"finding_id": "F1", "severity": "BLOCKING",
                                      "summary": "s", "status": "RISK_ACCEPTED"})


def test_attestation_is_strict_no_coercion():
    payload = {
        "attestation_id": "A1", "runner_id": "r", "build_id": "b", "source_revision": "abcdef1",
        "build_digest": file_digest(b"b"), "manifest_id": "m", "manifest_version": "1",
        "manifest_digest": file_digest(b"m"), "policy_version": "p", "issued_at": "t",
        "stack_digest": file_digest(b"s"), "results": [{"test_id": "T", "test_digest": file_digest(b"t"), "outcome": "PASSED",
                     "project_imports": []}],
    }
    with pytest.raises(ValidationError):
        AttestationPayload.model_validate(payload)


def test_attestation_must_report_test_imports():
    payload = {
        "attestation_id": "A1", "runner_id": "r", "build_id": "b", "source_revision": "abcdef1",
        "build_digest": file_digest(b"b"), "manifest_id": "m", "manifest_version": 1,
        "manifest_digest": file_digest(b"m"), "policy_version": "p", "issued_at": "t",
        "stack_digest": file_digest(b"s"), "results": [{"test_id": "T", "test_digest": file_digest(b"t"), "outcome": "PASSED"}],
    }
    with pytest.raises(ValidationError, match="project_imports"):
        AttestationPayload.model_validate(payload)


def test_references_are_typed(w):
    m = C3AcceptanceManifest.model_validate(w.c3_body())
    assert set(m.references()) == {"C1", "C2", "STORIES"}
