"""Progressive delivery with a governed rollback path (milestone M4).

The deployment service is a trusted platform principal (``deploy_service``).
For each environment, in policy order (staging, then production):

1. fetch the artifact from the registry - bytes are re-verified against the digest;
2. ask the gate for a deployment decision (re-verifies all release evidence,
   promotion order, and records the rollback target: what the environment runs now);
3. run a canary and report observed metrics; the gate judges them against the
   release's health checks;
4. promote on a HEALTHY verdict, otherwise roll back to the recorded target.

Rollback is pre-authorized by the approved release record (Template J
"rollback_plan: preauthorized boundaries"), so it needs no new human decision;
it is still recorded and audited like any other deployment event.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import GovernanceError
from .registry import ArtifactRegistry
from .service import ALLOW, GovernanceService

Telemetry = Callable[[str, str, str], Dict[str, int]]  # (environment, artifact_digest, phase) -> metrics


@dataclass
class DeploymentRecord:
    environment: str
    artifact_digest: str
    outcome: str  # PROMOTED | ROLLED_BACK | BLOCKED | REFUSED
    decision: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    running_after: Optional[str] = None


class DeploymentService:
    def __init__(self, gate: GovernanceService, registry: ArtifactRegistry, token: str, telemetry: Telemetry) -> None:
        self._gate = gate
        self._registry = registry
        self._token = token
        self._telemetry = telemetry
        self.running: Dict[str, str] = {}  # what each environment actually serves (simulated)
        self.history: List[DeploymentRecord] = []

    def deploy(self, work_item_id: str, release: Dict[str, Any], environment: str, idempotency_key: str) -> DeploymentRecord:
        digest = release["body"].get("artifact_digest") or ""
        record = DeploymentRecord(environment, digest, "REFUSED")
        self.history.append(record)
        try:
            self._registry.get(digest)
        except GovernanceError as exc:
            record.error = exc.code
            return record
        ref = {"artifact_id": release["artifact_id"], "version": release["version"]}
        decision = self._gate.evaluate_deployment(self._token, work_item_id, environment, ref, digest, idempotency_key)
        record.decision = decision
        if decision["decision"] != ALLOW:
            record.outcome = "BLOCKED"
            return record
        metrics = self._telemetry(environment, digest, "canary")
        canary = self._gate.record_deployment(self._token, decision["transition_id"], "CANARY", metrics)
        record.events.append(canary)
        if canary["verdict"] == "HEALTHY":
            record.events.append(self._gate.record_deployment(self._token, decision["transition_id"], "PROMOTED"))
            self.running[environment] = digest
            record.outcome = "PROMOTED"
        else:
            rolled = self._gate.record_deployment(self._token, decision["transition_id"], "ROLLED_BACK")
            record.events.append(rolled)
            self.running[environment] = rolled["restored_digest"]
            record.outcome = "ROLLED_BACK"
        record.running_after = self.running[environment]
        return record

    def execute_rollback(self, decision: Dict[str, Any]) -> DeploymentRecord:
        """Carry out a human-authorized incident rollback (the gate re-checks the decision)."""
        env = decision["environment"]
        record = DeploymentRecord(env, decision.get("artifact_digest") or "", "REFUSED", decision)
        self.history.append(record)
        rolled = self._gate.record_deployment(self._token, decision["transition_id"], "ROLLED_BACK")
        record.events.append(rolled)
        self.running[env] = rolled["restored_digest"]
        record.outcome, record.running_after = "ROLLED_BACK", self.running[env]
        return record
