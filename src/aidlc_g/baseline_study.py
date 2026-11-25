"""Baseline comparison: the same trials and adversarial slice under emulated conventional configurations.

Agents, agent-side validation, repository permissions, the CI runner and its signatures are held
constant. Only the transition evaluator changes. Each baseline switches off, inside the AIDLC-G gate,
exactly the controls that the baseline's documented *default* configuration does not provide, so a
difference in outcome is attributable to those controls.

Baselines (capabilities from public documentation; real tools can be configured or extended to do more,
and this study does not claim otherwise):

  B1  Conventional CI/CD, default protected branch: required approvals from anyone with write access,
      approvals not dismissed when new commits arrive, required status checks from CI, no code owners,
      no provenance verification at deployment.
  B2  Conventional CI/CD, hardened: B1 plus code-owner review (role-bound approval), stale approvals
      dismissed on new commits (version-bound approval), and provenance verification at deployment.
  B3  Agentic workflow with human stage checkpoints, as documented for AI-DLC and Spec Kit: a person
      validates each stage's output; validation is a workflow step, not bound to an artifact version or
      role; cross-artifact consistency analysis is advisory; repository CI as in B1.

Controls a baseline lacks, and how each is switched off:

  role_binding            any human may approve any artifact (all approval roles granted to humans)
  version_binding         an approval of any version of an artifact counts for later versions
  dependency_invalidation a new version invalidates nothing downstream
  domain_contract         no scenario ownership, design/story tracing, verbatim binding, coverage, or
                          negative-scenario rule (content checks off)
  failing_baseline        no requirement that locked tests fail on a stub first
  domain_boundary         no check that acceptance tests use only the domain interface
  stack_binding           no check that CI ran the stack the team decided
  waiver_enforcement      a quality check the stack cannot run counts as passed
  release_provenance      no signed-provenance check before release and deployment
"""

import dataclasses
from contextlib import ExitStack, contextmanager
from typing import Any, Dict, Iterator, List, Tuple
from unittest import mock

from .policy import DEFAULT_POLICY, Policy
from .service import HUMAN, GovernanceService
from .trials import TRIALS
from .testkit import World

CONTROLS = ("role_binding", "version_binding", "dependency_invalidation", "domain_contract", "failing_baseline",
            "domain_boundary", "stack_binding", "waiver_enforcement", "release_provenance")

BASELINES: Dict[str, Dict[str, Any]] = {
    "AIDLC-G": {"label": "AIDLC-G (all controls)", "off": ()},
    "B1": {"label": "Conventional CI/CD, default protection",
           "off": ("role_binding", "version_binding", "dependency_invalidation", "domain_contract", "failing_baseline",
                   "domain_boundary", "stack_binding", "waiver_enforcement", "release_provenance")},
    "B2": {"label": "Conventional CI/CD, hardened",
           "off": ("dependency_invalidation", "domain_contract", "failing_baseline", "domain_boundary",
                   "stack_binding", "waiver_enforcement")},
    "B3": {"label": "Agentic workflow, human stage checkpoints",
           "off": ("role_binding", "version_binding", "dependency_invalidation", "domain_contract", "failing_baseline",
                   "domain_boundary", "stack_binding", "waiver_enforcement", "release_provenance")},
}


def policy_for(off: Tuple[str, ...]) -> Policy:
    return dataclasses.replace(
        DEFAULT_POLICY,
        require_failing_baseline="failing_baseline" not in off,
        require_domain_boundary="domain_boundary" not in off,
        require_release_provenance="release_provenance" not in off,
    )


def _sticky_approval_state(self: GovernanceService, row: Any) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """Approval state where an approval of any earlier version still counts (approvals not dismissed)."""
    from .contracts import ContractType
    required = self._policy.approvers[ContractType(row["contract_type"])]
    records = self._db.execute(
        "SELECT * FROM approvals WHERE artifact_id = ? AND version <= ? AND policy_version = ? ORDER BY timestamp",
        (row["artifact_id"], row["version"], self._policy.version)).fetchall()
    current = [r for r in records if r["version"] == row["version"]]
    if any(r["decision"] == "REJECT" for r in current):
        return "REJECTED", list(required), []
    if any(r["decision"] == "REQUEST_CHANGES" for r in current):
        return "RETURNED", list(required), []
    covered: Dict[str, Dict[str, Any]] = {}
    for r in records:
        approver = self._principal(r["approver_id"])
        if approver and approver.active and approver.kind == HUMAN and r["approver_role"] in approver.roles \
                and r["decision"] == "APPROVE":
            covered.setdefault(r["approver_role"], {"approval_id": r["approval_id"], "approver_id": r["approver_id"],
                                                    "role": r["approver_role"], "timestamp": r["timestamp"]})
    missing = [role for role in required if role not in covered]
    return ("APPROVED" if not missing else "PENDING"), missing, list(covered.values())


@contextmanager
def configured(off: Tuple[str, ...]) -> Iterator[Policy]:
    """Patch the gate so that the listed controls are absent, for the duration of the block."""
    policy = policy_for(off)
    with ExitStack() as stack:
        if "role_binding" in off:
            approval_roles = sorted({r for roles in policy.approvers.values() for r in roles})
            original = GovernanceService.register_principal

            def register(self: GovernanceService, token: str, pid: str, name: str, kind: str, roles: List[str]):
                if kind == HUMAN:
                    roles = sorted(set(roles) | set(approval_roles))
                return original(self, token, pid, name, kind, roles)
            stack.enter_context(mock.patch.object(GovernanceService, "register_principal", register))
            original_approve = GovernanceService.approve

            def approve(self: GovernanceService, token: str, artifact_id: str, version: int, artifact_digest: str,
                        decision: str = "APPROVE", as_role: Any = None, rationale: Any = None):
                if as_role is None:  # "any writer may approve": the approval fills the first uncovered required role
                    from .contracts import ContractType
                    row = self._artifact(artifact_id, version)
                    if row is not None:
                        _, missing, _ = self._approval_state(row)
                        required = list(self._policy.approvers[ContractType(row["contract_type"])])
                        as_role = (missing or required)[0]
                return original_approve(self, token, artifact_id, version, artifact_digest, decision, as_role, rationale)
            stack.enter_context(mock.patch.object(GovernanceService, "approve", approve))
        if "version_binding" in off:
            stack.enter_context(mock.patch.object(GovernanceService, "_approval_state", _sticky_approval_state))
        if "dependency_invalidation" in off:
            stack.enter_context(mock.patch.object(GovernanceService, "_invalidate_artifact", lambda self, *a, **k: None))
        if "domain_contract" in off:
            stack.enter_context(mock.patch.object(GovernanceService, "_check_contract_content", lambda self, c, r: None))
        if "stack_binding" in off:
            stack.enter_context(mock.patch.object(GovernanceService, "_check_stack",
                                                  staticmethod(lambda payload, stack_, reasons, slot: None)))
        if "waiver_enforcement" in off:
            original_quality = GovernanceService._check_quality

            def quality(self: GovernanceService, build: Any, c3: Any, reasons: List[Dict[str, Any]],
                        evidence: Dict[str, Any], stack_: Any = None) -> None:
                before = len(reasons)
                original_quality(self, build, c3, reasons, evidence, stack_)
                reasons[before:] = [r for r in reasons[before:] if r["code"] != "QUALITY_CHECK_NOT_RUN"]
            stack.enter_context(mock.patch.object(GovernanceService, "_check_quality", quality))
        bootstrap = GovernanceService.bootstrap.__func__

        def boot(cls: Any, db_path: str = ":memory:", policy: Policy = policy, clock: Any = None,  # noqa: B008
                 admin_id: str = "admin"):
            return bootstrap(cls, db_path, policy_for(off) if policy is DEFAULT_POLICY else policy, clock, admin_id)
        stack.enter_context(mock.patch.object(GovernanceService, "bootstrap", classmethod(boot)))
        yield policy


def trial_outcomes(off: Tuple[str, ...]) -> List[Dict[str, Any]]:
    rows = []
    with configured(off) as policy:
        for tid, scenario, expected, violating, fn in TRIALS:
            try:
                observed, _ = fn(World(policy=policy))
            except Exception as exc:  # a baseline may break a trial's own setup; that is not a detection
                observed = f"ERROR:{type(exc).__name__}"
            rows.append({"trial": tid, "scenario": scenario, "violating": violating, "expected": expected,
                         "observed": observed,
                         "stopped": observed == expected if violating else None,
                         "false_block": (observed != expected) if not violating else None})
    return rows


def slice_outcomes(off: Tuple[str, ...]) -> List[Dict[str, Any]]:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import run_vertical_slice  # noqa: E402
    rows = []
    with configured(off):
        happy = run_vertical_slice.run()
        rows.append({"scenario": "Happy path (no misbehavior)", "violating": False,
                     "outcome": run_vertical_slice.outcome(happy), "stopped": not happy.completed})
        for name, expected, make in run_vertical_slice.scenarios():
            try:
                report = make()
                outcome, stopped = run_vertical_slice.outcome(report), not report.completed
            except Exception as exc:
                outcome, stopped = f"error {type(exc).__name__}", True
            rows.append({"scenario": name, "violating": True, "expected": expected, "outcome": outcome,
                         "stopped": stopped})
    return rows


def run(include_slice: bool = True) -> Dict[str, Any]:
    result: Dict[str, Any] = {"baselines": {}, "controls": list(CONTROLS)}
    for key, spec in BASELINES.items():
        trials = trial_outcomes(spec["off"])
        entry: Dict[str, Any] = {"label": spec["label"], "off": list(spec["off"]), "trials": trials}
        viol = [t for t in trials if t["violating"]]
        pos = [t for t in trials if not t["violating"]]
        entry["trial_stopped"] = sum(t["stopped"] for t in viol)
        entry["trial_violating"] = len(viol)
        entry["false_blocks"] = sum(t["false_block"] for t in pos)
        entry["positives"] = len(pos)
        if include_slice:
            sl = slice_outcomes(spec["off"])
            entry["slice"] = sl
            adv = [s for s in sl if s["violating"]]
            entry["slice_stopped"] = sum(s["stopped"] for s in adv)
            entry["slice_total"] = len(adv)
            entry["happy_path_released"] = not sl[0]["stopped"]
        result["baselines"][key] = entry
    return result
