"""Scenario-binding study: what the domain-first gate catches, and what it cannot.

Each case starts from the valid notifications fixture (testkit.World) and changes
one thing about how acceptance tests relate to the owner's scenarios. The gate's
decision on entering implementation is recorded. Three families:

  * semantic text changes to one scenario in C3 (negation, weakening, dropped
    condition, swapped actor, widened scope, replaced outcome);
  * presentation-only changes (double space, trailing period, capitalization,
    typographic apostrophe) that a reader would call "the same scenario";
  * structural and ownership changes (dropped, invented, proposed, test bindings
    swapped between scenarios, agent recorded as owner, no negative scenario).

Exact, verbatim comparison is a design choice. The study reports both sides of
it: every semantic change is caught, every presentation-only change is caught
too (a false positive by a reader's standard), and a swap of test bindings
between two scenarios is not caught by the gate at all; only the product owner
and QA approving C3 stand in the way.
"""

from typing import Any, Callable, Dict, List, Tuple

from .errors import GovernanceError
from .lifecycle import Stage
from .testkit import SCENARIOS, World

Field = str
Perturbation = Tuple[str, str, Field, Callable[[str], str]]


def _negate(text: str) -> str:
    if " do not " in text:
        return text.replace(" do not ", " ", 1)
    if " is " in text:
        return text.replace(" is ", " is not ", 1)
    return "it is not the case that " + text


PERTURBATIONS: List[Perturbation] = [
    ("semantic", "negation toggled", "then", _negate),
    ("semantic", "outcome weakened", "then", lambda t: t + ", in most cases"),
    ("semantic", "condition dropped", "given", lambda t: " ".join(t.split()[:2]) if len(t.split()) > 2 else "someone"),
    ("semantic", "actor swapped", "given", lambda t: t.replace("user", "administrator", 1) if "user" in t else "an administrator"),
    ("semantic", "scope widened", "when", lambda t: t + " or any other event"),
    ("semantic", "outcome replaced", "then", lambda t: "a summary is sent instead"),
    ("presentation", "double space", "when", lambda t: t.replace(" ", "  ", 1)),
    ("presentation", "trailing period", "then", lambda t: t + "."),
    ("presentation", "capitalized", "given", lambda t: t[:1].upper() + t[1:]),
    ("presentation", "typographic apostrophe or no-break space", "given",
     lambda t: t.replace("'", "’") if "'" in t else t.replace(" ", " ", 1)),
]


def _codes(decision: Dict[str, Any]) -> List[str]:
    return sorted({r["code"] for r in decision["reasons"]})


def _enter_implementation(world: World, c3_overrides: Dict[str, Any], drop: Tuple[str, ...] = ()) -> Dict[str, Any]:
    try:
        view = world.submit("uat_agent", "C3", "AC-NOTIFY", world.c3_body(drop, **c3_overrides))
    except GovernanceError as exc:  # refused before any gate decision
        return {"decision": "REFUSED_AT_SUBMISSION", "reasons": [{"code": exc.code}]}
    world.approve("po", view)
    world.approve("qa", view)
    world.refs["C3"] = {"artifact_id": view["artifact_id"], "version": view["version"], "digest": view["digest"]}
    world.make_baseline()
    return world.transition(Stage.IMPLEMENTATION)


def _bound(world: World) -> List[Dict[str, Any]]:
    return [dict(s) for s in world.c3_body()["scenarios"]]


def text_cases() -> List[Dict[str, Any]]:
    rows = []
    for family, name, field, change in PERTURBATIONS:
        for sid, *_ in SCENARIOS:
            world = World()
            world.advance_to(Stage.ACCEPTANCE_TESTS)
            scenarios = _bound(world)
            target = next(s for s in scenarios if s["scenario_id"] == sid)
            before = target[field]
            target[field] = change(before)
            decision = _enter_implementation(world, {"scenarios": scenarios})
            rows.append({"family": family, "perturbation": name, "scenario": sid, "field": field,
                         "changed": target[field] != before, "decision": decision["decision"],
                         "codes": _codes(decision), "caught": "SCENARIO_ALTERED" in _codes(decision)})
    return rows


def structural_cases() -> List[Dict[str, Any]]:
    rows = []

    def record(name: str, expected: str, decision: Dict[str, Any]) -> None:
        codes = _codes(decision)
        rows.append({"case": name, "expected": expected, "decision": decision["decision"], "codes": codes,
                     "caught": expected in codes if expected != "NONE" else decision["decision"] != "ALLOW"})

    world = World(); world.advance_to(Stage.ACCEPTANCE_TESTS)
    record("scenario dropped from C3", "SCENARIO_NOT_COVERED", _enter_implementation(world, {}, drop=("SC-4",)))

    world = World(); world.advance_to(Stage.ACCEPTANCE_TESTS)
    scenarios = _bound(world) + [{"scenario_id": "SC-9", "given": "any user", "when": "they log in",
                                  "then": "a welcome message is shown", "test_ids": ["UAT-001"]}]
    record("scenario invented in C3", "SCENARIO_NOT_IN_C1", _enter_implementation(world, {"scenarios": scenarios}))

    world = World(); world.advance_to(Stage.ACCEPTANCE_TESTS)
    proposal = [{"proposal_id": "P-1", "given": "a user in quiet hours", "when": "an optional event occurs",
                 "then": "it is held until morning", "rationale": "users asked for quiet hours"}]
    record("missing behavior proposed", "DOMAIN_PROPOSAL_PENDING",
           _enter_implementation(world, {"proposed_scenarios": proposal}))

    world = World(); world.advance_to(Stage.ACCEPTANCE_TESTS)
    scenarios = _bound(world)
    a, b = scenarios[0], scenarios[1]
    a["test_ids"], b["test_ids"] = b["test_ids"], a["test_ids"]
    record("test bindings swapped between two scenarios", "NONE", _enter_implementation(world, {"scenarios": scenarios}))

    world = World(); world.advance_to(Stage.EPIC)
    body = world.c1_body()
    body["domain_scenarios"][0]["decided_by"] = "requirements_agent"
    world.make_c1(domain_scenarios=body["domain_scenarios"])
    record("agent recorded as scenario owner", "SCENARIO_OWNER_INVALID", world.transition(Stage.SPIKE))

    world = World(); world.advance_to(Stage.EPIC)
    body = world.c1_body()
    only_normal = [s for s in body["domain_scenarios"] if s["kind"] == "NORMAL"]
    world.make_c1(domain_scenarios=only_normal)
    record("no negative or boundary scenario", "NEGATIVE_SCENARIO_MISSING", world.transition(Stage.SPIKE))
    return rows


def run() -> Dict[str, Any]:
    text = text_cases()
    structural = structural_cases()
    semantic = [r for r in text if r["family"] == "semantic"]
    presentation = [r for r in text if r["family"] == "presentation"]
    return {
        "text_cases": text,
        "structural_cases": structural,
        "summary": {
            "semantic_caught": sum(r["caught"] for r in semantic), "semantic_total": len(semantic),
            "presentation_caught": sum(r["caught"] for r in presentation), "presentation_total": len(presentation),
            "structural_as_expected": sum(r["caught"] for r in structural if r["expected"] != "NONE"),
            "structural_expected_total": sum(1 for r in structural if r["expected"] != "NONE"),
            "undetected": [r["case"] for r in structural if r["expected"] == "NONE" and r["decision"] == "ALLOW"],
        },
    }
