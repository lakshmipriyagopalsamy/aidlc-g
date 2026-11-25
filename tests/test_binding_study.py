"""The scenario-binding study behind the domain-first paper: what verbatim binding catches and misses."""

from aidlc_g.binding_study import PERTURBATIONS, run


def test_every_text_perturbation_changes_the_text():
    from aidlc_g.testkit import SCENARIOS
    for family, name, field, change in PERTURBATIONS:
        for sid, rule, kind, given, when, then, _ in SCENARIOS:
            before = {"given": given, "when": when, "then": then}[field]
            assert change(before) != before, (name, sid)


def test_study_reports_catches_and_the_known_miss():
    s = run()["summary"]
    assert s["semantic_caught"] == s["semantic_total"] == 24
    assert s["presentation_caught"] == s["presentation_total"] == 16  # exact binding also blocks harmless edits
    assert s["structural_as_expected"] == s["structural_expected_total"] == 5
    assert s["undetected"] == ["test bindings swapped between two scenarios"]  # left to human C3 approval
