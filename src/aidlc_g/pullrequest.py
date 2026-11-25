"""Pull requests against the protected main branch (milestone M3).

The merge service is a trusted platform principal (``merge_service``), not an
agent. It assembles the Template I review packet from gate evidence, asks the
gate for a merge decision, and only then asks the workspace to fast-forward
main - which re-checks that decision itself.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import Conflict
from .service import ALLOW, GovernanceService
from .workspace import MAIN, Workspace

DOC_FIELDS = ("user_docs", "runbook", "release_notes", "alerts", "rollback_plan", "api_docs")


@dataclass
class PullRequest:
    pr_id: str
    work_item_id: str
    branch: str
    build_id: str
    refs: Dict[str, Any]
    state: str = "OPEN"
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    merged_revision: Optional[str] = None


class MergeService:
    def __init__(self, gate: GovernanceService, workspace: Workspace, token: str) -> None:
        self._gate = gate
        self._workspace = workspace
        self._token = token

    def open(self, pr_id: str, work_item_id: str, branch: str, build_id: str, refs: Dict[str, Any]) -> PullRequest:
        return PullRequest(pr_id, work_item_id, branch, build_id, dict(refs))

    def _artifact(self, ref: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return self._gate.get_artifact(self._token, ref["artifact_id"], ref["version"]) if ref else None

    def packet(self, pr: PullRequest, docs_ref: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Template I: independent pull-request review packet, built only from gate records."""
        ev = self._gate.get_evidence(self._token, pr.build_id)
        c1, c2, c3 = (self._artifact(pr.refs.get(s)) for s in ("C1", "C2", "C3"))
        docs = self._artifact(docs_ref)
        quality = ev["quality"] or {"checks": [], "source_revision": None}
        checks = {c["kind"]: c for c in quality["checks"]}
        latest: Dict[str, Dict[str, Any]] = {}
        for review in ev["reviews"]:
            for f in review["findings"]:
                latest[f["finding_id"]] = f
        human = [r for r in ev["reviews"] if r["reviewer_kind"] == "human"]
        return {
            "pull_request": {"id": pr.pr_id, "branch": pr.branch, "base": self._workspace.branch_base(pr.branch),
                             "head": self._workspace.branch_head(pr.branch)},
            "business_requirement": {"id": f"{c1['artifact_id']}@v{c1['version']}", "digest": c1["digest"],
                                     "approvers": self._approvers(c1),
                                     "domain_scenarios": {s["scenario_id"]: s["decided_by"]
                                                          for s in c1["body"]["domain_scenarios"]}},
            "technical_requirement_and_adr": {"id": f"{c2['artifact_id']}@v{c2['version']}", "digest": c2["digest"],
                                              "adr": c2["body"]["adr"]["adr_id"], "decision": c2["body"]["adr"]["decision"],
                                              "approvers": self._approvers(c2)},
            "protected_uat": {"manifest": f"{c3['artifact_id']}@v{c3['version']}", "digest": c3["digest"],
                              "scenarios": {b["scenario_id"]: b["test_ids"] for b in c3["body"]["scenarios"]}},
            "implementation": {"commit": ev["build"]["source_revision"], "build_digest": ev["build"]["build_digest"],
                               "implementer": ev["build"]["registered_by"]},
            "independent_checks": {
                "acceptance": {r["test_id"]: r["outcome"] for r in (ev["attestation"] or {}).get("results", [])},
                "acceptance_attestation": (ev["attestation"] or {}).get("attestation_id"),
                "quality_attestation": quality.get("attestation_id"),
                "quality_revision": quality["source_revision"],
                **{k: f"{c['status']}: {c['summary']}" for k, c in checks.items()},
            },
            "security_findings": [*checks.get("SECRET_SCAN", {}).get("findings", []),
                                  *checks.get("DEPENDENCY_SCAN", {}).get("findings", [])],
            "surviving_mutants": checks.get("MUTATION", {}).get("findings", []),
            "docs_updated": [docs["body"][k]["uri"] for k in DOC_FIELDS if docs and docs["body"].get(k)],
            "ai_review_findings": list(latest.values()),
            "human_review_outcome": {"reviewer": human[-1]["reviewer_id"], "decision": human[-1]["decision"]}
            if human else "PENDING",
            "outstanding_risks": [f for f in latest.values() if f["status"] == "RISK_ACCEPTED"],
        }

    @staticmethod
    def _approvers(view: Dict[str, Any]) -> List[str]:
        return [f"{a['approver_id']} ({a['role']})" for a in view["approvals"]]

    def merge(self, pr: PullRequest, docs_ref: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        ev = self._gate.get_evidence(self._token, pr.build_id)
        tested = (ev["quality"] or {}).get("source_revision") or ev["build"]["source_revision"]
        head = self._workspace.branch_head(pr.branch)
        changed = self._workspace.changed_paths(self._token, tested, head)
        docs = self._artifact(docs_ref)
        stale = []
        for name in DOC_FIELDS:
            doc = docs["body"].get(name)
            if doc:
                path = doc["uri"][len("repo://"):]
                try:
                    actual = self._workspace.read(self._token, path, revision=head).digest
                except Exception:
                    actual = None
                if actual != doc["digest"]:
                    stale.append(path)
        if stale:
            raise Conflict("DOCS_DIGEST_MISMATCH", f"approved docs differ from the branch head: {stale}")
        decision = self._gate.evaluate_merge(self._token, pr.work_item_id, pr.branch, head, tested, changed,
                                             {"DOCS": docs_ref}, idempotency_key)
        pr.decisions.append(decision)
        if decision["decision"] == ALLOW:
            pr.merged_revision = self._workspace.merge(self._token, pr.branch, decision["transition_id"])
            pr.state = "MERGED"
        else:
            pr.state = "BLOCKED"
        return decision

    @property
    def main_head(self) -> str:
        return self._workspace.branch_head(MAIN)
