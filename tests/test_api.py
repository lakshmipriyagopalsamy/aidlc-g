"""HTTP layer: the section 23 endpoints enforce the same rules as the service."""

import pytest
from fastapi.testclient import TestClient

from aidlc_g.api import create_app
from aidlc_g.lifecycle import Stage
from aidlc_g.testkit import World


@pytest.fixture
def api(world: World):
    return world, TestClient(create_app(world.svc))


def auth(world: World, who: str):
    return {"Authorization": f"Bearer {world.tok(who)}"}


def test_requires_bearer_token(api):
    world, client = api
    r = client.post("/contracts/C0", json={"artifact_id": "INIT-NOTIFY", "body": world.c0_body()})
    assert r.status_code == 401 and r.json()["error"] == "UNAUTHENTICATED"


def test_wrong_role_is_403(api):
    world, client = api
    r = client.post("/contracts/C0", json={"artifact_id": "INIT-NOTIFY", "body": world.c0_body()},
                    headers=auth(world, "impl_agent"))
    assert r.status_code == 403 and r.json()["error"] == "ROLE_NOT_PERMITTED"


def test_schema_violation_is_422(api):
    world, client = api
    r = client.post("/contracts/C0", json={"artifact_id": "INIT-NOTIFY", "body": {"objective": "x"}},
                    headers=auth(world, "requirements_agent"))
    assert r.status_code == 422 and r.json()["error"] == "SCHEMA_VIOLATION"


def test_submit_approve_transition_and_audit(api):
    world, client = api
    r = client.post("/contracts/C0", json={"artifact_id": "INIT-NOTIFY", "body": world.c0_body()},
                    headers=auth(world, "requirements_agent"))
    assert r.status_code == 201
    view = r.json()
    r = client.post(f"/contracts/{view['artifact_id']}/approve",
                    json={"version": view["version"], "digest": view["digest"]}, headers=auth(world, "sponsor"))
    assert r.status_code == 200 and r.json()["decision"] == "APPROVE"
    r = client.post("/transitions/evaluate", headers=auth(world, "orchestrator"), json={
        "work_item_id": world.work_item, "target_stage": "EPIC",
        "refs": {"C0": {"artifact_id": view["artifact_id"], "version": view["version"]}},
        "policy_version": world.svc.policy_version, "idempotency_key": "HTTP-1",
    })
    decision = r.json()
    assert decision["decision"] == "ALLOW"
    r = client.get(f"/audit/{decision['transition_id']}", headers=auth(world, "orchestrator"))
    assert r.json()["evidence"]["C0"]["digest"] == view["digest"]
    assert client.get("/audit/verify", headers=auth(world, "orchestrator")).json()["ok"] is True


def test_requirement_aliases(api):
    world, client = api
    world.advance_to(Stage.EPIC)
    r = client.post("/requirements", json={"artifact_id": "BR-NOTIFY", "body": world.c1_body()},
                    headers=auth(world, "requirements_agent"))
    view = r.json()
    r = client.post(f"/requirements/{view['artifact_id']}/approve",
                    json={"version": 1, "digest": view["digest"]}, headers=auth(world, "eng_lead"))
    assert r.status_code == 403  # engineering cannot approve business meaning
    r = client.post(f"/requirements/{view['artifact_id']}/approve",
                    json={"version": 1, "digest": view["digest"]}, headers=auth(world, "po"))
    assert r.status_code == 200


def test_forged_attestation_is_401(api):
    world, client = api
    world.advance_to(Stage.IMPLEMENTATION)
    world.register_build("BLD-1")
    payload = world.attestation("BLD-1")
    r = client.post("/evidence/ingest", json={"payload": payload, "signature": "AAAA"})
    assert r.status_code == 401 and r.json()["error"] == "INVALID_SIGNATURE"
    r = client.post("/evidence/ingest", json={"payload": payload, "signature": world.sign(payload)})
    assert r.status_code == 201
