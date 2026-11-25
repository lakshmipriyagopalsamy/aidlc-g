"""Gate rules for build provenance, release records and deployment decisions (M4)."""

import sqlite3

import pytest

from aidlc_g.canonical import file_digest
from aidlc_g.crypto import QUALITY_DOMAIN, sign_payload
from aidlc_g.lifecycle import Stage
from aidlc_g.policy import Policy
from aidlc_g.registry import ArtifactRegistry, deterministic_tar
from aidlc_g.service import ALLOW, BLOCK
from aidlc_g.testkit import World, expect_error, ref

BASELINE = file_digest(b"legacy artifact")


def codes(decision):
    return [r["code"] for r in decision["reasons"]]


@pytest.fixture
def merged():
    w = World()
    w.advance_to(Stage.DOCUMENTATION)
    w.make_docs()
    w.make_merge()
    return w


# --- Provenance ---------------------------------------------------------------------


def test_provenance_requires_a_merged_source(merged):
    assert expect_error(lambda: merged.make_provenance(source_revision="abcdef1234567")) == "UNMERGED_SOURCE"


def test_provenance_must_describe_the_tested_code(merged):
    assert expect_error(lambda: merged.make_provenance(build_digest=file_digest(b"other"))) == "BUILD_BINDING_MISMATCH"


def test_provenance_signature_domain_is_separate(merged):
    payload = merged.provenance_payload()
    sig = sign_payload(merged.runner_key, payload, QUALITY_DOMAIN)
    assert expect_error(lambda: merged.svc.ingest_provenance(payload, sig)) == "INVALID_SIGNATURE"


def test_provenance_cannot_be_built_before_merge():
    w = World()
    w.advance_to(Stage.DOCUMENTATION)
    assert expect_error(w.make_provenance) == "UNMERGED_SOURCE"


# --- Release gate -------------------------------------------------------------------


def test_release_without_provenance_is_blocked(merged):
    merged.make_release()
    assert codes(merged.transition(Stage.RELEASE_READY)) == ["PROVENANCE_MISSING"]


def test_release_must_name_the_attested_artifact(merged):
    merged.make_provenance()
    view = merged.submit("release_agent", "RELEASE", "REL-NOTIFY",
                         merged.release_body(artifact_digest=file_digest(b"rebuilt on a laptop")))
    merged.approve("rel_mgr", view)
    merged.refs["RELEASE"] = ref(view)
    assert codes(merged.transition(Stage.RELEASE_READY)) == ["RELEASE_ARTIFACT_MISMATCH"]


def test_release_needs_a_monitoring_baseline(merged):
    merged.make_provenance()
    view = merged.submit("release_agent", "RELEASE", "REL-NOTIFY", merged.release_body(health_checks=[]))
    merged.approve("rel_mgr", view)
    merged.refs["RELEASE"] = ref(view)
    assert codes(merged.transition(Stage.RELEASE_READY)) == ["MONITORING_BASELINE_MISSING"]


def test_provenance_requirement_is_a_policy_switch():
    w = World(policy=Policy(require_release_provenance=False))
    w.advance_to(Stage.DOCUMENTATION)
    w.make_docs()
    w.make_release()
    assert w.transition(Stage.RELEASE_READY)["decision"] == ALLOW


# --- Deployment decisions -----------------------------------------------------------


@pytest.fixture
def released():
    w = World()
    w.advance_to(Stage.RELEASE_READY)
    for env in ("staging", "production"):
        w.svc.register_environment(w.admin, env, BASELINE)
    return w


def deploy(w, env="staging", digest=None, who="deploy-bot", release=None):
    return w.svc.evaluate_deployment(w.tok(who), w.work_item, env, release or w.refs["RELEASE"],
                                     digest or w.provenance["artifact"]["digest"], w.next_id("DEP"))


def healthy(w, decision):
    w.svc.record_deployment(w.tok("deploy-bot"), decision["transition_id"], "CANARY",
                            {"critical_alert_delivery_pct": 100})
    return w.svc.record_deployment(w.tok("deploy-bot"), decision["transition_id"], "PROMOTED")


def test_staging_then_production(released):
    s = deploy(released)
    assert s["decision"] == ALLOW and s["rollback_target"] == BASELINE
    assert codes(deploy(released, "production")) == ["PROMOTION_ORDER"]
    healthy(released, s)
    p = deploy(released, "production")
    assert p["decision"] == ALLOW
    healthy(released, p)
    assert released.svc.get_environment(released.admin, "production")["current_digest"] == released.provenance["artifact"]["digest"]


def test_only_the_released_artifact_deploys(released):
    assert codes(deploy(released, digest=file_digest(b"hotfix"))) == ["RELEASE_ARTIFACT_MISMATCH"]


def test_only_deploy_service_deploys(released):
    assert codes(deploy(released, who="rel_mgr")) == ["ACTOR_NOT_AUTHORIZED"]


def test_unknown_environment_blocked(released):
    assert codes(deploy(released, env="laptop")) == ["UNKNOWN_ENVIRONMENT"]


def test_environment_without_baseline_has_no_rollback_path():
    w = World()
    w.advance_to(Stage.RELEASE_READY)
    assert codes(deploy(w)) == ["NO_ROLLBACK_TARGET"]


def test_deploy_requires_release_ready():
    w = World()
    w.advance_to(Stage.DOCUMENTATION)
    w.svc.register_environment(w.admin, "staging", BASELINE)
    w.refs["RELEASE"] = {"artifact_id": "REL-X", "version": 1}
    w.provenance = {"artifact": {"digest": BASELINE}}
    assert codes(deploy(w)) == ["INVALID_TRANSITION"]


def test_upstream_drift_blocks_deployment(released):
    released.svc.submit_contract(released.tok("requirements_agent"), "C1", "BR-NOTIFY",
                                 released.c1_body(normal_flow="changed after release approval"))
    assert deploy(released)["decision"] == BLOCK


def test_gate_judges_canary_health(released):
    d = deploy(released)
    v = released.svc.record_deployment(released.tok("deploy-bot"), d["transition_id"], "CANARY",
                                       {"critical_alert_delivery_pct": 97})
    assert v["verdict"] == "UNHEALTHY" and "critical_alert_delivery_pct=97" in v["failed_checks"][0]
    code = expect_error(lambda: released.svc.record_deployment(released.tok("deploy-bot"), d["transition_id"], "PROMOTED"))
    assert code == "UNHEALTHY_CANARY"
    rb = released.svc.record_deployment(released.tok("deploy-bot"), d["transition_id"], "ROLLED_BACK")
    assert rb["restored_digest"] == BASELINE
    assert released.svc.get_environment(released.admin, "staging")["current_digest"] == BASELINE


def test_unreported_metric_is_unhealthy(released):
    d = deploy(released)
    v = released.svc.record_deployment(released.tok("deploy-bot"), d["transition_id"], "CANARY", {})
    assert v["verdict"] == "UNHEALTHY" and v["failed_checks"] == ["critical_alert_delivery_pct: not reported"]


def test_promotion_requires_a_canary(released):
    d = deploy(released)
    assert expect_error(lambda: released.svc.record_deployment(
        released.tok("deploy-bot"), d["transition_id"], "PROMOTED")) == "UNHEALTHY_CANARY"


def test_closed_deployment_takes_no_more_events(released):
    d = deploy(released)
    healthy(released, d)
    assert expect_error(lambda: released.svc.record_deployment(
        released.tok("deploy-bot"), d["transition_id"], "ROLLED_BACK")) == "DEPLOYMENT_CLOSED"


def test_blocked_decision_cannot_record_events(released):
    d = deploy(released, "production")
    assert expect_error(lambda: released.svc.record_deployment(
        released.tok("deploy-bot"), d["transition_id"], "CANARY", {})) == "DEPLOY_NOT_ALLOWED"


def test_metrics_must_be_integers(released):
    d = deploy(released)
    assert expect_error(lambda: released.svc.record_deployment(
        released.tok("deploy-bot"), d["transition_id"], "CANARY", {"critical_alert_delivery_pct": 99.9})) == "INVALID_METRICS"


def test_deployment_records_are_append_only(released):
    healthy(released, deploy(released))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        released.svc._db.execute("UPDATE deployments SET verdict = 'HEALTHY'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        released.svc._db.execute("DELETE FROM deployments")


def test_release_manifest_records_deployments(released):
    healthy(released, deploy(released))
    m = released.svc.release_manifest(released.tok("rel_mgr"), released.work_item)
    assert m["artifact_digest"] == released.provenance["artifact"]["digest"]
    assert m["provenance"]["provenance_id"] == released.provenance["provenance_id"]
    assert [d["event"] for d in m["production_deployment"]] == ["CANARY", "PROMOTED"]


# --- Registry -----------------------------------------------------------------------


def test_packaging_is_reproducible():
    files = {"src/b.py": b"b", "src/a.py": b"a"}
    assert deterministic_tar(files) == deterministic_tar(dict(reversed(list(files.items()))))


def test_registry_write_needs_ci_and_detects_corruption():
    w = World()
    w.add_principal("ci", "service", ["ci_runner"])
    reg = ArtifactRegistry(w.svc)
    assert expect_error(lambda: reg.put(w.tok("impl_agent"), b"x")) == "ROLE_NOT_PERMITTED"
    address = reg.put(w.tok("ci"), b"artifact")
    assert reg.get(address) == b"artifact"
    reg._corrupt_for_test(address, b"swapped")
    assert expect_error(lambda: reg.get(address)) == "ARTIFACT_CORRUPT"
