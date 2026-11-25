"""Policy-as-code: who may author, approve, build, review and promote.

The policy is data with its own version and digest; approvals and
attestations record the policy version they were made under and stop
counting when the active policy changes.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Tuple

from .canonical import digest
from .contracts import ContractType
from .lifecycle import Stage

HUMAN = "human"
AGENT = "agent"
SERVICE = "service"
PRINCIPAL_KINDS = (HUMAN, AGENT, SERVICE)


def _fz(*roles: str) -> FrozenSet[str]:
    return frozenset(roles)


@dataclass(frozen=True)
class Policy:
    version: str = "governance-policy-v2"

    # Roles allowed to submit a new version of each contract type.
    authors: Mapping[ContractType, FrozenSet[str]] = field(
        default_factory=lambda: {
            ContractType.C0: _fz("requirements_agent", "sponsor", "product_owner"),
            ContractType.C1: _fz("requirements_agent", "product_owner"),
            ContractType.C2: _fz("architecture_agent", "engineering_lead"),
            ContractType.STORIES: _fz("story_planning_agent", "engineering_lead"),
            ContractType.C3: _fz("acceptance_test_agent", "qa"),
            ContractType.DOCS: _fz("documentation_agent", "product_owner", "sre"),
            ContractType.RELEASE: _fz("release_evidence_agent", "release_manager"),
        }
    )

    # Each listed role must be satisfied by a distinct authenticated human.
    approvers: Mapping[ContractType, Tuple[str, ...]] = field(
        default_factory=lambda: {
            ContractType.C0: ("sponsor",),
            ContractType.C1: ("product_owner",),
            ContractType.C2: ("engineering_lead",),
            ContractType.STORIES: ("engineering_lead", "product_owner"),
            ContractType.C3: ("product_owner", "qa"),
            ContractType.DOCS: ("product_owner", "sre"),
            ContractType.RELEASE: ("release_manager",),
        }
    )

    # Who may decide domain behavior. Every C1 scenario names one of these people, who
    # must also approve that C1 version: agents propose, domain owners decide.
    domain_owner_roles: FrozenSet[str] = _fz("product_owner")
    # Risk tiers (severity-proportional governance). A tier only removes human approvals where
    # the gate's own evidence already carries the decision. The rest are covered by a policy
    # authorization: not an approval, but the gate applying a versioned rule, recorded with that rule. C0 intent, C1 domain scenarios and C3 acceptance stay human in
    # every tier. A person declares the tier; evidence can only raise it (see service._risk_tier).
    default_risk_tier: str = "HIGH"  # undeclared work gets the full set of approvals
    risk_declarer_roles: FrozenSet[str] = _fz("engineering_lead", "product_owner")
    tier_approvers: Mapping[str, Mapping[ContractType, Tuple[str, ...]]] = field(
        default_factory=lambda: {
            "LOW": {ContractType.C2: (), ContractType.STORIES: (), ContractType.DOCS: (), ContractType.RELEASE: ()},
            "MEDIUM": {ContractType.C2: ("engineering_lead",), ContractType.STORIES: (),
                       ContractType.DOCS: ("sre",), ContractType.RELEASE: ("release_manager",)},
        }
    )
    low_risk_max_changed_files: int = 10
    # Decision latency (minutes): approvals waiting longer than this are reported as over target.
    approval_target_minutes: int = 240

    # Who may decide the technology stack bound into C2: people, not agents.
    stack_owner_roles: FrozenSet[str] = _fz("engineering_lead")

    candidate_build_roles: FrozenSet[str] = _fz("implementation_agent", "engineer")
    baseline_build_roles: FrozenSet[str] = _fz("acceptance_test_agent", "qa")
    # Holders of these roles may never author or approve the acceptance manifest
    # their build is measured against.
    implementation_roles: FrozenSet[str] = _fz("implementation_agent", "engineer")
    reviewer_roles: FrozenSet[str] = _fz("reviewer")
    finding_agent_roles: FrozenSet[str] = _fz("code_review_agent", "test_coverage_agent")

    transition_requesters: Mapping[Stage, FrozenSet[str]] = field(
        default_factory=lambda: {
            **{s: _fz("orchestrator") for s in Stage},
            Stage.RELEASE_READY: _fz("orchestrator", "release_manager"),
        }
    )

    # Stage 5 exit: the locked suite must be shown to fail on a stub build.
    require_failing_baseline: bool = True
    # Template H quality rule: include negative / boundary paths.
    require_negative_path_test: bool = True
    # Acceptance tests exercise behavior through the C2 domain interface only.
    require_domain_boundary: bool = True

    # Stage 7 exit (M3): signed quality evidence on the tested build.
    require_quality_evidence: bool = True
    required_quality_checks: Tuple[str, ...] = ("COVERAGE_TESTS", "MUTATION", "SECRET_SCAN", "DEPENDENCY_SCAN")
    # Integer percent: the canonical form carries no floats. A policy choice, not a truth claim (24.1).
    min_mutation_score_pct: int = 80

    # Protected branch: who may request a merge decision, and which paths may change
    # between the last independently tested revision and the merged head.
    merge_requesters: FrozenSet[str] = _fz("merge_service")
    post_test_change_prefixes: Tuple[str, ...] = ("docs/",)

    # M4: release needs signed provenance for an artifact built from a merged revision,
    # and a machine-checkable monitoring baseline. Deployments go through environments in order.
    require_release_provenance: bool = True
    environments: Tuple[str, ...] = ("staging", "production")
    deploy_requesters: FrozenSet[str] = _fz("deploy_service")

    # M5: the operations agent may only file incident packets; humans decide, resolve and learn.
    incident_packet_roles: FrozenSet[str] = _fz("operational_support_agent")
    incident_responder_roles: FrozenSet[str] = _fz("sre")
    postmortem_roles: FrozenSet[str] = _fz("sre", "engineering_lead")

    def approvers_for(self, ctype: ContractType, tier: str) -> Tuple[str, ...]:
        """Human roles a contract needs at a risk tier. HIGH (and any unlisted type) is the full set."""
        return tuple(self.tier_approvers.get(tier, {}).get(ctype, self.approvers[ctype]))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "authors": {k.value: sorted(v) for k, v in self.authors.items()},
            "approvers": {k.value: list(v) for k, v in self.approvers.items()},
            "domain_owner_roles": sorted(self.domain_owner_roles),
            "stack_owner_roles": sorted(self.stack_owner_roles),
            "default_risk_tier": self.default_risk_tier,
            "risk_declarer_roles": sorted(self.risk_declarer_roles),
            "tier_approvers": {t: {k.value: list(v) for k, v in m.items()} for t, m in self.tier_approvers.items()},
            "low_risk_max_changed_files": self.low_risk_max_changed_files,
            "approval_target_minutes": self.approval_target_minutes,
            "candidate_build_roles": sorted(self.candidate_build_roles),
            "baseline_build_roles": sorted(self.baseline_build_roles),
            "implementation_roles": sorted(self.implementation_roles),
            "reviewer_roles": sorted(self.reviewer_roles),
            "finding_agent_roles": sorted(self.finding_agent_roles),
            "transition_requesters": {k.value: sorted(v) for k, v in self.transition_requesters.items()},
            "require_failing_baseline": self.require_failing_baseline,
            "require_negative_path_test": self.require_negative_path_test,
            "require_domain_boundary": self.require_domain_boundary,
            "require_quality_evidence": self.require_quality_evidence,
            "required_quality_checks": list(self.required_quality_checks),
            "min_mutation_score_pct": self.min_mutation_score_pct,
            "merge_requesters": sorted(self.merge_requesters),
            "post_test_change_prefixes": list(self.post_test_change_prefixes),
            "require_release_provenance": self.require_release_provenance,
            "environments": list(self.environments),
            "deploy_requesters": sorted(self.deploy_requesters),
            "incident_packet_roles": sorted(self.incident_packet_roles),
            "incident_responder_roles": sorted(self.incident_responder_roles),
            "postmortem_roles": sorted(self.postmortem_roles),
        }

    @property
    def digest(self) -> str:
        return digest(self.as_dict())


DEFAULT_POLICY = Policy()
