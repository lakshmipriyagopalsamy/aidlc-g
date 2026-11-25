"""M1 trusted evidence-gate service.

Agents and humans interact only through these commands; nothing outside the
service holds a reference to mutable state. Every command authenticates the
caller from a server-issued token and derives roles from the server's own
principal registry, never from caller-supplied strings (section 23.1).

Transition rule (section 10):
    Allow(T) = PolicySatisfied AND ActorAuthorized AND RequiredEvidencePresent
               AND EvidenceIndependentAndAuthentic AND HumanApprovalValid
               AND EvidenceCurrentForArtifactVersions
Any missing, unknown, stale or revoked term yields PAUSE or BLOCK.
"""

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

from pydantic import ValidationError

from .canonical import CanonicalizationError, digest
from .stacks import DEFAULT_STACK_DIGEST
from .contracts import (
    CONTRACT_MODELS,
    ArtifactRef,
    AttestationPayload,
    HealthCheck,
    IncidentDecision,
    IncidentPacket,
    IncidentResolution,
    Postmortem,
    ProvenancePayload,
    QualityAttestationPayload,
    C3AcceptanceManifest,
    ContractType,
    ReviewFinding,
)
from .crypto import PROVENANCE_DOMAIN, QUALITY_DOMAIN, validate_public_key, verify_payload
from .errors import (
    AuthenticationError,
    AuthorizationError,
    Conflict,
    GovernanceError,
    NotFound,
    ValidationFailure,
)
from .lifecycle import ALL_SLOTS, CONTRACT_SLOTS, STAGE_ORDER, Stage, gate_slots, next_stage, requires_review
from .policy import DEFAULT_POLICY, HUMAN, PRINCIPAL_KINDS, Policy

ALLOW = "ALLOW"
PAUSE = "PAUSE_FOR_HUMAN_DECISION"
BLOCK = "BLOCK_WITH_REASONS"

GENESIS_HASH = "sha256:" + "0" * 64
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_BUILD_NODE = "#build/"  # '#' cannot occur in an artifact id, so no collisions

_SCHEMA = """
CREATE TABLE principals (
    principal_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, kind TEXT NOT NULL,
    roles_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE tokens (
    token_hash TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(principal_id));
CREATE TABLE runners (
    runner_id TEXT PRIMARY KEY, public_key TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE artifacts (
    artifact_id TEXT NOT NULL, version INTEGER NOT NULL, contract_type TEXT NOT NULL,
    body_json TEXT NOT NULL, digest TEXT NOT NULL, author_id TEXT NOT NULL,
    created_at TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY (artifact_id, version));
CREATE TABLE edges (
    from_artifact TEXT NOT NULL, from_version INTEGER NOT NULL,
    to_node TEXT NOT NULL, to_version INTEGER NOT NULL);
CREATE INDEX edges_to ON edges (to_node, to_version);
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, version INTEGER NOT NULL,
    digest TEXT NOT NULL, approver_id TEXT NOT NULL, approver_role TEXT NOT NULL,
    decision TEXT NOT NULL, rationale TEXT, policy_version TEXT NOT NULL,
    timestamp TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1, invalidated_reason TEXT,
    UNIQUE (artifact_id, version, approver_id));
CREATE TABLE builds (
    build_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_revision TEXT NOT NULL,
    build_digest TEXT NOT NULL, manifest_id TEXT NOT NULL, manifest_version INTEGER NOT NULL,
    manifest_digest TEXT NOT NULL, registered_by TEXT NOT NULL, created_at TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1, invalidated_reason TEXT);
CREATE TABLE attestations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, attestation_id TEXT NOT NULL UNIQUE,
    runner_id TEXT NOT NULL, build_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    signature TEXT NOT NULL, received_at TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1, invalidated_reason TEXT);
CREATE TABLE quality_attestations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, attestation_id TEXT NOT NULL UNIQUE,
    runner_id TEXT NOT NULL, build_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    signature TEXT NOT NULL, received_at TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1, invalidated_reason TEXT);
CREATE TRIGGER quality_immutable BEFORE UPDATE OF
    attestation_id, runner_id, build_id, payload_json, signature ON quality_attestations
    BEGIN SELECT RAISE(ABORT, 'attestations are immutable'); END;
CREATE TABLE provenance (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, attestation_id TEXT NOT NULL UNIQUE,
    runner_id TEXT NOT NULL, build_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    signature TEXT NOT NULL, received_at TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1, invalidated_reason TEXT);
CREATE TRIGGER provenance_immutable BEFORE UPDATE OF
    attestation_id, runner_id, build_id, payload_json, signature ON provenance
    BEGIN SELECT RAISE(ABORT, 'attestations are immutable'); END;
CREATE TABLE deployments (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT, environment TEXT NOT NULL,
    event TEXT NOT NULL, artifact_digest TEXT NOT NULL, restored_digest TEXT, metrics_json TEXT,
    verdict TEXT, failed_json TEXT, recorded_by TEXT NOT NULL, timestamp TEXT NOT NULL);
CREATE TRIGGER deployments_no_update BEFORE UPDATE ON deployments
    BEGIN SELECT RAISE(ABORT, 'deployment records are append-only'); END;
CREATE TRIGGER deployments_no_delete BEFORE DELETE ON deployments
    BEGIN SELECT RAISE(ABORT, 'deployment records are append-only'); END;
CREATE TABLE incidents (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, event TEXT NOT NULL,
    payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, recorded_by TEXT NOT NULL,
    recorder_kind TEXT NOT NULL, timestamp TEXT NOT NULL);
-- A packet may be refreshed (each on-call question re-investigates) until a human decides;
-- every other step happens once.
CREATE UNIQUE INDEX incidents_single_step ON incidents (incident_id, event) WHERE event != 'PACKET';
CREATE TRIGGER incidents_no_update BEFORE UPDATE ON incidents
    BEGIN SELECT RAISE(ABORT, 'incident records are append-only'); END;
CREATE TRIGGER incidents_no_delete BEFORE DELETE ON incidents
    BEGIN SELECT RAISE(ABORT, 'incident records are append-only'); END;
CREATE TABLE reviews (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE,
    build_id TEXT NOT NULL, reviewer_id TEXT NOT NULL, reviewer_kind TEXT NOT NULL,
    decision TEXT NOT NULL, findings_json TEXT NOT NULL, policy_version TEXT NOT NULL,
    timestamp TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1, invalidated_reason TEXT);
CREATE TABLE risk_declarations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, work_item_id TEXT NOT NULL, tier TEXT NOT NULL,
    rationale TEXT NOT NULL, declared_by TEXT NOT NULL, timestamp TEXT NOT NULL);
CREATE TRIGGER risk_no_update BEFORE UPDATE ON risk_declarations
    BEGIN SELECT RAISE(ABORT, 'risk declarations are append-only'); END;
CREATE TRIGGER risk_no_delete BEFORE DELETE ON risk_declarations
    BEGIN SELECT RAISE(ABORT, 'risk declarations are append-only'); END;
CREATE TABLE work_items (
    work_item_id TEXT PRIMARY KEY, stage TEXT NOT NULL, bindings_json TEXT NOT NULL,
    opened_by TEXT NOT NULL, last_allow_refs_json TEXT);
CREATE TABLE transitions (
    transition_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL, requester_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL, decision TEXT NOT NULL, record_json TEXT NOT NULL);
CREATE TABLE audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, actor_id TEXT,
    payload_json TEXT NOT NULL, timestamp TEXT NOT NULL,
    prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER transitions_no_update BEFORE UPDATE ON transitions
    BEGIN SELECT RAISE(ABORT, 'transition decisions are immutable'); END;
CREATE TRIGGER transitions_no_delete BEFORE DELETE ON transitions
    BEGIN SELECT RAISE(ABORT, 'transition decisions are immutable'); END;
CREATE TRIGGER artifacts_immutable BEFORE UPDATE OF
    artifact_id, version, contract_type, body_json, digest, author_id, created_at ON artifacts
    BEGIN SELECT RAISE(ABORT, 'artifact versions are immutable'); END;
CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts
    BEGIN SELECT RAISE(ABORT, 'artifact versions are immutable'); END;
CREATE TRIGGER approvals_immutable BEFORE UPDATE OF
    approval_id, artifact_id, version, digest, approver_id, approver_role, decision,
    policy_version, timestamp ON approvals
    BEGIN SELECT RAISE(ABORT, 'approval records are immutable'); END;
CREATE TRIGGER builds_immutable BEFORE UPDATE OF
    build_id, kind, source_revision, build_digest, manifest_id, manifest_version,
    manifest_digest, registered_by ON builds
    BEGIN SELECT RAISE(ABORT, 'build registrations are immutable'); END;
CREATE TRIGGER attestations_immutable BEFORE UPDATE OF
    attestation_id, runner_id, build_id, payload_json, signature ON attestations
    BEGIN SELECT RAISE(ABORT, 'attestations are immutable'); END;
CREATE TRIGGER reviews_immutable BEFORE UPDATE OF
    review_id, build_id, reviewer_id, decision, findings_json ON reviews
    BEGIN SELECT RAISE(ABORT, 'reviews are immutable'); END;
"""

# Invalidated evidence can never be made valid again; a new version is required.
for _table in ("approvals", "builds", "attestations", "quality_attestations", "provenance", "reviews"):
    _SCHEMA += (
        f"CREATE TRIGGER {_table}_no_revalidate BEFORE UPDATE OF valid ON {_table} "
        f"WHEN NEW.valid > OLD.valid "
        f"BEGIN SELECT RAISE(ABORT, 'invalidated evidence cannot be revalidated'); END;\n"
        f"CREATE TRIGGER {_table}_no_delete BEFORE DELETE ON {_table} "
        f"BEGIN SELECT RAISE(ABORT, 'evidence records are immutable'); END;\n"
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationFailure("INVALID_IDENTIFIER", f"{field} is not a valid identifier", field=field)
    return value


def _reason(severity: str, code: str, message: str, slot: Optional[str] = None) -> Dict[str, Any]:
    return {"severity": severity, "code": code, "slot": slot, "message": message}


TIERS = ("LOW", "MEDIUM", "HIGH")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: str
    roles: FrozenSet[str]
    active: bool

    def has_any(self, roles: FrozenSet[str]) -> bool:
        return bool(self.roles & roles)


class GovernanceService:
    def __init__(
        self,
        db_path: str = ":memory:",
        policy: Policy = DEFAULT_POLICY,
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        self._policy = policy
        self._clock = clock or utc_now
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        exists = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit'"
        ).fetchone()
        if not exists:
            self._db.executescript(_SCHEMA)

    @classmethod
    def bootstrap(
        cls,
        db_path: str = ":memory:",
        policy: Policy = DEFAULT_POLICY,
        clock: Optional[Callable[[], str]] = None,
        admin_id: str = "admin",
    ) -> Tuple["GovernanceService", str]:
        """Create a service and its first administrator. The token is returned once."""
        service = cls(db_path, policy, clock)
        with service._tx():
            if service._db.execute("SELECT 1 FROM principals LIMIT 1").fetchone():
                raise Conflict("ALREADY_BOOTSTRAPPED", "this store already has principals")
            token = service._insert_principal(admin_id, "Administrator", "service", ["admin"])
            service._audit("BOOTSTRAP", None, {"admin_id": admin_id, "policy": policy.as_dict()})
        return service, token

    @property
    def policy_version(self) -> str:
        return self._policy.version

    @property
    def policy(self) -> Policy:
        return self._policy  # frozen dataclass

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _tx(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _command(
        self, name: str, token: Optional[str], fn: Callable[[Optional[Principal]], Any], authenticate: bool = True
    ) -> Any:
        actor: Optional[Principal] = None
        try:
            with self._tx():
                if authenticate:
                    actor = self._authenticate(token)
                return fn(actor)
        except GovernanceError as exc:
            # Denials are evidence too: record them outside the rolled-back transaction.
            with self._tx():
                self._audit(
                    "COMMAND_DENIED",
                    actor.principal_id if actor else None,
                    {"command": name, "error": exc.code, "message": exc.message},
                )
            raise

    def _audit(self, event_type: str, actor_id: Optional[str], payload: Dict[str, Any]) -> Dict[str, Any]:
        row = self._db.execute("SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = row["entry_hash"] if row else GENESIS_HASH
        entry = {
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "timestamp": self._clock(),
            "prev_hash": prev_hash,
        }
        entry_hash = digest(entry)
        self._db.execute(
            "INSERT INTO audit (event_type, actor_id, payload_json, timestamp, prev_hash, entry_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_type, actor_id, json.dumps(payload, sort_keys=True), entry["timestamp"], prev_hash, entry_hash),
        )
        return {**entry, "entry_hash": entry_hash}

    def _authenticate(self, token: Optional[str]) -> Principal:
        if not isinstance(token, str) or not token:
            raise AuthenticationError("UNAUTHENTICATED", "a bearer token is required")
        row = self._db.execute(
            "SELECT p.* FROM tokens t JOIN principals p ON p.principal_id = t.principal_id "
            "WHERE t.token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            raise AuthenticationError("INVALID_TOKEN", "token is not recognised")
        principal = self._row_principal(row)
        if not principal.active:
            raise AuthenticationError("PRINCIPAL_INACTIVE", f"{principal.principal_id} is deactivated")
        return principal

    @staticmethod
    def _row_principal(row: sqlite3.Row) -> Principal:
        return Principal(row["principal_id"], row["kind"], frozenset(json.loads(row["roles_json"])), bool(row["active"]))

    def _principal(self, principal_id: str) -> Optional[Principal]:
        row = self._db.execute("SELECT * FROM principals WHERE principal_id = ?", (principal_id,)).fetchone()
        return self._row_principal(row) if row else None

    @staticmethod
    def _require_role(actor: Principal, roles: FrozenSet[str], action: str) -> None:
        if not actor.has_any(roles):
            raise AuthorizationError(
                "ROLE_NOT_PERMITTED",
                f"{actor.principal_id} may not {action}; requires one of {sorted(roles)}",
                principal=actor.principal_id,
            )

    def _insert_principal(self, principal_id: str, display_name: str, kind: str, roles: List[str]) -> str:
        token = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO principals (principal_id, display_name, kind, roles_json) VALUES (?, ?, ?, ?)",
            (principal_id, display_name, kind, json.dumps(sorted(set(roles)))),
        )
        self._db.execute("INSERT INTO tokens (token_hash, principal_id) VALUES (?, ?)", (_hash_token(token), principal_id))
        return token

    def whoami(self, token: str) -> Dict[str, Any]:
        """Resolve a token to its server-side identity (used by peer services such as the workspace)."""

        def fn(actor: Principal) -> Dict[str, Any]:
            return {"principal_id": actor.principal_id, "kind": actor.kind, "roles": sorted(actor.roles)}

        return self._command("whoami", token, fn)

    # ------------------------------------------------------------ administration

    def register_principal(
        self, token: str, principal_id: str, display_name: str, kind: str, roles: List[str]
    ) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, frozenset({"admin"}), "register principals")
            _require_id(principal_id, "principal_id")
            if kind not in PRINCIPAL_KINDS:
                raise ValidationFailure("INVALID_KIND", f"kind must be one of {PRINCIPAL_KINDS}")
            for role in roles:
                _require_id(role, "role")
            if self._principal(principal_id):
                raise Conflict("PRINCIPAL_EXISTS", f"{principal_id} already exists")
            issued = self._insert_principal(principal_id, display_name, kind, roles)
            self._audit("PRINCIPAL_REGISTERED", actor.principal_id,
                        {"principal_id": principal_id, "kind": kind, "roles": sorted(set(roles))})
            return {"principal_id": principal_id, "kind": kind, "roles": sorted(set(roles)), "token": issued}

        return self._command("register_principal", token, fn)

    def set_principal_roles(self, token: str, principal_id: str, roles: List[str]) -> Dict[str, Any]:
        """Change authority. Approvals by a principal who loses a role stop counting."""

        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, frozenset({"admin"}), "change roles")
            if not self._principal(principal_id):
                raise NotFound("UNKNOWN_PRINCIPAL", principal_id)
            self._db.execute("UPDATE principals SET roles_json = ? WHERE principal_id = ?",
                             (json.dumps(sorted(set(roles))), principal_id))
            self._audit("PRINCIPAL_ROLES_CHANGED", actor.principal_id,
                        {"principal_id": principal_id, "roles": sorted(set(roles))})
            return {"principal_id": principal_id, "roles": sorted(set(roles))}

        return self._command("set_principal_roles", token, fn)

    def set_principal_active(self, token: str, principal_id: str, active: bool) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, frozenset({"admin"}), "change principal status")
            if not self._principal(principal_id):
                raise NotFound("UNKNOWN_PRINCIPAL", principal_id)
            self._db.execute("UPDATE principals SET active = ? WHERE principal_id = ?", (int(active), principal_id))
            self._audit("PRINCIPAL_STATUS_CHANGED", actor.principal_id, {"principal_id": principal_id, "active": active})
            return {"principal_id": principal_id, "active": active}

        return self._command("set_principal_active", token, fn)

    def register_runner(self, token: str, runner_id: str, public_key: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, frozenset({"admin"}), "register CI runners")
            _require_id(runner_id, "runner_id")
            try:
                validate_public_key(public_key)
            except Exception as exc:
                raise ValidationFailure("INVALID_PUBLIC_KEY", "expected a base64 raw Ed25519 public key") from exc
            if self._db.execute("SELECT 1 FROM runners WHERE runner_id = ?", (runner_id,)).fetchone():
                raise Conflict("RUNNER_EXISTS", runner_id)
            self._db.execute("INSERT INTO runners (runner_id, public_key) VALUES (?, ?)", (runner_id, public_key))
            self._audit("RUNNER_REGISTERED", actor.principal_id, {"runner_id": runner_id, "public_key": public_key})
            return {"runner_id": runner_id}

        return self._command("register_runner", token, fn)

    def revoke_runner(self, token: str, runner_id: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, frozenset({"admin"}), "revoke CI runners")
            cur = self._db.execute("UPDATE runners SET active = 0 WHERE runner_id = ?", (runner_id,))
            if cur.rowcount != 1:
                raise NotFound("UNKNOWN_RUNNER", runner_id)
            self._audit("RUNNER_REVOKED", actor.principal_id, {"runner_id": runner_id})
            return {"runner_id": runner_id, "active": False}

        return self._command("revoke_runner", token, fn)

    # ----------------------------------------------------------------- contracts

    def _artifact(self, artifact_id: str, version: int) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND version = ?", (artifact_id, version)
        ).fetchone()

    def _latest(self, artifact_id: str) -> Optional[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? ORDER BY version DESC LIMIT 1", (artifact_id,)
        ).fetchone()

    def _artifact_view(self, row: sqlite3.Row, **extra: Any) -> Dict[str, Any]:
        state, missing, approvals = self._approval_state(row)
        return {
            "artifact_id": row["artifact_id"],
            "version": row["version"],
            "contract_type": row["contract_type"],
            "digest": row["digest"],
            "author_id": row["author_id"],
            "status": row["status"],
            "approval_state": state,
            "missing_approver_roles": missing,
            "approvals": approvals,
            "body": json.loads(row["body_json"]),
            **extra,
        }

    def _parse_contract(self, ctype: ContractType, body: Any) -> Any:
        try:
            return CONTRACT_MODELS[ctype].model_validate(body)
        except ValidationError as exc:
            raise ValidationFailure(
                "SCHEMA_VIOLATION",
                f"{ctype.value} body failed schema validation",
                errors=json.loads(exc.json(include_url=False, include_context=False)),
            ) from None

    @staticmethod
    def _contract_type(value: Any) -> ContractType:
        try:
            return ContractType(value)
        except ValueError:
            raise ValidationFailure("UNKNOWN_CONTRACT_TYPE", f"{value!r} is not a contract type") from None

    def _require_ref(self, slot: str, ref: ArtifactRef) -> None:
        row = self._artifact(ref.artifact_id, ref.version)
        if row is None:
            raise ValidationFailure("UNKNOWN_REFERENCE", f"{ref.artifact_id}@v{ref.version} does not exist", slot=slot)
        if row["contract_type"] != slot:
            raise ValidationFailure(
                "WRONG_REFERENCE_TYPE", f"{ref.artifact_id} is {row['contract_type']}, expected {slot}", slot=slot
            )
        if row["digest"] != ref.digest:
            raise ValidationFailure("REFERENCE_DIGEST_MISMATCH", f"digest for {ref.artifact_id}@v{ref.version} differs", slot=slot)
        if self._latest(ref.artifact_id)["version"] != ref.version:
            raise ValidationFailure("STALE_REFERENCE", f"{ref.artifact_id}@v{ref.version} has been superseded", slot=slot)

    def submit_contract(self, token: str, contract_type: str, artifact_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Store a new immutable DRAFT version; supersedes and invalidates downstream evidence."""

        def fn(actor: Principal) -> Dict[str, Any]:
            ctype = self._contract_type(contract_type)
            self._require_role(actor, self._policy.authors[ctype], f"author {ctype.value} contracts")
            _require_id(artifact_id, "artifact_id")
            model = self._parse_contract(ctype, body)
            normalized = model.model_dump(mode="json")
            content_digest = digest(normalized)
            latest = self._latest(artifact_id)
            if latest is not None and latest["contract_type"] != ctype.value:
                raise Conflict("ARTIFACT_TYPE_MISMATCH", f"{artifact_id} is a {latest['contract_type']} artifact")
            if latest is not None and latest["digest"] == content_digest and latest["status"] != "INVALIDATED":
                return self._artifact_view(latest, unchanged=True, invalidated=[])
            for slot, ref in model.references().items():
                self._require_ref(slot, ref)
            build_ref = model.build_reference()
            if build_ref is not None:
                build = self._build(build_ref)
                if build is None or build["kind"] != "CANDIDATE":
                    raise ValidationFailure("UNKNOWN_BUILD", f"{build_ref} is not a registered candidate build")
                if not build["valid"]:
                    raise Conflict("EVIDENCE_INVALIDATED", f"build {build_ref} was invalidated")
            version = latest["version"] + 1 if latest else 1
            self._db.execute(
                "INSERT INTO artifacts (artifact_id, version, contract_type, body_json, digest, author_id, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'DRAFT')",
                (artifact_id, version, ctype.value, json.dumps(normalized, sort_keys=True), content_digest,
                 actor.principal_id, self._clock()),
            )
            for ref in model.references().values():
                self._db.execute("INSERT INTO edges VALUES (?, ?, ?, ?)", (artifact_id, version, ref.artifact_id, ref.version))
            if build_ref is not None:
                self._db.execute("INSERT INTO edges VALUES (?, ?, ?, 0)", (artifact_id, version, _BUILD_NODE + build_ref))
            invalidated: List[str] = []
            if latest is not None:
                self._db.execute(
                    "UPDATE artifacts SET status = 'SUPERSEDED' WHERE artifact_id = ? AND version = ?",
                    (artifact_id, latest["version"]),
                )
                self._invalidate_artifact(
                    artifact_id, latest["version"], f"superseded by v{version}", invalidated, set(), mark=False
                )
            self._audit("CONTRACT_SUBMITTED", actor.principal_id, {
                "artifact_id": artifact_id, "version": version, "contract_type": ctype.value,
                "digest": content_digest, "invalidated": invalidated,
            })
            return self._artifact_view(self._artifact(artifact_id, version), unchanged=False, invalidated=invalidated)

        return self._command("submit_contract", token, fn)

    def _invalidate_artifact(
        self, artifact_id: str, version: int, reason: str, out: List[str], seen: Set[str], mark: bool = True
    ) -> None:
        node = f"{artifact_id}@v{version}"
        if node in seen:
            return
        seen.add(node)
        self._db.execute(
            "UPDATE approvals SET valid = 0, invalidated_reason = ? WHERE artifact_id = ? AND version = ? AND valid = 1",
            (reason, artifact_id, version),
        )
        if mark:
            cur = self._db.execute(
                "UPDATE artifacts SET status = 'INVALIDATED' WHERE artifact_id = ? AND version = ? "
                "AND status NOT IN ('SUPERSEDED', 'INVALIDATED')",
                (artifact_id, version),
            )
            if cur.rowcount:
                out.append(node)
        downstream = f"upstream {node} changed"
        for edge in self._db.execute(
            "SELECT from_artifact, from_version FROM edges WHERE to_node = ? AND to_version = ?", (artifact_id, version)
        ).fetchall():
            self._invalidate_artifact(edge["from_artifact"], edge["from_version"], downstream, out, seen)
        for build in self._db.execute(
            "SELECT build_id FROM builds WHERE manifest_id = ? AND manifest_version = ? AND valid = 1",
            (artifact_id, version),
        ).fetchall():
            self._invalidate_build(build["build_id"], downstream, out, seen)

    def _invalidate_build(self, build_id: str, reason: str, out: List[str], seen: Set[str]) -> None:
        node = _BUILD_NODE + build_id
        if node in seen:
            return
        seen.add(node)
        self._db.execute("UPDATE builds SET valid = 0, invalidated_reason = ? WHERE build_id = ?", (reason, build_id))
        for table in ("attestations", "quality_attestations", "provenance", "reviews"):
            self._db.execute(
                f"UPDATE {table} SET valid = 0, invalidated_reason = ? WHERE build_id = ? AND valid = 1",
                (reason, build_id),
            )
        out.append(f"build:{build_id}")
        for edge in self._db.execute(
            "SELECT from_artifact, from_version FROM edges WHERE to_node = ? AND to_version = 0", (node,)
        ).fetchall():
            self._invalidate_artifact(edge["from_artifact"], edge["from_version"], f"build {build_id} invalidated", out, seen)

    def _approval_state(self, row: sqlite3.Row) -> Tuple[str, List[str], List[Dict[str, Any]]]:
        """Return (state, missing roles, counted approvals) for one artifact version."""
        required = self._policy.approvers[ContractType(row["contract_type"])]
        records = self._db.execute(
            "SELECT * FROM approvals WHERE artifact_id = ? AND version = ? AND valid = 1 "
            "AND digest = ? AND policy_version = ? ORDER BY timestamp",
            (row["artifact_id"], row["version"], row["digest"], self._policy.version),
        ).fetchall()
        if any(r["decision"] == "REJECT" for r in records):
            return "REJECTED", list(required), []
        if any(r["decision"] == "REQUEST_CHANGES" for r in records):
            return "RETURNED", list(required), []
        covered: Dict[str, Dict[str, Any]] = {}
        for r in records:
            approver = self._principal(r["approver_id"])
            # Revoked authority (section 8.1 invalidate_on) silently stops counting.
            if approver and approver.active and approver.kind == HUMAN and r["approver_role"] in approver.roles:
                covered.setdefault(r["approver_role"], {
                    "approval_id": r["approval_id"], "approver_id": r["approver_id"],
                    "role": r["approver_role"], "timestamp": r["timestamp"],
                })
        missing = [role for role in required if role not in covered]
        return ("APPROVED" if not missing else "PENDING"), missing, list(covered.values())

    def _refresh_status(self, row: sqlite3.Row) -> None:
        if row["status"] in ("SUPERSEDED", "INVALIDATED"):
            return
        state, _, _ = self._approval_state(row)
        if state == "APPROVED":
            status = "APPROVED_LOCKED" if row["contract_type"] == ContractType.C3.value else "APPROVED"
        elif state in ("REJECTED", "RETURNED"):
            status = state
        else:
            status = "READY_FOR_REVIEW"
        self._db.execute(
            "UPDATE artifacts SET status = ? WHERE artifact_id = ? AND version = ?",
            (status, row["artifact_id"], row["version"]),
        )

    def approve(
        self,
        token: str,
        artifact_id: str,
        version: int,
        artifact_digest: str,
        decision: str = "APPROVE",
        as_role: Optional[str] = None,
        rationale: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an authenticated human decision bound to an exact artifact digest."""

        def fn(actor: Principal) -> Dict[str, Any]:
            row = self._artifact(artifact_id, version)
            if row is None:
                raise NotFound("UNKNOWN_ARTIFACT", f"{artifact_id}@v{version}")
            ctype = ContractType(row["contract_type"])
            if actor.kind != HUMAN:
                raise AuthorizationError("HUMAN_APPROVAL_REQUIRED", f"{actor.principal_id} is a {actor.kind}, not a human")
            if decision not in ("APPROVE", "REQUEST_CHANGES", "REJECT"):
                raise ValidationFailure("INVALID_DECISION", "decision must be APPROVE, REQUEST_CHANGES or REJECT")
            eligible = [r for r in self._policy.approvers[ctype] if r in actor.roles]
            if as_role is not None:
                if as_role not in eligible:
                    raise AuthorizationError("ROLE_NOT_PERMITTED", f"{actor.principal_id} cannot approve {ctype.value} as {as_role}")
                role = as_role
            elif not eligible:
                raise AuthorizationError(
                    "ROLE_NOT_PERMITTED",
                    f"{actor.principal_id} lacks approval authority for {ctype.value}; requires {list(self._policy.approvers[ctype])}",
                )
            elif len(eligible) > 1:
                raise ValidationFailure("AMBIGUOUS_ROLE", f"specify as_role; eligible roles: {eligible}")
            else:
                role = eligible[0]
            if actor.principal_id == row["author_id"]:
                raise AuthorizationError("SELF_APPROVAL", "the author of an artifact version cannot approve it")
            if ctype == ContractType.C3 and actor.has_any(self._policy.implementation_roles):
                raise AuthorizationError("INDEPENDENCE_VIOLATION", "implementers cannot approve the acceptance manifest")
            if self._latest(artifact_id)["version"] != version:
                raise Conflict("STALE_VERSION", f"{artifact_id}@v{version} has been superseded")
            if artifact_digest != row["digest"]:
                raise Conflict("DIGEST_MISMATCH", "the digest presented does not match the stored version")
            if row["status"] in ("SUPERSEDED", "INVALIDATED"):
                raise Conflict("ARTIFACT_INVALIDATED", f"{artifact_id}@v{version} is {row['status']}; submit a new version")
            existing = self._db.execute(
                "SELECT * FROM approvals WHERE artifact_id = ? AND version = ? AND approver_id = ?",
                (artifact_id, version, actor.principal_id),
            ).fetchone()
            if existing is not None:
                if existing["decision"] == decision and existing["approver_role"] == role:
                    return {**dict(existing), "replayed": True}
                raise Conflict("DECISION_ALREADY_RECORDED", f"{actor.principal_id} already decided {existing['decision']}")
            approval_id = "APR-" + uuid.uuid4().hex
            record = {
                "approval_id": approval_id, "artifact_id": artifact_id, "version": version,
                "digest": row["digest"], "approver_id": actor.principal_id, "approver_role": role,
                "decision": decision, "rationale": rationale, "policy_version": self._policy.version,
                "timestamp": self._clock(),
            }
            self._db.execute(
                "INSERT INTO approvals (approval_id, artifact_id, version, digest, approver_id, approver_role, "
                "decision, rationale, policy_version, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(record.values()),
            )
            self._refresh_status(row)
            self._audit("APPROVAL_RECORDED", actor.principal_id, {k: v for k, v in record.items() if k != "rationale"})
            return {**record, "valid": 1, "replayed": False}

        return self._command("approve", token, fn)

    def get_artifact(self, token: str, artifact_id: str, version: Optional[int] = None) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            row = self._latest(artifact_id) if version is None else self._artifact(artifact_id, version)
            if row is None:
                raise NotFound("UNKNOWN_ARTIFACT", artifact_id)
            return self._artifact_view(row)

        return self._command("get_artifact", token, fn)

    # ---------------------------------------------------------- builds, evidence

    def _build(self, build_id: str) -> Optional[sqlite3.Row]:
        return self._db.execute("SELECT * FROM builds WHERE build_id = ?", (build_id,)).fetchone()

    def _manifest_people(self, manifest_id: str, version: int) -> Set[str]:
        """Principals who authored or approved a manifest version."""
        people = {self._artifact(manifest_id, version)["author_id"]}
        people.update(
            r["approver_id"] for r in self._db.execute(
                "SELECT approver_id FROM approvals WHERE artifact_id = ? AND version = ?", (manifest_id, version)
            )
        )
        return people

    def register_build(
        self,
        token: str,
        build_id: str,
        kind: str,
        source_revision: str,
        build_digest: str,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Bind commit + build digest to the locked acceptance manifest it will be tested against."""

        def fn(actor: Principal) -> Dict[str, Any]:
            if kind == "CANDIDATE":
                self._require_role(actor, self._policy.candidate_build_roles, "register candidate builds")
            elif kind == "BASELINE_STUB":
                self._require_role(actor, self._policy.baseline_build_roles, "register baseline stub builds")
            else:
                raise ValidationFailure("INVALID_BUILD_KIND", "kind must be CANDIDATE or BASELINE_STUB")
            _require_id(build_id, "build_id")
            if not isinstance(source_revision, str) or not re.fullmatch(r"[0-9a-f]{7,64}", source_revision):
                raise ValidationFailure("INVALID_REVISION", "source_revision must be a hex commit id")
            if not isinstance(build_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", build_digest):
                raise ValidationFailure("INVALID_DIGEST", "build_digest must be sha256:<hex>")
            try:
                ref = ArtifactRef.model_validate(manifest)
            except ValidationError:
                raise ValidationFailure("INVALID_REFERENCE", "manifest must be {artifact_id, version, digest}") from None
            row = self._artifact(ref.artifact_id, ref.version)
            if row is None or row["contract_type"] != ContractType.C3.value:
                raise ValidationFailure("UNKNOWN_MANIFEST", f"{ref.artifact_id}@v{ref.version} is not a C3 manifest")
            if row["digest"] != ref.digest or self._latest(ref.artifact_id)["version"] != ref.version:
                raise Conflict("STALE_MANIFEST", "builds must bind to the current manifest digest")
            if self._approval_state(row)[0] != "APPROVED" or row["status"] != "APPROVED_LOCKED":
                raise Conflict("MANIFEST_NOT_LOCKED", "the acceptance manifest is not approved and locked")
            if kind == "CANDIDATE" and actor.principal_id in self._manifest_people(ref.artifact_id, ref.version):
                raise AuthorizationError("INDEPENDENCE_VIOLATION", "the implementer authored or approved the acceptance manifest")
            if self._build(build_id) is not None:
                raise Conflict("BUILD_EXISTS", build_id)
            self._db.execute(
                "INSERT INTO builds (build_id, kind, source_revision, build_digest, manifest_id, manifest_version, "
                "manifest_digest, registered_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (build_id, kind, source_revision, build_digest, ref.artifact_id, ref.version, ref.digest,
                 actor.principal_id, self._clock()),
            )
            record = {"build_id": build_id, "kind": kind, "source_revision": source_revision,
                      "build_digest": build_digest, "manifest": ref.model_dump(), "registered_by": actor.principal_id}
            self._audit("BUILD_REGISTERED", actor.principal_id, record)
            return record

        return self._command("register_build", token, fn)

    def get_build(self, token: str, build_id: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            build = self._build(build_id)
            if build is None:
                raise NotFound("UNKNOWN_BUILD", build_id)
            return {k: build[k] for k in build.keys()}

        return self._command("get_build", token, fn)

    def ingest_attestation(self, payload: Dict[str, Any], signature: str) -> Dict[str, Any]:
        """Accept a CI result only if signed by an active registered runner and bound to a known build."""

        def fn(_: None) -> Dict[str, Any]:
            if not isinstance(payload, dict) or not isinstance(signature, str):
                raise ValidationFailure("MALFORMED_ATTESTATION", "payload must be an object and signature a string")
            runner = self._db.execute(
                "SELECT * FROM runners WHERE runner_id = ?", (payload.get("runner_id"),)
            ).fetchone()
            if runner is None or not runner["active"]:
                raise AuthenticationError("UNKNOWN_RUNNER", f"runner {payload.get('runner_id')!r} is not trusted")
            if not verify_payload(runner["public_key"], payload, signature):
                raise AuthenticationError("INVALID_SIGNATURE", "attestation signature does not verify")
            try:
                att = AttestationPayload.model_validate(payload)
            except ValidationError:
                raise ValidationFailure("MALFORMED_ATTESTATION", "attestation payload failed schema validation") from None
            if att.model_dump(mode="json") != payload:
                raise ValidationFailure("NON_CANONICAL_ATTESTATION", "payload contains non-canonical values")
            if self._db.execute("SELECT 1 FROM attestations WHERE attestation_id = ?", (att.attestation_id,)).fetchone():
                raise Conflict("REPLAY_DETECTED", f"attestation {att.attestation_id} was already ingested")
            build = self._build(att.build_id)
            if build is None:
                raise ValidationFailure("UNKNOWN_BUILD", f"build {att.build_id} is not registered")
            if not build["valid"]:
                raise Conflict("EVIDENCE_INVALIDATED", f"build {att.build_id} was invalidated: {build['invalidated_reason']}")
            if (att.build_digest, att.source_revision) != (build["build_digest"], build["source_revision"]):
                raise ValidationFailure("BUILD_BINDING_MISMATCH", "attested commit/build digest differs from the registered build")
            if (att.manifest_id, att.manifest_version, att.manifest_digest) != (
                build["manifest_id"], build["manifest_version"], build["manifest_digest"]
            ):
                raise ValidationFailure("MANIFEST_BINDING_MISMATCH", "attested manifest differs from the build's locked manifest")
            if att.policy_version != self._policy.version:
                raise ValidationFailure("POLICY_VERSION_MISMATCH", f"attestation made under {att.policy_version}")
            manifest = C3AcceptanceManifest.model_validate(
                json.loads(self._artifact(build["manifest_id"], build["manifest_version"])["body_json"])
            )
            unknown = {r.test_id for r in att.results} - {t.test_id for t in manifest.tests}
            if unknown:
                raise ValidationFailure("UNKNOWN_TEST_RESULT", f"results for tests not in the manifest: {sorted(unknown)}")
            self._db.execute(
                "INSERT INTO attestations (attestation_id, runner_id, build_id, payload_json, signature, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (att.attestation_id, att.runner_id, att.build_id, json.dumps(payload, sort_keys=True), signature, self._clock()),
            )
            outcomes: Dict[str, int] = {}
            for r in att.results:
                outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
            record = {"attestation_id": att.attestation_id, "runner_id": att.runner_id,
                      "build_id": att.build_id, "outcomes": outcomes}
            self._audit("ATTESTATION_INGESTED", None, record)
            return record

        return self._command("ingest_attestation", None, fn, authenticate=False)

    def ingest_quality_attestation(self, payload: Dict[str, Any], signature: str) -> Dict[str, Any]:
        """Stage 7 evidence: coverage tests, mutation score and scans, signed by a trusted runner."""

        def fn(_: None) -> Dict[str, Any]:
            if not isinstance(payload, dict) or not isinstance(signature, str):
                raise ValidationFailure("MALFORMED_ATTESTATION", "payload must be an object and signature a string")
            runner = self._db.execute("SELECT * FROM runners WHERE runner_id = ?", (payload.get("runner_id"),)).fetchone()
            if runner is None or not runner["active"]:
                raise AuthenticationError("UNKNOWN_RUNNER", f"runner {payload.get('runner_id')!r} is not trusted")
            if not verify_payload(runner["public_key"], payload, signature, QUALITY_DOMAIN):
                raise AuthenticationError("INVALID_SIGNATURE", "quality attestation signature does not verify")
            try:
                att = QualityAttestationPayload.model_validate(payload)
            except ValidationError:
                raise ValidationFailure("MALFORMED_ATTESTATION", "quality attestation failed schema validation") from None
            if att.model_dump(mode="json") != payload:
                raise ValidationFailure("NON_CANONICAL_ATTESTATION", "payload contains non-canonical values")
            if self._db.execute("SELECT 1 FROM quality_attestations WHERE attestation_id = ?",
                                (att.attestation_id,)).fetchone():
                raise Conflict("REPLAY_DETECTED", f"attestation {att.attestation_id} was already ingested")
            build = self._build(att.build_id)
            if build is None or build["kind"] != "CANDIDATE":
                raise ValidationFailure("UNKNOWN_BUILD", f"{att.build_id} is not a registered candidate build")
            if not build["valid"]:
                raise Conflict("EVIDENCE_INVALIDATED", f"build {att.build_id} was invalidated")
            # The quality run may sit on a later revision (added coverage tests), but the code must be identical.
            if att.build_digest != build["build_digest"]:
                raise ValidationFailure("BUILD_BINDING_MISMATCH", "quality run built different code than the registered build")
            if (att.manifest_id, att.manifest_version, att.manifest_digest) != (
                build["manifest_id"], build["manifest_version"], build["manifest_digest"]
            ):
                raise ValidationFailure("MANIFEST_BINDING_MISMATCH", "quality run names a different manifest")
            if att.policy_version != self._policy.version:
                raise ValidationFailure("POLICY_VERSION_MISMATCH", f"attestation made under {att.policy_version}")
            self._db.execute(
                "INSERT INTO quality_attestations (attestation_id, runner_id, build_id, payload_json, signature, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (att.attestation_id, att.runner_id, att.build_id, json.dumps(payload, sort_keys=True), signature, self._clock()),
            )
            record = {"attestation_id": att.attestation_id, "runner_id": att.runner_id, "build_id": att.build_id,
                      "source_revision": att.source_revision,
                      "checks": {c.kind: c.status for c in att.checks}}
            self._audit("QUALITY_ATTESTATION_INGESTED", None, record)
            return record

        return self._command("ingest_quality_attestation", None, fn, authenticate=False)

    def get_evidence(self, token: str, build_id: str) -> Dict[str, Any]:
        """Latest valid machine and review evidence for a build (input to the PR packet)."""

        def fn(actor: Principal) -> Dict[str, Any]:
            build = self._build(build_id)
            if build is None:
                raise NotFound("UNKNOWN_BUILD", build_id)

            def latest(table: str) -> Optional[Dict[str, Any]]:
                row = self._db.execute(f"SELECT payload_json FROM {table} WHERE build_id = ? AND valid = 1 "
                                       "ORDER BY seq DESC LIMIT 1", (build_id,)).fetchone()
                return json.loads(row["payload_json"]) if row else None

            reviews = [{"review_id": r["review_id"], "reviewer_id": r["reviewer_id"], "reviewer_kind": r["reviewer_kind"],
                        "decision": r["decision"], "findings": json.loads(r["findings_json"])}
                       for r in self._db.execute("SELECT * FROM reviews WHERE build_id = ? AND valid = 1 ORDER BY seq",
                                                 (build_id,))]
            return {"build": {k: build[k] for k in build.keys()}, "attestation": latest("attestations"),
                    "quality": latest("quality_attestations"), "provenance": latest("provenance"), "reviews": reviews}

        return self._command("get_evidence", token, fn)

    def _open_blocking_findings(self, build_id: str) -> List[str]:
        latest: Dict[str, Dict[str, Any]] = {}
        for review in self._db.execute(
            "SELECT findings_json FROM reviews WHERE build_id = ? AND valid = 1 ORDER BY seq", (build_id,)
        ):
            for f in json.loads(review["findings_json"]):
                latest[f["finding_id"]] = f
        return sorted(fid for fid, f in latest.items() if f["severity"] == "BLOCKING" and f["status"] == "OPEN")

    def record_review(
        self, token: str, review_id: str, build_id: str, decision: str, findings: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Human review decision, or agent findings that can raise but never clear issues."""

        def fn(actor: Principal) -> Dict[str, Any]:
            _require_id(review_id, "review_id")
            build = self._build(build_id)
            if build is None or build["kind"] != "CANDIDATE":
                raise NotFound("UNKNOWN_BUILD", f"{build_id} is not a candidate build")
            if not build["valid"]:
                raise Conflict("EVIDENCE_INVALIDATED", f"build {build_id} was invalidated")
            try:
                parsed = [ReviewFinding.model_validate(f) for f in (findings or [])]
            except ValidationError:
                raise ValidationFailure("SCHEMA_VIOLATION", "review findings failed schema validation") from None
            if actor.kind == HUMAN and actor.has_any(self._policy.reviewer_roles):
                if decision not in ("APPROVE", "REQUEST_CHANGES"):
                    raise ValidationFailure("INVALID_DECISION", "human reviews decide APPROVE or REQUEST_CHANGES")
            elif actor.has_any(self._policy.finding_agent_roles):
                if decision != "FINDINGS_ONLY":
                    raise AuthorizationError("HUMAN_APPROVAL_REQUIRED", "review agents may only report findings")
                if any(f.status != "OPEN" for f in parsed):
                    raise AuthorizationError("AGENT_CANNOT_CLEAR_FINDINGS", "review agents cannot resolve or risk-accept findings")
            else:
                self._require_role(actor, self._policy.reviewer_roles | self._policy.finding_agent_roles, "review builds")
            if actor.principal_id == build["registered_by"]:
                raise AuthorizationError("SEPARATION_OF_DUTIES", "the implementer cannot review their own build")
            if self._db.execute("SELECT 1 FROM reviews WHERE review_id = ?", (review_id,)).fetchone():
                raise Conflict("REVIEW_EXISTS", review_id)
            dumped = [f.model_dump(mode="json") for f in parsed]
            self._db.execute(
                "INSERT INTO reviews (review_id, build_id, reviewer_id, reviewer_kind, decision, findings_json, "
                "policy_version, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, build_id, actor.principal_id, actor.kind, decision, json.dumps(dumped),
                 self._policy.version, self._clock()),
            )
            if decision == "APPROVE" and self._open_blocking_findings(build_id):
                raise ValidationFailure(
                    "BLOCKING_FINDINGS_OPEN",
                    f"cannot approve with open blocking findings {self._open_blocking_findings(build_id)}",
                )
            record = {"review_id": review_id, "build_id": build_id, "reviewer_id": actor.principal_id,
                      "decision": decision, "findings": dumped}
            self._audit("REVIEW_RECORDED", actor.principal_id, record)
            return record

        return self._command("record_review", token, fn)

    # ---------------------------------------------------------------- workflow

    def open_work_item(self, token: str, work_item_id: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, frozenset({"orchestrator"}), "open work items")
            _require_id(work_item_id, "work_item_id")
            if self._db.execute("SELECT 1 FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone():
                raise Conflict("WORK_ITEM_EXISTS", work_item_id)
            self._db.execute(
                "INSERT INTO work_items (work_item_id, stage, bindings_json, opened_by) VALUES (?, ?, '{}', ?)",
                (work_item_id, Stage.INITIATIVE.value, actor.principal_id),
            )
            self._audit("WORK_ITEM_OPENED", actor.principal_id, {"work_item_id": work_item_id})
            return {"work_item_id": work_item_id, "stage": Stage.INITIATIVE.value, "bindings": {}}

        return self._command("open_work_item", token, fn)

    def declare_risk_tier(self, token: str, work_item_id: str, tier: str, rationale: str) -> Dict[str, Any]:
        """A person states how risky the change is. Evidence may raise the tier later; nothing lowers it below that."""

        def fn(actor: Principal) -> Dict[str, Any]:
            self._work_item(work_item_id)
            if actor.kind != HUMAN:
                raise AuthorizationError("HUMAN_APPROVAL_REQUIRED", "only a person may declare a risk tier")
            self._require_role(actor, self._policy.risk_declarer_roles, "declare risk tiers")
            if tier not in TIERS:
                raise ValidationFailure("INVALID_RISK_TIER", f"tier must be one of {list(TIERS)}")
            if not isinstance(rationale, str) or not rationale.strip():
                raise ValidationFailure("RATIONALE_REQUIRED", "say why this tier fits the change")
            self._db.execute(
                "INSERT INTO risk_declarations (work_item_id, tier, rationale, declared_by, timestamp) VALUES (?, ?, ?, ?, ?)",
                (work_item_id, tier, rationale, actor.principal_id, self._clock()))
            self._audit("RISK_TIER_DECLARED", actor.principal_id,
                        {"work_item_id": work_item_id, "tier": tier, "rationale": rationale})
            return {"work_item_id": work_item_id, "declared": tier, "declared_by": actor.principal_id}

        return self._command("declare_risk_tier", token, fn)

    def required_approvers(self, token: str, work_item_id: str, contract_type: str) -> Dict[str, Any]:
        """Roles that must decide this contract type now, at the tier the evidence so far supports."""

        def fn(actor: Principal) -> Dict[str, Any]:
            wi = self._work_item(work_item_id)
            refs = json.loads(wi["last_allow_refs_json"] or "{}")
            contracts: Dict[str, Tuple[sqlite3.Row, Any]] = {}
            for slot, ref in refs.items():
                if slot in CONTRACT_SLOTS and isinstance(ref, dict):
                    row = self._artifact(ref["artifact_id"], ref["version"])
                    if row is not None:
                        contracts[slot] = (row, CONTRACT_MODELS[ContractType(slot)].model_validate(json.loads(row["body_json"])))
            risk = self._risk_tier(work_item_id, contracts, refs.get("BUILD"))
            ctype = ContractType(contract_type)
            return {**risk, "roles": list(self._policy.approvers_for(ctype, risk["tier"])),
                    "policy_roles": [r for r in self._policy.approvers[ctype]
                                     if r not in self._policy.approvers_for(ctype, risk["tier"])]}

        return self._command("required_approvers", token, fn)

    def decision_latency(self, token: str, work_item_id: str) -> Dict[str, Any]:
        """Decision latency for one work item, computed only from the gate's own records.

        Proxies (after the decision-latency framing): how long each decision waited in the
        approval queue, review loops (extra versions and returned versions), escalations
        (PAUSE decisions), risk-tier raises, rework churn (invalidated evidence), how many
        decisions people made versus policy rules, and lead time. Approvals of one artifact
        version are treated as parallel: the version waits for its slowest approver.
        """

        def minutes(start: str, end: str) -> int:
            return int((_parse_time(end) - _parse_time(start)).total_seconds() // 60)

        def fn(actor: Principal) -> Dict[str, Any]:
            wi = self._work_item(work_item_id)
            records = [json.loads(r["record_json"]) for r in self._db.execute(
                "SELECT record_json FROM transitions WHERE work_item_id = ? ORDER BY rowid", (work_item_id,))]
            ids = {e["artifact_id"] for r in records for slot, e in r["evidence"].items()
                   if slot in CONTRACT_SLOTS and isinstance(e, dict) and "artifact_id" in e}
            ids |= {r["artifact_id"] for r in json.loads(wi["last_allow_refs_json"] or "{}").values()
                    if isinstance(r, dict) and "artifact_id" in r}
            opened = self._db.execute("SELECT timestamp, payload_json FROM audit WHERE event_type = 'WORK_ITEM_OPENED'")
            opened_at = next((r["timestamp"] for r in opened if json.loads(r["payload_json"])["work_item_id"] == work_item_id),
                             None)
            artifacts, waits, over = [], [], []
            for aid in sorted(ids):
                versions = self._db.execute("SELECT * FROM artifacts WHERE artifact_id = ? ORDER BY version", (aid,)).fetchall()
                approvals = self._db.execute("SELECT * FROM approvals WHERE artifact_id = ? ORDER BY timestamp", (aid,)).fetchall()
                created = {v["version"]: v["created_at"] for v in versions}
                decided = []
                for a in approvals:
                    wait = minutes(created[a["version"]], a["timestamp"])
                    decided.append({"version": a["version"], "role": a["approver_role"], "approver": a["approver_id"],
                                    "decision": a["decision"], "waited_minutes": wait})
                    waits.append(wait)
                    if wait > self._policy.approval_target_minutes:
                        over.append(f"{aid}@v{a['version']} {a['approver_role']} waited {wait} min")
                final = versions[-1]["version"]
                on_final = [d["waited_minutes"] for d in decided if d["version"] == final]
                artifacts.append({
                    "artifact_id": aid, "contract_type": versions[-1]["contract_type"], "versions": len(versions),
                    "review_loops": len(versions) - 1 + sum(d["decision"] != "APPROVE" for d in decided),
                    "human_decisions": len(decided),
                    "decision_wait_minutes": max(on_final) if on_final else 0,
                    "approvals": decided,
                })
            last_allow = next((r for r in reversed(records) if r["decision"] == ALLOW), None)
            # Latest standing: a role authorized by policy earlier but approved by a person after a tier raise counts once, as human.
            latest: Dict[str, Any] = {}
            for r in records:
                if r["decision"] == ALLOW:
                    latest.update({slot: e for slot, e in r["evidence"].items() if isinstance(e, dict)})
            policy = sorted(f"{slot}:{p['role']}" for slot, e in latest.items() for p in e.get("policy_authorizations", []))
            builds = [b["build_id"] for b in self._db.execute("SELECT build_id, manifest_id FROM builds")
                      if b["manifest_id"] in ids]
            reviews = [r for b in builds for r in self._db.execute(
                "SELECT reviewer_kind, timestamp FROM reviews WHERE build_id = ?", (b,)) if r["reviewer_kind"] == HUMAN]
            invalidated = self._db.execute(
                f"SELECT COUNT(*) AS n FROM artifacts WHERE status = 'INVALIDATED' AND artifact_id IN "
                f"({','.join('?' * len(ids)) or 'NULL'})", tuple(sorted(ids))).fetchone()["n"]
            invalid_builds = sum(1 for b in builds if not self._build(b)["valid"])
            declarations = self._db.execute("SELECT tier, declared_by, timestamp FROM risk_declarations "
                                            "WHERE work_item_id = ? ORDER BY seq", (work_item_id,)).fetchall()
            risk = (last_allow or {}).get("evidence", {}).get("RISK") or {"declared": self._declared_tier(work_item_id)}
            return {
                "work_item_id": work_item_id,
                "risk": {"declared": risk.get("declared"), "effective": risk.get("tier", risk.get("declared")),
                         "signals": risk.get("signals", []),
                         "declarations": [dict(d) for d in declarations]},
                "human_decisions": sum(a["human_decisions"] for a in artifacts) + len(reviews) + len(declarations),
                "policy_decisions": len(policy),
                "policy_authorized": policy,
                "approval_queue_minutes": {"total": sum(waits), "max": max(waits, default=0),
                                           "critical_path": sum(a["decision_wait_minutes"] for a in artifacts)},
                "review_loops": sum(a["review_loops"] for a in artifacts),
                "escalations": sum(r["decision"] == PAUSE for r in records),
                "blocks": sum(r["decision"] == BLOCK for r in records),
                "risk_raises": sum(any(x["code"] == "RISK_TIER_RAISED" for x in r["reasons"]) for r in records),
                "rework_churn": invalidated + invalid_builds,
                "lead_time_minutes": minutes(opened_at, last_allow["evaluated_at"]) if opened_at and last_allow else None,
                "over_target": over,
                "target_minutes": self._policy.approval_target_minutes,
                "artifacts": artifacts,
            }

        return self._command("decision_latency", token, fn)

    def get_work_item(self, token: str, work_item_id: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            row = self._work_item(work_item_id)
            return {"work_item_id": work_item_id, "stage": row["stage"], "bindings": json.loads(row["bindings_json"])}

        return self._command("get_work_item", token, fn)

    def _work_item(self, work_item_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
        if row is None:
            raise NotFound("UNKNOWN_WORK_ITEM", work_item_id)
        return row

    def evaluate_transition(
        self,
        token: str,
        work_item_id: str,
        target_stage: str,
        refs: Dict[str, Any],
        policy_version: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Fail-closed decision: ALLOW, PAUSE_FOR_HUMAN_DECISION or BLOCK_WITH_REASONS.

        Idempotent per key: a retried request returns the original decision and
        commits nothing new; reusing a key for a different request is refused.
        """
        try:
            fingerprint = digest({"work_item_id": work_item_id, "target_stage": target_stage,
                                  "refs": refs, "policy_version": policy_version})
        except CanonicalizationError as exc:
            raise ValidationFailure("MALFORMED_REQUEST", str(exc)) from None

        def fn(actor: Principal) -> Dict[str, Any]:
            _require_id(idempotency_key, "idempotency_key")
            prior = self._db.execute("SELECT * FROM transitions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["request_fingerprint"] != fingerprint or prior["requester_id"] != actor.principal_id:
                    raise Conflict("IDEMPOTENCY_KEY_REUSED", "this key was used for a different request")
                return {**json.loads(prior["record_json"]), "replayed": True}
            wi = self._work_item(work_item_id)
            current = Stage(wi["stage"])
            bindings = json.loads(wi["bindings_json"])
            reasons: List[Dict[str, Any]] = []
            evidence: Dict[str, Any] = {}
            target: Optional[Stage]
            try:
                target = Stage(target_stage)
            except ValueError:
                target = None
                reasons.append(_reason("BLOCK", "INVALID_TRANSITION", f"unknown stage {target_stage!r}"))
            if target is not None:
                if not actor.has_any(self._policy.transition_requesters[target]):
                    reasons.append(_reason("BLOCK", "ACTOR_NOT_AUTHORIZED",
                                           f"{actor.principal_id} may not request promotion to {target.value}"))
                elif next_stage(current) != target:
                    reasons.append(_reason("BLOCK", "INVALID_TRANSITION",
                                           f"work item is at {current.value}; cannot move to {target.value}"))
                elif policy_version != self._policy.version:
                    reasons.append(_reason("BLOCK", "POLICY_VERSION_MISMATCH",
                                           f"request names {policy_version!r}; active policy is {self._policy.version!r}"))
                elif not isinstance(refs, dict):
                    reasons.append(_reason("BLOCK", "MALFORMED_REQUEST", "refs must be an object"))
                else:
                    self._evaluate_gate(target, refs, bindings, reasons, evidence, work_item_id)
            decision = self._decide(reasons)
            if decision == ALLOW:
                for slot in gate_slots(target):
                    if slot in CONTRACT_SLOTS:
                        bindings[slot] = refs[slot]["artifact_id"]
                cur = self._db.execute(
                    "UPDATE work_items SET stage = ?, bindings_json = ?, last_allow_refs_json = ? "
                    "WHERE work_item_id = ? AND stage = ?",
                    (target.value, json.dumps(bindings, sort_keys=True), json.dumps(refs, sort_keys=True),
                     work_item_id, current.value),
                )
                if cur.rowcount != 1:  # pragma: no cover - serialized by the transaction lock
                    raise Conflict("CONCURRENT_TRANSITION", "work item changed during evaluation")
            record = {
                "transition_id": "TRN-" + uuid.uuid4().hex,
                "idempotency_key": idempotency_key,
                "work_item_id": work_item_id,
                "from_stage": current.value,
                "to_stage": target_stage,
                "decision": decision,
                "reasons": reasons,
                "evidence": evidence,
                "policy_version": self._policy.version,
                "policy_digest": self._policy.digest,
                "requester_id": actor.principal_id,
                "evaluated_at": self._clock(),
            }
            audit = self._audit("TRANSITION_EVALUATED", actor.principal_id, {
                "transition_id": record["transition_id"], "work_item_id": work_item_id,
                "from_stage": current.value, "to_stage": target_stage, "decision": decision,
                "reason_codes": [r["code"] for r in reasons], "evidence_digest": digest(evidence),
            })
            record["audit_entry_hash"] = audit["entry_hash"]
            self._db.execute(
                "INSERT INTO transitions (transition_id, idempotency_key, request_fingerprint, requester_id, "
                "work_item_id, decision, record_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["transition_id"], idempotency_key, fingerprint, actor.principal_id, work_item_id,
                 decision, json.dumps(record, sort_keys=True)),
            )
            return {**record, "replayed": False}

        return self._command("evaluate_transition", token, fn)

    def evaluate_merge(
        self,
        token: str,
        work_item_id: str,
        branch: str,
        head_revision: str,
        tested_revision: str,
        changed_paths: List[str],
        refs: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Protected-branch decision: may this branch head become main?

        Re-verifies all evidence up to the work item's stage, requires approved
        documentation for the tested build, and allows only policy-listed paths
        (docs/) to differ between the last independently tested revision and the
        head. ``changed_paths`` is computed by the merge service, a trusted
        platform principal; the workspace re-checks the decision before merging.
        """
        try:
            fingerprint = digest({"work_item_id": work_item_id, "branch": branch, "head_revision": head_revision,
                                  "tested_revision": tested_revision, "changed_paths": changed_paths, "refs": refs,
                                  "kind": "MERGE"})
        except CanonicalizationError as exc:
            raise ValidationFailure("MALFORMED_REQUEST", str(exc)) from None

        def fn(actor: Principal) -> Dict[str, Any]:
            _require_id(idempotency_key, "idempotency_key")
            prior = self._db.execute("SELECT * FROM transitions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["request_fingerprint"] != fingerprint or prior["requester_id"] != actor.principal_id:
                    raise Conflict("IDEMPOTENCY_KEY_REUSED", "this key was used for a different request")
                return {**json.loads(prior["record_json"]), "replayed": True}
            wi = self._work_item(work_item_id)
            stage = Stage(wi["stage"])
            reasons: List[Dict[str, Any]] = []
            evidence: Dict[str, Any] = {}
            if not actor.has_any(self._policy.merge_requesters):
                reasons.append(_reason("BLOCK", "ACTOR_NOT_AUTHORIZED", f"{actor.principal_id} may not request merges"))
            elif STAGE_ORDER.index(stage) < STAGE_ORDER.index(Stage.DOCUMENTATION) or wi["last_allow_refs_json"] is None:
                reasons.append(_reason("BLOCK", "INVALID_TRANSITION",
                                       f"work item is at {stage.value}; coverage and review gates have not passed"))
            elif not isinstance(refs, dict) or not isinstance(changed_paths, list):
                reasons.append(_reason("BLOCK", "MALFORMED_REQUEST", "refs must be an object and changed_paths a list"))
            else:
                self._evaluate_gate(stage, json.loads(wi["last_allow_refs_json"]), json.loads(wi["bindings_json"]),
                                    reasons, evidence, work_item_id)
                quality = evidence.get("QUALITY")
                if quality is not None and tested_revision != quality["source_revision"]:
                    reasons.append(_reason("BLOCK", "STALE_HEAD", "diff base is not the last independently tested revision"))
                outside = sorted(p for p in changed_paths
                                 if not any(str(p).startswith(x) for x in self._policy.post_test_change_prefixes))
                if outside:
                    reasons.append(_reason("BLOCK", "STALE_HEAD", f"changed after the last independent test run: {outside}"))
                if "DOCS" not in refs:
                    reasons.append(_reason("BLOCK", "MISSING_EVIDENCE", "approved user and operational docs are required", "DOCS"))
                else:
                    docs = self._check_contract("DOCS", refs["DOCS"], reasons, evidence)
                    if docs is not None:
                        self._require_approvals("DOCS", docs[0], evidence["RISK"], reasons, evidence)
                    build = evidence.get("BUILD")
                    if docs is not None and build is not None and docs[1].build_id != build["build_id"]:
                        reasons.append(_reason("BLOCK", "REF_MISMATCH", "documentation describes a different build", "DOCS"))
            decision = self._decide(reasons)
            record = {
                "transition_id": "MRG-" + uuid.uuid4().hex, "idempotency_key": idempotency_key,
                "work_item_id": work_item_id, "from_stage": stage.value, "to_stage": "MERGE", "branch": branch,
                "head_revision": head_revision, "tested_revision": tested_revision, "decision": decision,
                "reasons": reasons, "evidence": evidence, "policy_version": self._policy.version,
                "policy_digest": self._policy.digest, "requester_id": actor.principal_id, "evaluated_at": self._clock(),
            }
            audit = self._audit("MERGE_EVALUATED", actor.principal_id, {
                "transition_id": record["transition_id"], "work_item_id": work_item_id, "branch": branch,
                "head_revision": head_revision, "decision": decision, "reason_codes": [r["code"] for r in reasons],
            })
            record["audit_entry_hash"] = audit["entry_hash"]
            self._db.execute(
                "INSERT INTO transitions (transition_id, idempotency_key, request_fingerprint, requester_id, "
                "work_item_id, decision, record_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["transition_id"], idempotency_key, fingerprint, actor.principal_id, work_item_id,
                 decision, json.dumps(record, sort_keys=True)),
            )
            return {**record, "replayed": False}

        return self._command("evaluate_merge", token, fn)

    @staticmethod
    def _decide(reasons: List[Dict[str, Any]]) -> str:
        if any(r["severity"] == "BLOCK" for r in reasons):
            return BLOCK
        if reasons:
            return PAUSE
        return ALLOW

    # ------------------------------------------------------------- gate checks

    def _evaluate_gate(
        self, target: Stage, refs: Dict[str, Any], bindings: Dict[str, str],
        reasons: List[Dict[str, Any]], evidence: Dict[str, Any], work_item_id: Optional[str] = None,
    ) -> None:
        slots = gate_slots(target)
        unknown = sorted(set(refs) - set(ALL_SLOTS))
        if unknown:
            reasons.append(_reason("BLOCK", "MALFORMED_REQUEST", f"unknown evidence slots {unknown}"))
            return
        for slot in slots:
            if slot not in refs:
                reasons.append(_reason("BLOCK", "MISSING_EVIDENCE", f"{slot} evidence is required to enter {target.value}", slot))

        contracts: Dict[str, Tuple[sqlite3.Row, Any]] = {}
        for slot in slots:
            if slot in CONTRACT_SLOTS and slot in refs:
                ref = refs[slot]
                if isinstance(ref, dict) and slot in bindings and ref.get("artifact_id") != bindings[slot]:
                    reasons.append(_reason("BLOCK", "BINDING_MISMATCH",
                                           f"work item is bound to {bindings[slot]}, not {ref.get('artifact_id')}", slot))
                    continue
                checked = self._check_contract(slot, ref, reasons, evidence)
                if checked is not None:
                    contracts[slot] = checked

        # Every derived contract must point at exactly the upstream versions under evaluation.
        for slot, (_, model) in contracts.items():
            for up_slot, up_ref in model.references().items():
                if up_slot in contracts:
                    up_row = contracts[up_slot][0]
                    if (up_ref.artifact_id, up_ref.version, up_ref.digest) != (
                        up_row["artifact_id"], up_row["version"], up_row["digest"]
                    ):
                        reasons.append(_reason("BLOCK", "REF_MISMATCH",
                                               f"{slot} derives from {up_ref.artifact_id}@v{up_ref.version}, "
                                               f"not the {up_slot} under evaluation", slot))

        self._check_contract_content(contracts, reasons)

        risk = self._risk_tier(work_item_id, contracts, refs.get("BUILD"))
        evidence["RISK"] = risk
        for slot, (row, _) in contracts.items():
            self._require_approvals(slot, row, risk, reasons, evidence)

        c3 = contracts.get("C3")
        interface = frozenset(contracts["C2"][1].domain_interface) if "C2" in contracts else None
        stack = contracts["C2"][1].stack if "C2" in contracts else None
        if "BASELINE" in slots and "BASELINE" in refs and c3 is not None and self._policy.require_failing_baseline:
            self._check_build("BASELINE", refs["BASELINE"], c3, reasons, evidence, interface, stack)
        build = None
        if "BUILD" in slots and "BUILD" in refs and c3 is not None:
            build = self._check_build("BUILD", refs["BUILD"], c3, reasons, evidence, interface, stack)
        if build is not None and requires_review(target):
            if self._policy.require_quality_evidence:
                self._check_quality(build, c3, reasons, evidence, stack)
            self._check_review(build, reasons, evidence)
        if build is not None and "DOCS" in contracts:
            if contracts["DOCS"][1].build_id != build["build_id"]:
                reasons.append(_reason("BLOCK", "REF_MISMATCH", "documentation describes a different build", "DOCS"))
        if build is not None and "RELEASE" in contracts:
            rel = contracts["RELEASE"][1]
            if (rel.build_id, rel.build_digest, rel.source_revision) != (
                build["build_id"], build["build_digest"], build["source_revision"]
            ):
                reasons.append(_reason("BLOCK", "RELEASE_BUILD_MISMATCH",
                                       "release record does not point at the exact tested build", "RELEASE"))
            if self._policy.require_release_provenance:
                self._check_release_provenance(build, rel, reasons, evidence)

    def _check_contract(
        self, slot: str, ref: Any, reasons: List[Dict[str, Any]], evidence: Dict[str, Any]
    ) -> Optional[Tuple[sqlite3.Row, Any]]:
        if not isinstance(ref, dict) or not isinstance(ref.get("artifact_id"), str) or not isinstance(ref.get("version"), int):
            reasons.append(_reason("BLOCK", "MALFORMED_REQUEST", "expected {artifact_id, version[, digest]}", slot))
            return None
        row = self._artifact(ref["artifact_id"], ref["version"])
        if row is None:
            reasons.append(_reason("BLOCK", "UNKNOWN_ARTIFACT", f"{ref['artifact_id']}@v{ref['version']} not found", slot))
            return None
        if row["contract_type"] != slot:
            reasons.append(_reason("BLOCK", "WRONG_ARTIFACT_TYPE", f"{row['artifact_id']} is {row['contract_type']}", slot))
            return None
        if "digest" in ref and ref["digest"] != row["digest"]:
            reasons.append(_reason("BLOCK", "DIGEST_MISMATCH", "presented digest differs from stored version", slot))
            return None
        body = json.loads(row["body_json"])
        if digest(body) != row["digest"]:
            reasons.append(_reason("BLOCK", "INTEGRITY_FAILURE", "stored content no longer matches its digest", slot))
            return None
        latest = self._latest(row["artifact_id"])
        if latest["version"] != row["version"]:
            reasons.append(_reason("BLOCK", "STALE_VERSION",
                                   f"v{row['version']} superseded by v{latest['version']}", slot))
            return None
        if row["status"] == "INVALIDATED":
            reasons.append(_reason("BLOCK", "EVIDENCE_INVALIDATED",
                                   f"{row['artifact_id']}@v{row['version']} depends on a changed upstream contract", slot))
            return None
        model = CONTRACT_MODELS[ContractType(slot)].model_validate(body)
        state, missing, approvals = self._approval_state(row)
        if state in ("REJECTED", "RETURNED"):
            reasons.append(_reason("BLOCK", "ARTIFACT_RETURNED", f"{row['artifact_id']}@v{row['version']} was {state}", slot))
        # Which approvals are required depends on the work item's risk tier: see _require_approvals.
        evidence[slot] = {"artifact_id": row["artifact_id"], "version": row["version"],
                          "digest": row["digest"], "approvals": approvals}
        return row, model

    def _require_approvals(self, slot: str, row: sqlite3.Row, risk: Dict[str, Any],
                           reasons: List[Dict[str, Any]], evidence: Dict[str, Any]) -> None:
        """Human approvals the tier needs. Roles it does not need get a policy authorization: the gate applies
        a versioned rule and records it with the tier evidence. That is never recorded as a human approval."""
        state, _, approvals = self._approval_state(row)
        if state in ("REJECTED", "RETURNED"):
            return
        ctype = ContractType(row["contract_type"])
        covered = {a["role"] for a in approvals}
        required = self._policy.approvers_for(ctype, risk["tier"])
        missing = [r for r in required if r not in covered]
        if missing:
            needed_before = self._policy.approvers_for(ctype, risk["declared"])
            reason = _reason("PAUSE", "APPROVAL_MISSING", f"awaiting approval from {missing} (risk tier {risk['tier']})", slot)
            reason["roles"] = missing
            reason["artifact"] = {"artifact_id": row["artifact_id"], "version": row["version"]}
            reasons.append(reason)
            if any(r not in needed_before for r in missing):
                reasons.append(_reason("PAUSE", "RISK_TIER_RAISED",
                                       f"evidence raised the tier from {risk['declared']} to {risk['tier']} "
                                       f"({', '.join(s['code'] for s in risk['signals'])}); {missing} must now decide", slot))
        policy = [r for r in self._policy.approvers[ctype] if r not in required and r not in covered]
        if slot in evidence:
            evidence[slot]["policy_authorizations"] = [{"role": r, "rule": f"tier:{risk['tier']}:{ctype.value}"}
                                                       for r in policy]

    def _declared_tier(self, work_item_id: Optional[str]) -> str:
        row = None if work_item_id is None else self._db.execute(
            "SELECT tier FROM risk_declarations WHERE work_item_id = ? ORDER BY seq DESC LIMIT 1", (work_item_id,)).fetchone()
        return row["tier"] if row else self._policy.default_risk_tier

    def _risk_tier(self, work_item_id: Optional[str], contracts: Dict[str, Tuple[sqlite3.Row, Any]],
                   build_id: Any) -> Dict[str, Any]:
        """Effective tier: the declared one, raised (never lowered) by what the evidence shows."""
        declared = self._declared_tier(work_item_id)
        signals: List[Dict[str, str]] = []
        c2 = contracts.get("C2", (None, None))[1]
        if c2 is not None and c2.threat_model:
            signals.append({"tier": "HIGH", "code": "SECURITY_RELEVANT_DESIGN", "detail": "C2 lists threats"})
        if c2 is not None and c2.stack is not None and c2.stack.quality_waivers:
            signals.append({"tier": "MEDIUM", "code": "QUALITY_CHECK_WAIVED", "detail": ", ".join(c2.stack.quality_waivers)})
        if isinstance(build_id, str):
            q = self._db.execute("SELECT payload_json FROM quality_attestations WHERE build_id = ? AND valid = 1 "
                                 "ORDER BY seq DESC LIMIT 1", (build_id,)).fetchone()
            if q is not None:
                for c in json.loads(q["payload_json"])["checks"]:
                    files = c.get("metrics", {}).get("files", 0) if c["kind"] == "SECRET_SCAN" else 0
                    if files > self._policy.low_risk_max_changed_files:
                        signals.append({"tier": "MEDIUM", "code": "LARGE_CHANGE", "detail": f"{files} changed files"})
            findings = [f for r in self._db.execute("SELECT findings_json FROM reviews WHERE build_id = ? AND valid = 1",
                                                    (build_id,)) for f in json.loads(r["findings_json"])]
            if any(f["severity"] == "BLOCKING" for f in findings):
                signals.append({"tier": "HIGH", "code": "BLOCKING_FINDING_RAISED", "detail": "a blocking review finding"})
            if any(f["status"] == "RISK_ACCEPTED" for f in findings):
                signals.append({"tier": "MEDIUM", "code": "RISK_ACCEPTED", "detail": "a reviewer accepted a known risk"})
        tier = max([declared, *(s["tier"] for s in signals)], key=TIERS.index)
        return {"declared": declared, "tier": tier, "signals": signals}

    def _check_contract_content(self, contracts: Dict[str, Tuple[sqlite3.Row, Any]], reasons: List[Dict[str, Any]]) -> None:
        for slot, field in (("C0", "open_questions"), ("C1", "unknowns")):
            if slot in contracts:
                blocking = [i.item_id for i in getattr(contracts[slot][1], field) if i.blocking and i.status == "OPEN"]
                if blocking:
                    reasons.append(_reason("PAUSE", "OPEN_BLOCKING_DECISION", f"unresolved decisions {blocking}", slot))
        if "C2" in contracts and contracts["C2"][1].new_product_decisions_required:
            reasons.append(_reason("BLOCK", "RETURN_TO_C1",
                                   "the design requires product decisions; revise and re-approve C1", "C2"))
        if "C2" in contracts and contracts["C2"][1].stack is not None:
            stack = contracts["C2"][1].stack
            owner = self._principal(stack.decided_by)
            if owner is None or owner.kind != HUMAN or not owner.active or not owner.has_any(self._policy.stack_owner_roles):
                reasons.append(_reason("BLOCK", "STACK_OWNER_INVALID",
                                       f"stack {stack.stack_id} must be decided by an active human holding "
                                       f"{sorted(self._policy.stack_owner_roles)}, not {stack.decided_by!r}", "C2"))
        if "C1" not in contracts:
            return
        scenarios = self._check_domain_scenarios(contracts["C1"], reasons)
        if "C2" in contracts:
            designed = {d.scenario_id for d in contracts["C2"][1].scenario_designs}
            unknown = sorted(designed - set(scenarios))
            if unknown:
                reasons.append(_reason("BLOCK", "UNKNOWN_SCENARIO", f"design answers scenarios not in C1: {unknown}", "C2"))
            undesigned = sorted(set(scenarios) - designed)
            if undesigned:
                reasons.append(_reason("PAUSE", "SCENARIO_NOT_DESIGNED",
                                       f"the design does not say how these scenarios are met: {undesigned}", "C2"))
        if "STORIES" in contracts:
            planned = set(contracts["STORIES"][1].scenario_ids())
            unknown = sorted(planned - set(scenarios))
            if unknown:
                reasons.append(_reason("BLOCK", "UNKNOWN_SCENARIO", f"stories plan scenarios not in C1: {unknown}", "STORIES"))
            unplanned = sorted(set(scenarios) - planned)
            if unplanned:
                reasons.append(_reason("BLOCK", "SCENARIO_NOT_PLANNED", f"no story delivers {unplanned}", "STORIES"))
        if "RELEASE" in contracts:
            protected = {s for c in contracts["RELEASE"][1].health_checks for s in c.protects}
            unknown = sorted(protected - set(scenarios))
            if unknown:
                reasons.append(_reason("BLOCK", "UNKNOWN_SCENARIO",
                                       f"health checks protect scenarios not in C1: {unknown}", "RELEASE"))
        if "C3" not in contracts:
            return
        c3_row, manifest = contracts["C3"]
        author = self._principal(c3_row["author_id"])
        if author is None or author.has_any(self._policy.implementation_roles):
            reasons.append(_reason("BLOCK", "INDEPENDENCE_VIOLATION", "acceptance manifest authored by an implementer", "C3"))
        bound = {b.scenario_id: b for b in manifest.scenarios}
        invented = sorted(set(bound) - set(scenarios))
        if invented:
            reasons.append(_reason("BLOCK", "SCENARIO_NOT_IN_C1",
                                   f"tests bind behavior no domain owner decided: {invented}", "C3"))
        # Word for word: the test author may not reinterpret, narrow or weaken a decided scenario.
        altered = sorted(sid for sid, b in bound.items() if sid in scenarios and b.text() != scenarios[sid].text())
        if altered:
            reasons.append(_reason("BLOCK", "SCENARIO_ALTERED", f"scenario text differs from approved C1: {altered}", "C3"))
        uncovered = sorted(set(scenarios) - set(bound))
        if uncovered:
            reasons.append(_reason("BLOCK", "SCENARIO_NOT_COVERED",
                                   f"domain scenarios without an executable acceptance test: {uncovered}", "C3"))
        if manifest.proposed_scenarios:
            reasons.append(_reason("PAUSE", "DOMAIN_PROPOSAL_PENDING",
                                   f"proposed behavior awaits the domain owner: "
                                   f"{[p.proposal_id for p in manifest.proposed_scenarios]}; revise C1 or withdraw", "C3"))
        if self._policy.require_negative_path_test and not any(t.kind in ("NEGATIVE", "BOUNDARY") for t in manifest.tests):
            reasons.append(_reason("BLOCK", "NEGATIVE_PATH_MISSING", "no negative or boundary acceptance test", "C3"))

    def _check_domain_scenarios(
        self, c1: Tuple[sqlite3.Row, Any], reasons: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Domain behavior must be decided by an accountable human, not merely drafted by an agent."""
        row, requirement = c1
        scenarios = requirement.scenarios()
        state, _, _ = self._approval_state(row)
        # Any valid approval of this exact version counts, not only the one that filled the role:
        # two product owners may each own different scenarios.
        approvers = {r["approver_id"] for r in self._db.execute(
            "SELECT approver_id FROM approvals WHERE artifact_id = ? AND version = ? AND valid = 1 "
            "AND digest = ? AND policy_version = ? AND decision = 'APPROVE'",
            (row["artifact_id"], row["version"], row["digest"], self._policy.version),
        ).fetchall()}
        invalid, unaccepted = [], []
        for sid, s in scenarios.items():
            owner = self._principal(s.decided_by)
            if owner is None or owner.kind != HUMAN or not owner.active or not owner.has_any(self._policy.domain_owner_roles):
                invalid.append(sid)
            # Only meaningful once C1 is otherwise approved; until then APPROVAL_MISSING already pauses.
            # It is an independent control: approval by someone else does not stand in for the owner's.
            elif state == "APPROVED" and s.decided_by not in approvers:
                unaccepted.append(sid)
        if invalid:
            reasons.append(_reason("BLOCK", "SCENARIO_OWNER_INVALID",
                                   f"scenarios not owned by an active human domain owner: {sorted(invalid)}", "C1"))
        if unaccepted:
            reasons.append(_reason("PAUSE", "SCENARIO_NOT_ACCEPTED",
                                   f"the named domain owner has not approved this C1 version: {sorted(unaccepted)}", "C1"))
        if self._policy.require_negative_path_test and not any(s.kind in ("NEGATIVE", "BOUNDARY") for s in scenarios.values()):
            reasons.append(_reason("BLOCK", "NEGATIVE_SCENARIO_MISSING",
                                   "the domain owner has not decided any negative or boundary behavior", "C1"))
        return scenarios

    def _check_build(
        self, slot: str, build_id: Any, c3: Tuple[sqlite3.Row, Any],
        reasons: List[Dict[str, Any]], evidence: Dict[str, Any], interface: Optional[FrozenSet[str]] = None,
        stack: Any = None,
    ) -> Optional[sqlite3.Row]:
        expected_kind, expected_outcome = ("BASELINE_STUB", "FAILED") if slot == "BASELINE" else ("CANDIDATE", "PASSED")
        build = self._build(build_id) if isinstance(build_id, str) else None
        if build is None:
            reasons.append(_reason("BLOCK", "UNKNOWN_BUILD", f"build {build_id!r} is not registered", slot))
            return None
        if build["kind"] != expected_kind:
            reasons.append(_reason("BLOCK", "WRONG_BUILD_KIND", f"{build_id} is {build['kind']}, expected {expected_kind}", slot))
            return None
        if not build["valid"]:
            reasons.append(_reason("BLOCK", "EVIDENCE_INVALIDATED", f"build invalidated: {build['invalidated_reason']}", slot))
            return None
        c3_row, manifest = c3
        if (build["manifest_id"], build["manifest_version"], build["manifest_digest"]) != (
            c3_row["artifact_id"], c3_row["version"], c3_row["digest"]
        ):
            reasons.append(_reason("BLOCK", "STALE_BUILD", "build was tested against a different manifest version", slot))
            return None
        if slot == "BUILD":
            people = self._manifest_people(c3_row["artifact_id"], c3_row["version"])
            registrant = self._principal(build["registered_by"])
            if build["registered_by"] in people or registrant is None or not registrant.has_any(self._policy.candidate_build_roles):
                reasons.append(_reason("BLOCK", "INDEPENDENCE_VIOLATION", "implementer is not independent of the manifest", slot))
        att_row = self._db.execute(
            "SELECT * FROM attestations WHERE build_id = ? AND valid = 1 ORDER BY seq DESC LIMIT 1", (build_id,)
        ).fetchone()
        if att_row is None:
            reasons.append(_reason("BLOCK", "MISSING_EVIDENCE", f"no trusted CI attestation for {build_id}", slot))
            return build
        payload = json.loads(att_row["payload_json"])
        runner = self._db.execute("SELECT * FROM runners WHERE runner_id = ?", (att_row["runner_id"],)).fetchone()
        # Re-verify at decision time: storage tampering or runner revocation must fail closed.
        if runner is None or not runner["active"] or not verify_payload(runner["public_key"], payload, att_row["signature"]):
            reasons.append(_reason("BLOCK", "ATTESTATION_UNTRUSTED", "attestation signature or runner no longer trusted", slot))
            return build
        self._check_stack(payload, stack, reasons, slot)
        results = {r["test_id"]: r for r in payload["results"]}
        missing = [t.test_id for t in manifest.tests if t.test_id not in results]
        if missing:
            reasons.append(_reason("BLOCK", "REQUIRED_TEST_RESULT_MISSING", f"no result for required tests {missing}", slot))
        tampered = [t.test_id for t in manifest.tests
                    if t.test_id in results and results[t.test_id]["test_digest"] != t.content_digest]
        if tampered:
            reasons.append(_reason("BLOCK", "TEST_INTEGRITY_VIOLATION",
                                   f"executed tests differ from the locked manifest: {tampered}", slot))
        wrong = [tid for tid, r in results.items() if r["outcome"] != expected_outcome]
        if wrong:
            code = "TESTS_CANNOT_FAIL" if slot == "BASELINE" else "TESTS_FAILED"
            reasons.append(_reason("BLOCK", code, f"tests not {expected_outcome}: {sorted(wrong)}", slot))
        if interface is not None and self._policy.require_domain_boundary:
            # The runner observed the imports; the gate judges them against the approved design.
            internal = sorted({f"{tid}:{m}" for tid, r in results.items() for m in r["project_imports"]
                               if m not in interface})
            if internal:
                reasons.append(_reason("BLOCK", "TEST_BYPASSES_DOMAIN_INTERFACE",
                                       f"acceptance tests reach past the C2 domain interface {sorted(interface)}: "
                                       f"{internal}", slot))
        evidence[slot] = {"build_id": build_id, "source_revision": build["source_revision"],
                          "build_digest": build["build_digest"], "attestation_id": att_row["attestation_id"],
                          "runner_id": att_row["runner_id"], "registered_by": build["registered_by"]}
        return build

    @staticmethod
    def _check_stack(payload: Dict[str, Any], stack: Any, reasons: List[Dict[str, Any]], slot: str) -> None:
        """The runner must have executed the stack the team decided in C2 (default: python-pytest)."""
        expected = stack.profile_digest if stack is not None else DEFAULT_STACK_DIGEST
        if payload["stack_digest"] != expected:
            name = stack.stack_id if stack is not None else "python-pytest (default)"
            reasons.append(_reason("BLOCK", "STACK_MISMATCH",
                                   f"the runner executed a different stack than the approved C2 decision {name}", slot))

    def _check_quality(
        self, build: sqlite3.Row, c3: Tuple[sqlite3.Row, Any], reasons: List[Dict[str, Any]], evidence: Dict[str, Any],
        stack: Any = None,
    ) -> None:
        row = self._db.execute(
            "SELECT * FROM quality_attestations WHERE build_id = ? AND valid = 1 ORDER BY seq DESC LIMIT 1",
            (build["build_id"],),
        ).fetchone()
        if row is None:
            reasons.append(_reason("BLOCK", "QUALITY_EVIDENCE_MISSING", "no signed quality attestation for the build", "QUALITY"))
            return
        payload = json.loads(row["payload_json"])
        runner = self._db.execute("SELECT * FROM runners WHERE runner_id = ?", (row["runner_id"],)).fetchone()
        if runner is None or not runner["active"] or not verify_payload(
                runner["public_key"], payload, row["signature"], QUALITY_DOMAIN):
            reasons.append(_reason("BLOCK", "ATTESTATION_UNTRUSTED", "quality attestation no longer trusted", "QUALITY"))
            return
        locked = {t.test_id: t.content_digest for t in c3[1].tests}
        ran = {d["test_id"]: d["test_digest"] for d in payload["uat_digests"]}
        if ran != locked:
            reasons.append(_reason("BLOCK", "TEST_INTEGRITY_VIOLATION",
                                   "quality run used acceptance tests that differ from the locked manifest", "QUALITY"))
        self._check_stack(payload, stack, reasons, "QUALITY")
        checks = {c["kind"]: c for c in payload["checks"]}
        missing = [k for k in self._policy.required_quality_checks if k not in checks]
        if missing:
            reasons.append(_reason("BLOCK", "QUALITY_EVIDENCE_MISSING", f"missing quality checks {missing}", "QUALITY"))
        waivers = set(stack.quality_waivers) if stack is not None else set()
        not_run = sorted(k for k, c in checks.items() if c["status"] == "NOT_RUN")
        unwaived = [k for k in not_run if k not in waivers]
        if unwaived:
            reasons.append(_reason("BLOCK", "QUALITY_CHECK_NOT_RUN",
                                   f"checks {unwaived} cannot run on this stack and C2 does not waive them", "QUALITY"))
        failed = [k for k, c in checks.items() if k != "MUTATION" and c["status"] not in ("PASS", "NOT_RUN")]
        if failed:
            details = [f for k in failed for f in checks[k]["findings"]][:10]
            reasons.append(_reason("BLOCK", "QUALITY_CHECK_FAILED", f"failed checks {failed}: {details}", "QUALITY"))
        score = None
        if "MUTATION" in checks and checks["MUTATION"]["status"] != "NOT_RUN":
            m = checks["MUTATION"]["metrics"]
            total, killed = m.get("mutants", 0), m.get("killed", 0)
            score = 100 if total == 0 else (killed * 100) // total  # computed here, not trusted from the runner
            if score < self._policy.min_mutation_score_pct:
                reasons.append(_reason("BLOCK", "MUTATION_SCORE_LOW",
                                       f"mutation score {score}% < {self._policy.min_mutation_score_pct}%; "
                                       f"survivors: {checks['MUTATION']['findings'][:5]}", "QUALITY"))
        evidence["QUALITY"] = {"attestation_id": row["attestation_id"], "source_revision": payload["source_revision"],
                               "checks": {k: ("WAIVED" if k in not_run and k in waivers else c["status"])
                                          for k, c in checks.items()}, "mutation_score_pct": score}

    def _check_review(self, build: sqlite3.Row, reasons: List[Dict[str, Any]], evidence: Dict[str, Any]) -> None:
        open_blocking = self._open_blocking_findings(build["build_id"])
        if open_blocking:
            reasons.append(_reason("BLOCK", "BLOCKING_FINDINGS_OPEN", f"unresolved blocking findings {open_blocking}", "BUILD"))
        review = self._db.execute(
            "SELECT * FROM reviews WHERE build_id = ? AND valid = 1 AND reviewer_kind = ? ORDER BY seq DESC LIMIT 1",
            (build["build_id"], HUMAN),
        ).fetchone()
        if review is None:
            reasons.append(_reason("PAUSE", "REVIEW_MISSING", "awaiting independent human review", "BUILD"))
            return
        reviewer = self._principal(review["reviewer_id"])
        if (reviewer is None or not reviewer.active or not reviewer.has_any(self._policy.reviewer_roles)
                or review["policy_version"] != self._policy.version):
            reasons.append(_reason("PAUSE", "REVIEW_MISSING", "the recorded review no longer carries authority", "BUILD"))
            return
        if review["reviewer_id"] == build["registered_by"]:
            reasons.append(_reason("BLOCK", "SEPARATION_OF_DUTIES", "implementer reviewed own build", "BUILD"))
        if review["decision"] != "APPROVE":
            reasons.append(_reason("BLOCK", "REVIEW_CHANGES_REQUESTED", "reviewer requested changes", "BUILD"))
        evidence["REVIEW"] = {"review_id": review["review_id"], "reviewer_id": review["reviewer_id"],
                              "decision": review["decision"]}

    # ---------------------------------------------------------- provenance reads

    # ------------------------------------------------------------- M4 release

    def _merged_heads(self, build_id: str) -> Set[str]:
        """Heads the gate allowed to merge for this build: the only revisions a release may be built from."""
        heads = set()
        for r in self._db.execute("SELECT record_json FROM transitions WHERE decision = ?", (ALLOW,)):
            rec = json.loads(r["record_json"])
            if rec["to_stage"] == "MERGE" and rec["evidence"].get("BUILD", {}).get("build_id") == build_id:
                heads.add(rec["head_revision"])
        return heads

    def ingest_provenance(self, payload: Dict[str, Any], signature: str) -> Dict[str, Any]:
        """Signed build provenance for an artifact built from a gate-approved merged revision."""

        def fn(_: None) -> Dict[str, Any]:
            if not isinstance(payload, dict) or not isinstance(signature, str):
                raise ValidationFailure("MALFORMED_ATTESTATION", "payload must be an object and signature a string")
            runner = self._db.execute("SELECT * FROM runners WHERE runner_id = ?", (payload.get("builder_id"),)).fetchone()
            if runner is None or not runner["active"]:
                raise AuthenticationError("UNKNOWN_RUNNER", f"builder {payload.get('builder_id')!r} is not trusted")
            if not verify_payload(runner["public_key"], payload, signature, PROVENANCE_DOMAIN):
                raise AuthenticationError("INVALID_SIGNATURE", "provenance signature does not verify")
            try:
                prov = ProvenancePayload.model_validate(payload)
            except ValidationError:
                raise ValidationFailure("MALFORMED_ATTESTATION", "provenance failed schema validation") from None
            if prov.model_dump(mode="json") != payload:
                raise ValidationFailure("NON_CANONICAL_ATTESTATION", "payload contains non-canonical values")
            if self._db.execute("SELECT 1 FROM provenance WHERE attestation_id = ?", (prov.provenance_id,)).fetchone():
                raise Conflict("REPLAY_DETECTED", f"provenance {prov.provenance_id} was already ingested")
            build = self._build(prov.build_id)
            if build is None or build["kind"] != "CANDIDATE":
                raise ValidationFailure("UNKNOWN_BUILD", f"{prov.build_id} is not a registered candidate build")
            if not build["valid"]:
                raise Conflict("EVIDENCE_INVALIDATED", f"build {prov.build_id} was invalidated")
            if prov.build_digest != build["build_digest"]:
                raise ValidationFailure("BUILD_BINDING_MISMATCH", "artifact was built from different code than the tested build")
            if prov.source_revision not in self._merged_heads(prov.build_id):
                raise ValidationFailure("UNMERGED_SOURCE", "artifacts may only be built from a gate-approved merged head")
            if prov.policy_version != self._policy.version:
                raise ValidationFailure("POLICY_VERSION_MISMATCH", f"provenance made under {prov.policy_version}")
            self._db.execute(
                "INSERT INTO provenance (attestation_id, runner_id, build_id, payload_json, signature, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (prov.provenance_id, prov.builder_id, prov.build_id, json.dumps(payload, sort_keys=True), signature,
                 self._clock()),
            )
            record = {"provenance_id": prov.provenance_id, "build_id": prov.build_id,
                      "source_revision": prov.source_revision, "artifact_digest": prov.artifact.digest}
            self._audit("PROVENANCE_INGESTED", None, record)
            return record

        return self._command("ingest_provenance", None, fn, authenticate=False)

    def _check_release_provenance(self, build: sqlite3.Row, rel: Any, reasons: List[Dict[str, Any]],
                                  evidence: Dict[str, Any]) -> None:
        row = self._db.execute("SELECT * FROM provenance WHERE build_id = ? AND valid = 1 ORDER BY seq DESC LIMIT 1",
                               (build["build_id"],)).fetchone()
        if row is None:
            reasons.append(_reason("BLOCK", "PROVENANCE_MISSING", "no signed build provenance for the release", "RELEASE"))
            return
        payload = json.loads(row["payload_json"])
        runner = self._db.execute("SELECT * FROM runners WHERE runner_id = ?", (row["runner_id"],)).fetchone()
        if runner is None or not runner["active"] or not verify_payload(
                runner["public_key"], payload, row["signature"], PROVENANCE_DOMAIN):
            reasons.append(_reason("BLOCK", "ATTESTATION_UNTRUSTED", "provenance no longer trusted", "RELEASE"))
            return
        if payload["source_revision"] not in self._merged_heads(build["build_id"]):
            reasons.append(_reason("BLOCK", "UNMERGED_SOURCE", "artifact source is not a gate-approved merged head", "RELEASE"))
        if (rel.artifact_digest, rel.merged_revision, rel.provenance_id) != (
            payload["artifact"]["digest"], payload["source_revision"], payload["provenance_id"]
        ):
            reasons.append(_reason("BLOCK", "RELEASE_ARTIFACT_MISMATCH",
                                   "release record does not name the attested artifact, source and provenance", "RELEASE"))
        if not rel.health_checks:
            reasons.append(_reason("BLOCK", "MONITORING_BASELINE_MISSING",
                                   "release has no machine-checkable health checks", "RELEASE"))
        evidence["PROVENANCE"] = {"provenance_id": payload["provenance_id"], "builder_id": payload["builder_id"],
                                  "source_revision": payload["source_revision"], "artifact": payload["artifact"]}

    def register_environment(self, token: str, environment: str, baseline_digest: str) -> Dict[str, Any]:
        """Record what an environment runs before governed deployments start: the first rollback target."""

        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, frozenset({"admin"}), "register environments")
            if environment not in self._policy.environments:
                raise ValidationFailure("UNKNOWN_ENVIRONMENT", environment)
            if self._env_state(environment) is not None:
                raise Conflict("ENVIRONMENT_EXISTS", environment)
            self._db.execute(
                "INSERT INTO deployments (environment, event, artifact_digest, recorded_by, timestamp) "
                "VALUES (?, 'BASELINE', ?, ?, ?)", (environment, baseline_digest, actor.principal_id, self._clock()))
            self._audit("ENVIRONMENT_REGISTERED", actor.principal_id,
                        {"environment": environment, "baseline_digest": baseline_digest})
            return {"environment": environment, "current_digest": baseline_digest}

        return self._command("register_environment", token, fn)

    def _env_states(self, environment: str) -> List[str]:
        """What the environment ran over time (consecutive duplicates collapsed)."""
        states: List[str] = []
        for r in self._db.execute("SELECT * FROM deployments WHERE environment = ? ORDER BY seq", (environment,)):
            current = None
            if r["event"] in ("BASELINE", "PROMOTED"):
                current = r["artifact_digest"]
            elif r["event"] == "ROLLED_BACK":
                current = r["restored_digest"]
            if current is not None and (not states or states[-1] != current):
                states.append(current)
        return states

    def _env_state(self, environment: str) -> Optional[str]:
        states = self._env_states(environment)
        return states[-1] if states else None

    def get_environment(self, token: str, environment: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            history = [{"event": r["event"], "artifact_digest": r["artifact_digest"], "restored_digest": r["restored_digest"],
                        "verdict": r["verdict"], "decision_id": r["decision_id"]}
                       for r in self._db.execute("SELECT * FROM deployments WHERE environment = ? ORDER BY seq",
                                                 (environment,))]
            return {"environment": environment, "current_digest": self._env_state(environment), "history": history}

        return self._command("get_environment", token, fn)

    def evaluate_deployment(
        self, token: str, work_item_id: str, environment: str, release: Dict[str, Any],
        artifact_digest: str, idempotency_key: str,
    ) -> Dict[str, Any]:
        """May this exact artifact start a canary in this environment? Records the rollback target."""
        try:
            fingerprint = digest({"work_item_id": work_item_id, "environment": environment, "release": release,
                                  "artifact_digest": artifact_digest, "kind": "DEPLOY"})
        except CanonicalizationError as exc:
            raise ValidationFailure("MALFORMED_REQUEST", str(exc)) from None

        def fn(actor: Principal) -> Dict[str, Any]:
            _require_id(idempotency_key, "idempotency_key")
            prior = self._db.execute("SELECT * FROM transitions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["request_fingerprint"] != fingerprint or prior["requester_id"] != actor.principal_id:
                    raise Conflict("IDEMPOTENCY_KEY_REUSED", "this key was used for a different request")
                return {**json.loads(prior["record_json"]), "replayed": True}
            wi = self._work_item(work_item_id)
            reasons: List[Dict[str, Any]] = []
            evidence: Dict[str, Any] = {}
            rollback_target = None
            if not actor.has_any(self._policy.deploy_requesters):
                reasons.append(_reason("BLOCK", "ACTOR_NOT_AUTHORIZED", f"{actor.principal_id} may not deploy"))
            elif environment not in self._policy.environments:
                reasons.append(_reason("BLOCK", "UNKNOWN_ENVIRONMENT", f"{environment!r} is not a governed environment"))
            elif wi["stage"] != Stage.RELEASE_READY.value:
                reasons.append(_reason("BLOCK", "INVALID_TRANSITION", f"work item is at {wi['stage']}, not RELEASE_READY"))
            else:
                last = json.loads(wi["last_allow_refs_json"])
                self._evaluate_gate(Stage.RELEASE_READY, last, json.loads(wi["bindings_json"]), reasons, evidence,
                                    work_item_id)
                bound = last.get("RELEASE", {})
                if not isinstance(release, dict) or (release.get("artifact_id"), release.get("version")) != (
                        bound.get("artifact_id"), bound.get("version")):
                    reasons.append(_reason("BLOCK", "BINDING_MISMATCH", "not the release approved for this work item", "RELEASE"))
                else:
                    rel = json.loads(self._artifact(bound["artifact_id"], bound["version"])["body_json"])
                    if artifact_digest != rel.get("artifact_digest"):
                        reasons.append(_reason("BLOCK", "RELEASE_ARTIFACT_MISMATCH",
                                               "artifact presented for deployment is not the released one", "RELEASE"))
                    index = self._policy.environments.index(environment)
                    if index > 0:
                        before = self._policy.environments[index - 1]
                        promoted = self._db.execute(
                            "SELECT 1 FROM deployments WHERE environment = ? AND event = 'PROMOTED' AND artifact_digest = ?",
                            (before, artifact_digest)).fetchone()
                        if promoted is None:
                            reasons.append(_reason("BLOCK", "PROMOTION_ORDER",
                                                   f"artifact has not been promoted in {before} yet"))
                    rollback_target = self._env_state(environment)
                    if rollback_target is None:
                        reasons.append(_reason("BLOCK", "NO_ROLLBACK_TARGET",
                                               f"{environment} has no recorded baseline to roll back to"))
                    evidence["DEPLOY"] = {"environment": environment, "artifact_digest": artifact_digest,
                                          "rollback_target": rollback_target, "health_checks": rel.get("health_checks", [])}
            decision = self._decide(reasons)
            record = {
                "transition_id": "DEP-" + uuid.uuid4().hex, "idempotency_key": idempotency_key,
                "work_item_id": work_item_id, "from_stage": wi["stage"], "to_stage": f"DEPLOY:{environment}",
                "environment": environment, "artifact_digest": artifact_digest, "rollback_target": rollback_target,
                "decision": decision, "reasons": reasons, "evidence": evidence,
                "policy_version": self._policy.version, "policy_digest": self._policy.digest,
                "requester_id": actor.principal_id, "evaluated_at": self._clock(),
            }
            audit = self._audit("DEPLOYMENT_EVALUATED", actor.principal_id, {
                "transition_id": record["transition_id"], "work_item_id": work_item_id, "environment": environment,
                "artifact_digest": artifact_digest, "decision": decision, "reason_codes": [r["code"] for r in reasons],
            })
            record["audit_entry_hash"] = audit["entry_hash"]
            self._db.execute(
                "INSERT INTO transitions (transition_id, idempotency_key, request_fingerprint, requester_id, "
                "work_item_id, decision, record_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["transition_id"], idempotency_key, fingerprint, actor.principal_id, work_item_id,
                 decision, json.dumps(record, sort_keys=True)),
            )
            return {**record, "replayed": False}

        return self._command("evaluate_deployment", token, fn)

    def record_deployment(self, token: str, decision_id: str, event: str,
                          metrics: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """Append a deployment event. The gate, not the deployer, judges canary health against the release."""

        def fn(actor: Principal) -> Dict[str, Any]:
            self._require_role(actor, self._policy.deploy_requesters, "record deployments")
            row = self._db.execute("SELECT record_json FROM transitions WHERE transition_id = ?", (decision_id,)).fetchone()
            dec = json.loads(row["record_json"]) if row else {}
            stage_kind = dec.get("to_stage", "").split(":", 1)[0]
            if stage_kind not in ("DEPLOY", "ROLLBACK") or dec.get("decision") != ALLOW:
                raise AuthorizationError("DEPLOY_NOT_ALLOWED", f"{decision_id} is not an allowed deployment decision")
            if stage_kind == "ROLLBACK" and event != "ROLLED_BACK":
                raise ValidationFailure("INVALID_EVENT", "an incident rollback decision only authorizes ROLLED_BACK")
            prior = list(self._db.execute("SELECT * FROM deployments WHERE decision_id = ? ORDER BY seq", (decision_id,)))
            if any(p["event"] in ("PROMOTED", "ROLLED_BACK") for p in prior):
                raise Conflict("DEPLOYMENT_CLOSED", f"{decision_id} was already promoted or rolled back")
            verdict, failed, restored = None, [], None
            if event == "CANARY":
                if not isinstance(metrics, dict) or not all(isinstance(k, str) and type(v) is int for k, v in metrics.items()):
                    raise ValidationFailure("INVALID_METRICS", "metrics must map names to integers")
                for raw in dec["evidence"]["DEPLOY"]["health_checks"]:
                    check = HealthCheck.model_validate(raw)
                    if check.metric not in metrics:
                        failed.append(f"{check.metric}: not reported")
                    elif not check.holds(metrics[check.metric]):
                        failed.append(f"{check.metric}={metrics[check.metric]} {check.unit} violates "
                                      f"{check.op} {check.threshold}")
                verdict = "UNHEALTHY" if failed else "HEALTHY"
            elif event == "PROMOTED":
                canaries = [p for p in prior if p["event"] == "CANARY"]
                if not canaries or canaries[-1]["verdict"] != "HEALTHY":
                    raise AuthorizationError("UNHEALTHY_CANARY", "promotion requires a healthy canary verdict from the gate")
            elif event == "ROLLED_BACK":
                restored = dec["rollback_target"]
            else:
                raise ValidationFailure("INVALID_EVENT", "event must be CANARY, PROMOTED or ROLLED_BACK")
            self._db.execute(
                "INSERT INTO deployments (decision_id, environment, event, artifact_digest, restored_digest, metrics_json, "
                "verdict, failed_json, recorded_by, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, dec["environment"], event, dec["artifact_digest"], restored,
                 json.dumps(metrics, sort_keys=True) if metrics is not None else None, verdict, json.dumps(failed),
                 actor.principal_id, self._clock()),
            )
            record = {"decision_id": decision_id, "environment": dec["environment"], "event": event,
                      "artifact_digest": dec["artifact_digest"], "verdict": verdict, "failed_checks": failed,
                      "restored_digest": restored}
            self._audit("DEPLOYMENT_RECORDED", actor.principal_id, record)
            return record

        return self._command("record_deployment", token, fn)

    # ------------------------------------------------------------ M5 incidents

    _INCIDENT_ORDER = ("PACKET", "DECISION", "RESOLVED", "POSTMORTEM")

    def record_incident(self, token: str, incident_id: str, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Append one step of the section 12.1 incident workflow.

        PACKET comes from the read-only operations agent; DECISION and RESOLVED
        from an authenticated human responder; POSTMORTEM from SRE or engineering.
        Steps are append-only and must arrive in order.
        """

        def fn(actor: Principal) -> Dict[str, Any]:
            _require_id(incident_id, "incident_id")
            models = {"PACKET": IncidentPacket, "DECISION": IncidentDecision,
                      "RESOLVED": IncidentResolution, "POSTMORTEM": Postmortem}
            if event not in models:
                raise ValidationFailure("INVALID_EVENT", f"event must be one of {list(models)}")
            if event == "PACKET":
                self._require_role(actor, self._policy.incident_packet_roles, "file incident packets")
            else:
                roles = self._policy.postmortem_roles if event == "POSTMORTEM" else self._policy.incident_responder_roles
                if actor.kind != HUMAN:
                    raise AuthorizationError("HUMAN_APPROVAL_REQUIRED", f"{event} must be recorded by a human")
                self._require_role(actor, roles, f"record incident {event.lower()}")
            try:
                parsed = models[event].model_validate(payload)
            except ValidationError as exc:
                raise ValidationFailure("SCHEMA_VIOLATION", f"{event} failed schema validation",
                                        errors=json.loads(exc.json(include_url=False, include_context=False))) from None
            body = parsed.model_dump(mode="json")
            if event == "PACKET" and body["incident_id"] != incident_id:
                raise ValidationFailure("REF_MISMATCH", "packet names a different incident")
            done = {r["event"] for r in self._db.execute("SELECT event FROM incidents WHERE incident_id = ?", (incident_id,))}
            index = self._INCIDENT_ORDER.index(event)
            if event == "PACKET" and "DECISION" in done:
                raise Conflict("INCIDENT_DECIDED", f"{incident_id} was decided; open a new incident to re-investigate")
            if event != "PACKET" and event in done:
                raise Conflict("EVENT_ALREADY_RECORDED", f"{incident_id} already has {event}")
            if index > 0 and self._INCIDENT_ORDER[index - 1] not in done:
                raise Conflict("OUT_OF_ORDER", f"{event} requires {self._INCIDENT_ORDER[index - 1]} first")
            if event == "DECISION":
                latest = self._db.execute(
                    "SELECT payload_json, payload_digest FROM incidents WHERE incident_id = ? AND event = 'PACKET' "
                    "ORDER BY seq DESC LIMIT 1", (incident_id,)).fetchone()
                # The responder decides on what they saw: a newer investigation supersedes it.
                if body["packet_digest"] != latest["payload_digest"]:
                    raise Conflict("STALE_PACKET", "the decision names a packet that is not the latest investigation")
                packet = json.loads(latest["payload_json"])
                if body["hypothesis_id"] is not None and body["hypothesis_id"] not in {
                        h["hypothesis_id"] for h in packet["working_hypotheses"]}:
                    raise ValidationFailure("UNKNOWN_HYPOTHESIS", "decision must name a hypothesis from the packet")
            payload_digest = digest(body)
            self._db.execute(
                "INSERT INTO incidents (incident_id, event, payload_json, payload_digest, recorded_by, recorder_kind, "
                "timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (incident_id, event, json.dumps(body, sort_keys=True), payload_digest, actor.principal_id, actor.kind,
                 self._clock()),
            )
            record = {"incident_id": incident_id, "event": event, "payload_digest": payload_digest,
                      "recorded_by": actor.principal_id}
            self._audit("INCIDENT_RECORDED", actor.principal_id, record)
            return record

        return self._command("record_incident", token, fn)

    def _incident_payload(self, incident_id: str, event: str) -> Dict[str, Any]:
        row = self._db.execute("SELECT payload_json FROM incidents WHERE incident_id = ? AND event = ? "
                               "ORDER BY seq DESC LIMIT 1", (incident_id, event)).fetchone()
        if row is None:
            raise NotFound("UNKNOWN_INCIDENT_EVENT", f"{incident_id} has no {event}")
        return json.loads(row["payload_json"])

    def get_incident(self, token: str, incident_id: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            events = [{"event": r["event"], "payload": json.loads(r["payload_json"]), "payload_digest": r["payload_digest"],
                       "recorded_by": r["recorded_by"], "recorder_kind": r["recorder_kind"]}
                      for r in self._db.execute("SELECT * FROM incidents WHERE incident_id = ? ORDER BY seq", (incident_id,))]
            if not events:
                raise NotFound("UNKNOWN_INCIDENT", incident_id)
            return {"incident_id": incident_id, "events": events}

        return self._command("get_incident", token, fn)

    def evaluate_incident_rollback(self, token: str, incident_id: str, environment: str,
                                   idempotency_key: str) -> Dict[str, Any]:
        """A human responder's decision to restore the previous artifact. The agent can never request this."""
        fingerprint = digest({"incident_id": incident_id, "environment": environment, "kind": "ROLLBACK"})

        def fn(actor: Principal) -> Dict[str, Any]:
            _require_id(idempotency_key, "idempotency_key")
            prior = self._db.execute("SELECT * FROM transitions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["request_fingerprint"] != fingerprint or prior["requester_id"] != actor.principal_id:
                    raise Conflict("IDEMPOTENCY_KEY_REUSED", "this key was used for a different request")
                return {**json.loads(prior["record_json"]), "replayed": True}
            reasons: List[Dict[str, Any]] = []
            current = previous = None
            if actor.kind != HUMAN or not actor.has_any(self._policy.incident_responder_roles):
                reasons.append(_reason("BLOCK", "ACTOR_NOT_AUTHORIZED", "only a human on-call responder may roll back"))
            else:
                decided = self._db.execute("SELECT payload_json FROM incidents WHERE incident_id = ? AND event = 'DECISION'",
                                           (incident_id,)).fetchone()
                if decided is None or json.loads(decided["payload_json"])["action"] != "ROLLBACK":
                    reasons.append(_reason("BLOCK", "MISSING_EVIDENCE", "no recorded responder decision to roll back"))
                states = self._env_states(environment) if environment in self._policy.environments else []
                if environment not in self._policy.environments:
                    reasons.append(_reason("BLOCK", "UNKNOWN_ENVIRONMENT", environment))
                elif len(states) < 2:
                    reasons.append(_reason("BLOCK", "NO_ROLLBACK_TARGET", f"{environment} has no previous artifact"))
                else:
                    current, previous = states[-1], states[-2]
            decision = self._decide(reasons)
            record = {
                "transition_id": "RBK-" + uuid.uuid4().hex, "idempotency_key": idempotency_key,
                "work_item_id": incident_id, "from_stage": "INCIDENT", "to_stage": f"ROLLBACK:{environment}",
                "environment": environment, "artifact_digest": current, "rollback_target": previous,
                "decision": decision, "reasons": reasons, "evidence": {"incident_id": incident_id},
                "policy_version": self._policy.version, "policy_digest": self._policy.digest,
                "requester_id": actor.principal_id, "evaluated_at": self._clock(),
            }
            audit = self._audit("ROLLBACK_EVALUATED", actor.principal_id, {
                "transition_id": record["transition_id"], "incident_id": incident_id, "environment": environment,
                "decision": decision, "reason_codes": [r["code"] for r in reasons]})
            record["audit_entry_hash"] = audit["entry_hash"]
            self._db.execute(
                "INSERT INTO transitions (transition_id, idempotency_key, request_fingerprint, requester_id, "
                "work_item_id, decision, record_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["transition_id"], idempotency_key, fingerprint, actor.principal_id, incident_id,
                 decision, json.dumps(record, sort_keys=True)),
            )
            return {**record, "replayed": False}

        return self._command("evaluate_incident_rollback", token, fn)

    def get_transition(self, token: str, transition_id: str) -> Dict[str, Any]:
        def fn(actor: Principal) -> Dict[str, Any]:
            row = self._db.execute("SELECT record_json FROM transitions WHERE transition_id = ?", (transition_id,)).fetchone()
            if row is None:
                raise NotFound("UNKNOWN_TRANSITION", transition_id)
            return json.loads(row["record_json"])

        return self._command("get_transition", token, fn)

    def reverify(self, token: str, work_item_id: str) -> Dict[str, Any]:
        """Re-run the gate that admitted the current stage, without committing anything.

        Behavioral-monitoring hook: answers "is the evidence behind this stage still valid?"
        """

        def fn(actor: Principal) -> Dict[str, Any]:
            wi = self._work_item(work_item_id)
            stage = Stage(wi["stage"])
            if wi["last_allow_refs_json"] is None:
                return {"work_item_id": work_item_id, "stage": stage.value, "decision": ALLOW, "reasons": []}
            reasons: List[Dict[str, Any]] = []
            evidence: Dict[str, Any] = {}
            self._evaluate_gate(stage, json.loads(wi["last_allow_refs_json"]), json.loads(wi["bindings_json"]), reasons,
                                evidence, work_item_id)
            return {"work_item_id": work_item_id, "stage": stage.value, "decision": self._decide(reasons),
                    "reasons": reasons, "evidence": evidence}

        return self._command("reverify", token, fn)

    def release_manifest(self, token: str, work_item_id: str) -> Dict[str, Any]:
        """Template J evidence manifest for a work item that reached RELEASE_READY."""

        def fn(actor: Principal) -> Dict[str, Any]:
            wi = self._work_item(work_item_id)
            if wi["stage"] != Stage.RELEASE_READY.value:
                raise Conflict("NOT_RELEASE_READY", f"work item is at {wi['stage']}")
            records = [json.loads(r["record_json"]) for r in self._db.execute(
                "SELECT record_json FROM transitions WHERE work_item_id = ? AND decision = ? ORDER BY rowid",
                (work_item_id, ALLOW))]
            ev = [r for r in records if r["to_stage"] == Stage.RELEASE_READY.value][-1]["evidence"]
            deploy_ids = [r["transition_id"] for r in records if r["to_stage"].startswith("DEPLOY:")]
            deployments = [
                {"environment": d["environment"], "event": d["event"], "artifact_digest": d["artifact_digest"],
                 "restored_digest": d["restored_digest"], "verdict": d["verdict"], "executor": d["recorded_by"],
                 "timestamp": d["timestamp"]}
                for d in self._db.execute("SELECT * FROM deployments ORDER BY seq")
                if d["decision_id"] in deploy_ids
            ]
            release = json.loads(self._artifact(ev["RELEASE"]["artifact_id"], ev["RELEASE"]["version"])["body_json"])
            docs = json.loads(self._artifact(ev["DOCS"]["artifact_id"], ev["DOCS"]["version"])["body_json"])
            return {
                "release_id": release["release_id"],
                "tag": release["tag"],
                "source_revision": ev["BUILD"]["source_revision"],
                "build_digest": ev["BUILD"]["build_digest"],
                "acceptance_manifest": ev["C3"],
                "requirement_versions": {s: ev[s] for s in ("C0", "C1", "C2", "STORIES")},
                "policy_version": self._policy.version,
                "test_attestations": [ev["BUILD"]["attestation_id"]],
                "review": ev.get("REVIEW"),
                "human_release_approvers": ev["RELEASE"]["approvals"],
                "policy_release_authorizations": ev["RELEASE"].get("policy_authorizations", []),
                "risk": ev.get("RISK"),
                "documentation": {k: docs[k] for k in ("user_docs", "runbook", "release_notes", "alerts", "rollback_plan")},
                "monitoring_baseline": docs["monitoring_baseline"],
                "canary_plan": release["canary_plan"],
                "rollback_plan": release["rollback_plan"],
                "deployment_target": release["deployment_target"],
                "merged_revision": release.get("merged_revision"),
                "artifact_digest": release.get("artifact_digest"),
                "provenance": ev.get("PROVENANCE"),
                "health_checks": release.get("health_checks", []),
                "production_deployment": deployments,
            }

        return self._command("release_manifest", token, fn)

    def audit_log(self, token: str) -> List[Dict[str, Any]]:
        def fn(actor: Principal) -> List[Dict[str, Any]]:
            return [
                {"seq": r["seq"], "event_type": r["event_type"], "actor_id": r["actor_id"],
                 "payload": json.loads(r["payload_json"]), "timestamp": r["timestamp"],
                 "prev_hash": r["prev_hash"], "entry_hash": r["entry_hash"]}
                for r in self._db.execute("SELECT * FROM audit ORDER BY seq")
            ]

        return self._command("audit_log", token, fn)

    def verify_audit_chain(self) -> Dict[str, Any]:
        """Recompute the hash chain; reports the first entry that fails."""
        with self._lock:
            prev = GENESIS_HASH
            count = 0
            for r in self._db.execute("SELECT * FROM audit ORDER BY seq"):
                entry = {"event_type": r["event_type"], "actor_id": r["actor_id"],
                         "payload": json.loads(r["payload_json"]), "timestamp": r["timestamp"], "prev_hash": r["prev_hash"]}
                if r["prev_hash"] != prev or digest(entry) != r["entry_hash"]:
                    return {"ok": False, "entries": count, "first_bad_seq": r["seq"]}
                prev = r["entry_hash"]
                count += 1
            return {"ok": True, "entries": count, "head": prev}
