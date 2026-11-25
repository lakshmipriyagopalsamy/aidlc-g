"""Domain-first acceptance: people decide the behavior, agents translate it into tests.

C1 carries domain scenarios owned by a named human domain owner. The gate checks
that the owner is real and approved that exact version, that the design and the
stories trace every scenario, that C3 binds each one word for word without
inventing or rewording behavior, and that acceptance tests reach the code only
through the domain interface C2 names.
"""

import pytest

from aidlc_g.examples import notifications as ex
from aidlc_g.lifecycle import Stage
from aidlc_g.orchestrator import SliceEnvironment, SliceOrchestrator
from aidlc_g.policy import HUMAN
from aidlc_g.runner import DYNAMIC_IMPORT, project_imports
from aidlc_g.service import ALLOW, BLOCK, PAUSE
from aidlc_g.testkit import World, expect_error, ref


def codes(decision):
    return [r["code"] for r in decision["reasons"]]


def scenarios_with(w, **changes):
    """C1 scenarios with field changes applied to every scenario, or per id via {id: {...}}."""
    body = w.c1_body()["domain_scenarios"]
    per_id = changes.pop("per_id", {})
    return [{**s, **changes, **per_id.get(s["scenario_id"], {})} for s in body]


# --- C1: the domain owner decides -------------------------------------------------


def test_agent_cannot_own_a_scenario():
    w = World()
    w.advance_to(Stage.EPIC)
    w.make_c1(domain_scenarios=scenarios_with(w, per_id={"SC-2": {"decided_by": "requirements_agent"}}))
    decision = w.transition(Stage.SPIKE)
    assert decision["decision"] == BLOCK and "SCENARIO_OWNER_INVALID" in codes(decision)


def test_a_human_without_domain_authority_cannot_own_a_scenario():
    w = World()
    w.advance_to(Stage.EPIC)
    w.make_c1(domain_scenarios=scenarios_with(w, decided_by="eng_lead"))
    assert "SCENARIO_OWNER_INVALID" in codes(w.transition(Stage.SPIKE))


def test_named_owner_must_approve_their_own_scenarios():
    w = World()
    w.add_principal("po2", HUMAN, ["product_owner"])
    w.advance_to(Stage.EPIC)
    w.make_c1(domain_scenarios=scenarios_with(w, per_id={"SC-4": {"decided_by": "po2"}}))  # po approves
    decision = w.transition(Stage.SPIKE)
    assert decision["decision"] == PAUSE and codes(decision) == ["SCENARIO_NOT_ACCEPTED"]
    w.approve("po2", w.svc.get_artifact(w.admin, "BR-NOTIFY"))
    assert w.transition(Stage.SPIKE)["decision"] == ALLOW


def test_owner_who_left_no_longer_owns_the_behavior():
    w = World()
    w.advance_to(Stage.SPIKE)
    w.svc.set_principal_active(w.admin, "po", False)
    w.make_c2()
    assert "SCENARIO_OWNER_INVALID" in codes(w.transition(Stage.STORIES))


def test_owner_must_decide_some_negative_or_boundary_behavior():
    w = World()
    w.advance_to(Stage.EPIC)
    w.make_c1(domain_scenarios=scenarios_with(w, kind="NORMAL"))
    assert "NEGATIVE_SCENARIO_MISSING" in codes(w.transition(Stage.SPIKE))


# --- C2 and stories trace every scenario --------------------------------------------


def test_design_must_answer_every_scenario():
    w = World()
    w.advance_to(Stage.SPIKE)
    designs = w.c2_body()["scenario_designs"]
    w.make_c2(scenario_designs=designs[:3])
    decision = w.transition(Stage.STORIES)
    assert decision["decision"] == PAUSE and codes(decision) == ["SCENARIO_NOT_DESIGNED"]


def test_design_cannot_answer_scenarios_nobody_decided():
    w = World()
    w.advance_to(Stage.SPIKE)
    designs = w.c2_body()["scenario_designs"] + [{"scenario_id": "SC-9", "design_response": "quiet hours"}]
    w.make_c2(scenario_designs=designs)
    assert "UNKNOWN_SCENARIO" in codes(w.transition(Stage.STORIES))


def test_stories_must_deliver_every_scenario_and_add_none():
    w = World()
    w.advance_to(Stage.STORIES)
    body = w.stories_body()
    body["stories"][1]["scenario_ids"] = ["SC-3", "SC-9"]  # SC-4 dropped, SC-9 invented
    view = w.submit("story_agent", "STORIES", "STORIES-NOTIFY", body)
    w.approve("eng_lead", view)
    w.approve("po", view)
    w.refs["STORIES"] = ref(view)
    found = codes(w.transition(Stage.ACCEPTANCE_TESTS))
    assert "SCENARIO_NOT_PLANNED" in found and "UNKNOWN_SCENARIO" in found


# --- C3 binds scenarios word for word ---------------------------------------------


def c3_decision(w, **overrides):
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    w.make_c3(**overrides)
    w.make_baseline()
    return w.transition(Stage.IMPLEMENTATION)


def test_test_author_cannot_reword_a_scenario():
    w = World()
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    body = w.c3_body()
    body["scenarios"][1]["then"] = "the alert is usually delivered"
    w.make_c3(scenarios=body["scenarios"])
    w.make_baseline()
    decision = w.transition(Stage.IMPLEMENTATION)
    assert decision["decision"] == BLOCK and "SCENARIO_ALTERED" in codes(decision)


def test_test_author_cannot_invent_behavior():
    w = World()
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    body = w.c3_body()
    body["scenarios"][3]["scenario_id"] = "SC-9"
    w.make_c3(scenarios=body["scenarios"])
    w.make_baseline()
    found = codes(w.transition(Stage.IMPLEMENTATION))
    assert "SCENARIO_NOT_IN_C1" in found and "SCENARIO_NOT_COVERED" in found


def test_proposed_behavior_goes_back_to_the_domain_owner():
    w = World()
    decision = c3_decision(w, proposed_scenarios=[{
        "proposal_id": "P-1", "rationale": "re-enabling is not covered",
        "given": "a user who turned optional notifications off", "when": "they turn them back on",
        "then": "optional notifications are delivered again"}])
    assert decision["decision"] == PAUSE and codes(decision) == ["DOMAIN_PROPOSAL_PENDING"]


def test_changing_a_scenario_invalidates_everything_built_on_it():
    w = World()
    w.advance_to(Stage.IMPLEMENTATION)
    v2 = w.submit("requirements_agent", "C1", "BR-NOTIFY",
                  w.c1_body(domain_scenarios=scenarios_with(w, per_id={"SC-1": {"then": "a weekly digest is sent"}})))
    assert {"TR-NOTIFY@v1", "STORIES-NOTIFY@v1", "AC-NOTIFY@v1", "build:BLD-STUB-1"} <= set(v2["invalidated"])
    # The old tests no longer define done: nothing can be built against them.
    assert expect_error(w.make_candidate) == "MANIFEST_NOT_LOCKED"
    assert w.transition(Stage.IMPLEMENTATION, refs={**w.refs, "C1": ref(v2)})["decision"] == BLOCK


# --- Tests exercise behavior through the domain interface --------------------------


@pytest.mark.parametrize("imports", [["notifications.store"], ["legacy_email_prefs"], [DYNAMIC_IMPORT]])
def test_acceptance_tests_may_not_reach_past_the_domain_interface(imports):
    w = World()
    w.test_imports["UAT-003"] = imports
    decision = c3_decision(w)
    assert decision["decision"] == BLOCK and "TEST_BYPASSES_DOMAIN_INTERFACE" in codes(decision)


def test_runner_reports_project_imports_only():
    tree = {
        "src/notifications/__init__.py": b"",
        "src/legacy_email_prefs.py": b"",
        "uat/a.py": b"import pytest\nimport json\nfrom notifications import X\nfrom notifications.store import Y\n",
        "uat/b.py": b"import importlib\nm = importlib.import_module('notifications')\n",
        "uat/c.py": b"from . import helpers\n",
        "uat/d.py": b"x = __import__('legacy_email_prefs')\n",
        "uat/e.py": b"def broken(:\n",
        "uat/f.py": b"import legacy_email_prefs as prefs\n",
    }
    assert project_imports(tree, "uat/a.py") == ["notifications", "notifications.store"]
    for path in ("uat/b.py", "uat/c.py", "uat/d.py", "uat/e.py"):
        assert DYNAMIC_IMPORT in project_imports(tree, path), path
    assert project_imports(tree, "uat/f.py") == ["legacy_email_prefs"]


# --- End to end with agents ---------------------------------------------------------


def run_with(task, fn):
    env = SliceEnvironment.create(ex.SEED)
    orchestrator = SliceOrchestrator(env, ex.model().with_override("acceptance_test_agent", task, fn), ex.desk())
    return env, orchestrator.run(ex.REQUEST)


def halted(report):
    return report.halted["decision"], [r["code"] for r in report.halted.get("reasons", [])]


def test_owner_decisions_not_agent_candidates_reach_c1():
    env = SliceEnvironment.create(ex.SEED)
    report = SliceOrchestrator(env, ex.model(), ex.desk()).run(ex.REQUEST)
    assert report.completed
    c1 = env.gate.get_artifact(env.admin, "BR-NOTIFY")["body"]
    assert [s["scenario_id"] for s in c1["domain_scenarios"]] == ["SC-1", "SC-2", "SC-3", "SC-4"]
    assert {s["decided_by"] for s in c1["domain_scenarios"]} == {"po"}
    decisions = {d["scenario_id"]: d["decision"] for d in report.domain_decisions}
    assert decisions == {"SC-1": "KEPT", "SC-2": "REWRITTEN", "SC-3": "KEPT",
                         "SC-Q": "DROPPED (agent proposal)", "SC-4": "ADDED BY OWNER"}
    assert "Domain scenarios (decided by the domain owner)" in report.to_markdown()


@pytest.mark.parametrize("variant, expected", [
    (ex.rewording_tests, (BLOCK, "SCENARIO_ALTERED")),
    (ex.inventing_tests, (BLOCK, "SCENARIO_NOT_IN_C1")),
    (ex.proposing_tests, (PAUSE, "DOMAIN_PROPOSAL_PENDING")),
    (ex.internal_tests, (BLOCK, "TEST_BYPASSES_DOMAIN_INTERFACE")),
])
def test_test_agent_translates_but_does_not_decide(variant, expected):
    _, report = run_with("author_tests", variant)
    decision, found = halted(report)
    assert report.halted["stage"] == "IMPLEMENTATION"
    assert decision == expected[0] and expected[1] in found
