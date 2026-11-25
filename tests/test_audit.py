"""Append-only, hash-chained audit ledger and storage-tamper detection.

Tests reaching into ``svc._db`` simulate an attacker with raw database access;
the service itself never exposes that handle.
"""

import json
import sqlite3

import pytest

from aidlc_g.lifecycle import Stage
from aidlc_g.testkit import World, expect_error


def test_chain_verifies_after_full_lifecycle(world: World):
    world.advance_to(Stage.RELEASE_READY)
    result = world.svc.verify_audit_chain()
    assert result["ok"] and result["entries"] > 30


def test_denied_commands_are_recorded(world: World):
    view = world.submit("requirements_agent", "C0", "INIT-NOTIFY", world.c0_body())
    expect_error(lambda: world.approve("po", view))
    denied = [e for e in world.svc.audit_log(world.admin) if e["event_type"] == "COMMAND_DENIED"]
    assert denied[-1]["actor_id"] == "po" and denied[-1]["payload"]["error"] == "ROLE_NOT_PERMITTED"


def test_transition_decision_is_retrievable_with_audit_hash(world: World):
    world.make_c0()
    decision = world.transition(Stage.EPIC)
    stored = world.svc.get_transition(world.admin, decision["transition_id"])
    assert stored["decision"] == decision["decision"]
    hashes = {e["entry_hash"] for e in world.svc.audit_log(world.admin)}
    assert stored["audit_entry_hash"] in hashes


def test_tokens_never_stored_or_logged_in_plaintext(world: World):
    token = world.tok("po")
    dump = "\n".join(world.svc._db.iterdump())
    assert token not in dump


def test_tampering_detected_even_without_triggers(world: World):
    world.make_c0()
    db = world.svc._db
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit SET payload_json = ? WHERE seq = 3", (json.dumps({"forged": True}),))
    result = world.svc.verify_audit_chain()
    assert not result["ok"] and result["first_bad_seq"] == 3


def test_tampered_contract_body_fails_closed_at_gate(world: World):
    world.make_c0()
    db = world.svc._db
    db.execute("DROP TRIGGER artifacts_immutable")
    db.execute("UPDATE artifacts SET body_json = replace(body_json, 'optional', 'all') WHERE artifact_id = 'INIT-NOTIFY'")
    decision = world.transition(Stage.EPIC)
    assert [r["code"] for r in decision["reasons"]] == ["INTEGRITY_FAILURE"]


def test_invalidated_evidence_cannot_be_revalidated(world: World):
    world.make_c0()
    world.submit("requirements_agent", "C0", "INIT-NOTIFY", world.c0_body(objective="revised"))
    with pytest.raises(sqlite3.DatabaseError, match="revalidated"):
        world.svc._db.execute("UPDATE approvals SET valid = 1")
