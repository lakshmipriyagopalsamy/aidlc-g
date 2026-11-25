"""Template M trial matrix, plus gate mutants proving the trial oracles can fail."""

import pytest

from aidlc_g import service as service_module
from aidlc_g.contracts import ContractType
from aidlc_g.policy import Policy
from aidlc_g.service import GovernanceService
from aidlc_g.testkit import World
from aidlc_g.trials import TRIALS, run_trial


@pytest.mark.parametrize("trial_id", [t[0] for t in TRIALS])
def test_trial_matches_predeclared_outcome(trial_id):
    result = run_trial(trial_id)
    assert result.passed, f"{trial_id}: expected {result.expected}, observed {result.observed}\n" + "\n".join(result.evidence)


def _trial_fn(trial_id):
    return next(t[4] for t in TRIALS if t[0] == trial_id)


# --- Mutants: disable one control; the matching trial must stop passing. --------


def test_mutant_signature_check_disabled_is_caught(monkeypatch):
    monkeypatch.setattr(service_module, "verify_payload", lambda *a, **k: True)
    observed, _ = _trial_fn("G06")(World())
    assert observed != "DENY"


def test_mutant_coverage_check_disabled_is_caught(monkeypatch):
    monkeypatch.setattr(GovernanceService, "_check_contract_content", lambda self, c, r: None)
    observed, _ = _trial_fn("G11")(World())
    assert observed != "DENY"


def test_mutant_invalidation_disabled_is_caught(monkeypatch):
    monkeypatch.setattr(GovernanceService, "_invalidate_artifact", lambda self, *a, **k: None)
    observed, _ = _trial_fn("G02")(World())
    assert observed != "INVALIDATE"


def test_mutant_policy_without_c1_approval_is_caught(monkeypatch):
    lax = Policy(approvers={**Policy().approvers, ContractType.C1: ()})
    # Defense in depth: with no C1 approvers configured, the domain owner's own
    # acceptance of their scenarios still has to be present, so G01 still denies.
    observed, _ = _trial_fn("G01")(World(policy=lax))
    assert observed == "DENY"
    monkeypatch.setattr(GovernanceService, "_check_domain_scenarios", lambda self, c1, r: c1[1].scenarios())
    observed, _ = _trial_fn("G01")(World(policy=lax))
    assert observed != "DENY"


def test_mutant_review_gate_disabled_is_caught(monkeypatch):
    monkeypatch.setattr(GovernanceService, "_check_review", lambda self, b, r, e: None)
    observed, _ = _trial_fn("G07")(World())
    assert observed != "DENY"


def test_mutant_domain_owner_check_disabled_is_caught(monkeypatch):
    monkeypatch.setattr(GovernanceService, "_check_domain_scenarios", lambda self, c1, r: c1[1].scenarios())
    observed, _ = _trial_fn("G13")(World())
    assert observed != "DENY"


def test_mutant_scenario_comparison_disabled_is_caught(monkeypatch):
    from aidlc_g.contracts import ScenarioText

    monkeypatch.setattr(ScenarioText, "text", lambda self: {})
    observed, _ = _trial_fn("G14")(World())
    assert observed != "DENY"


def test_mutant_domain_boundary_disabled_is_caught():
    observed, _ = _trial_fn("G15")(World(policy=Policy(require_domain_boundary=False)))
    assert observed != "DENY"


def test_mutant_stack_owner_check_disabled_is_caught(monkeypatch):
    original = GovernanceService._check_contract_content

    def without_stack_owner(self, contracts, reasons):
        original(self, contracts, reasons)
        reasons[:] = [r for r in reasons if r["code"] != "STACK_OWNER_INVALID"]

    monkeypatch.setattr(GovernanceService, "_check_contract_content", without_stack_owner)
    observed, _ = _trial_fn("G16")(World())
    assert observed != "DENY"


def test_mutant_stack_comparison_disabled_is_caught(monkeypatch):
    monkeypatch.setattr(GovernanceService, "_check_stack", staticmethod(lambda payload, stack, reasons, slot: None))
    observed, _ = _trial_fn("G17")(World())
    assert observed != "DENY"


def test_mutant_quality_waiver_check_disabled_is_caught(monkeypatch):
    original = GovernanceService._check_quality

    def accepts_anything_not_run(self, build, c3, reasons, evidence, stack=None):
        original(self, build, c3, reasons, evidence, stack)
        reasons[:] = [r for r in reasons if r["code"] != "QUALITY_CHECK_NOT_RUN"]

    monkeypatch.setattr(GovernanceService, "_check_quality", accepts_anything_not_run)
    observed, _ = _trial_fn("G18")(World())
    assert observed != "DENY"
