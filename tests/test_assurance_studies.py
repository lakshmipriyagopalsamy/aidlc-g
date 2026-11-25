"""Model checking, baseline comparison and invalidation studies (RQ1, RQ2 and RQ4 of the evaluation)."""

from aidlc_g.baseline_study import BASELINES, trial_outcomes
from aidlc_g.invalidation_study import concurrency_cases, expected_dependents, targeted_cases
from aidlc_g.model_check import run as model_check


def test_every_model_holds_and_every_mutant_is_caught():
    r = model_check()
    assert all(m["holds"] for m in r["models"].values())
    assert all(m["caught"] for m in r["mutants"].values()), r["mutants"]


def test_full_gate_stops_every_trial_and_baselines_miss_the_cross_artifact_ones():
    full = trial_outcomes(BASELINES["AIDLC-G"]["off"])
    assert all(t["stopped"] for t in full if t["violating"])
    assert not any(t["false_block"] for t in full if not t["violating"])
    hardened = {t["trial"]: t for t in trial_outcomes(BASELINES["B2"]["off"])}
    assert not hardened["G02"]["stopped"]  # changed requirement: downstream approvals stay valid without invalidation
    assert not hardened["G14"]["stopped"]  # reworded scenario: no verbatim binding
    assert hardened["G01"]["stopped"]  # code-owner review stops the unauthorized approval
    assert not any(t["false_block"] for t in hardened.values() if not t["violating"])


def test_dependency_table_is_the_lifecycle_graph():
    assert expected_dependents("DOCS") == {"RELEASE"}
    assert expected_dependents("C2") == {"STORIES", "C3", "BUILD", "DOCS", "RELEASE"}
    assert "C1" not in expected_dependents("C2")


def test_changes_invalidate_exactly_their_dependents():
    for case in targeted_cases():
        assert not case["missed"] and not case["extra"], case
        assert case["stale_approvals_aidlcg"] == 0
        assert case["reapprovals_targeted"] <= case["reapprovals_restart_all"]


def test_racing_change_never_admits_on_invalidated_evidence():
    r = concurrency_cases(runs=8)
    assert r["violations"] == 0 and r["readmitted"] == 0
