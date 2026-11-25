"""M5 study harness: reproducibility, ablation sensitivity and scoring sanity."""

from dataclasses import asdict

import pytest

from aidlc_g.incident_study import released_environment, run_study, score, wilson
from aidlc_g.ops import simulate


@pytest.fixture(scope="module")
def world():
    return released_environment()


@pytest.fixture(scope="module")
def study(world):
    env, release = world
    return run_study(per_class=4, seed=11, env=env, release=release)


def test_study_is_reproducible(world, study):
    env, release = world
    again = run_study(per_class=4, seed=11, env=env, release=release, run_tag="R2")
    assert [asdict(s) for s in again.scores] == [asdict(s) for s in study.scores]


def test_reference_packets_are_never_rejected(study):
    assert all(s.rejected is None for s in study.scores)


def test_release_evidence_ablation_loses_release_attribution(study):
    s = study.summary()
    assert s["full"]["RELEASE_REGRESSION"]["top1_correct"] == 4
    assert s["no_release_evidence"]["RELEASE_REGRESSION"]["top1_correct"] == 0
    assert s["no_release_evidence"]["PROVIDER_OUTAGE"]["top1_correct"] == 4  # unaffected classes stay unaffected


def test_runbook_ablation_loses_useful_first_steps(study):
    s = study.summary()
    assert s["full"]["ALL"]["first_step_useful_rate"] == 1.0
    assert s["no_runbooks"]["ALL"]["first_step_useful_rate"] == 0.0
    assert s["no_runbooks"]["ALL"]["top1_accuracy"] == s["full"]["ALL"]["top1_accuracy"]


def test_naive_baseline_falls_for_the_red_herring(study):
    s = study.summary()
    assert s["naive_baseline"]["RED_HERRING"]["top1_correct"] == 0
    assert s["naive_baseline"]["RELEASE_REGRESSION"]["top1_correct"] == 4
    assert s["full"]["RED_HERRING"]["change_assessed_rate"] == 1.0


def test_review_effort_is_measured(study):
    s = study.summary()
    assert s["full"]["ALL"]["mean_review_items"] > s["naive_baseline"]["ALL"]["mean_review_items"]
    assert s["full"]["ALL"]["mean_review_words"] > 0


def test_rejected_packet_scores_as_worst_case(world):
    env, release = world
    sc = simulate("CAPACITY", 1, release, env.baseline_digest)
    r = score(None, "UNCITED_EVIDENCE", sc, "x")
    assert not r.top1_correct and r.rejected == "UNCITED_EVIDENCE" and r.sim_minutes_to_restore == 30 + 25 + 10


def test_wilson_interval():
    lo, hi = wilson(40, 40)
    assert hi == 1.0 and 0.90 < lo < 0.92
    assert wilson(0, 0) == (0.0, 0.0)


def test_config_history_ablation_loses_config_attribution(study):
    s = study.summary()
    assert s["full"]["CONFIG_CHANGE"]["top1_correct"] == 4
    assert s["no_config_history"]["CONFIG_CHANGE"]["top1_correct"] == 0
    assert s["no_config_history"]["RELEASE_REGRESSION"]["top1_correct"] == 4


def test_conversation_history_is_context_not_attribution(study):
    # Under the fixed reasoner, prior threads are cited as history and never change the ranking.
    s = study.summary()
    assert s["no_conversation_history"]["ALL"]["top1_accuracy"] == s["full"]["ALL"]["top1_accuracy"]
