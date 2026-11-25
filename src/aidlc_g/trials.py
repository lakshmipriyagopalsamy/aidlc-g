"""Template M governance trial matrix (G01-G12) plus a positive re-approval control.

Each trial builds a fresh World, injects one mutation and reports the
observed policy outcome. Expected outcomes are fixed here, before any run,
so the matrix is a predeclared oracle rather than a post-hoc label.
"""

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from .canonical import file_digest
from .crypto import generate_private_key
from .lifecycle import Stage
from .service import ALLOW, PAUSE
from .stacks import JAVA_SPRING_MAVEN, instructions_digest, profile_digest
from .testkit import World, expect_error, ref

DENY = "DENY"
INVALIDATE = "INVALIDATE"
IDEMPOTENT = "IDEMPOTENT_SINGLE_COMMIT"


@dataclass
class TrialResult:
    trial_id: str
    scenario: str
    expected: str
    observed: str
    violating: bool
    evidence: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.expected == self.observed


def _codes(decision: Dict) -> List[str]:
    return [r["code"] for r in decision["reasons"]]


def _all(checks: List[Tuple[bool, str]]) -> Tuple[bool, List[str]]:
    return all(ok for ok, _ in checks), [f"{'ok ' if ok else 'BAD'} {msg}" for ok, msg in checks]


def g01_unauthorized_product_approval(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.EPIC)
    view = w.submit("requirements_agent", "C1", "BR-NOTIFY", w.c1_body())
    w.add_principal("intruder_agent", "agent", ["product_owner"])  # agent holding a human role
    codes = [
        expect_error(lambda: w.approve("eng_lead", view)),  # wrong authority
        expect_error(lambda: w.approve("intruder_agent", view)),  # not a human
        expect_error(lambda: w.approve("requirements_agent", view)),  # author / agent
    ]
    w.refs["C1"] = ref(view)
    decision = w.transition(Stage.SPIKE)
    ok, ev = _all([(all(c is not None for c in codes), f"approvals refused: {codes}"),
                   (decision["decision"] == PAUSE, f"transition {decision['decision']} {_codes(decision)}")])
    return (DENY if ok else ALLOW), ev


def g02_c1_change_after_approval(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.SPIKE)
    w.make_c2()
    old_c1, old_c2 = dict(w.refs["C1"]), dict(w.refs["C2"])
    v2 = w.submit("requirements_agent", "C1", "BR-NOTIFY",
                  w.c1_body(requested_behavior="A signed-in user can disable optional notifications per channel"))
    c1v1 = w.svc.get_artifact(w.admin, "BR-NOTIFY", 1)
    c2v1 = w.svc.get_artifact(w.admin, "TR-NOTIFY", 1)
    stale = w.transition(Stage.STORIES, refs={**w.refs, "C1": old_c1, "C2": old_c2})
    fresh = w.transition(Stage.STORIES, refs={**w.refs, "C1": ref(v2), "C2": old_c2})
    ok, ev = _all([
        (c1v1["status"] == "SUPERSEDED" and c1v1["approval_state"] == "PENDING", f"C1 v1 {c1v1['status']}"),
        (c2v1["status"] == "INVALIDATED", f"downstream C2 v1 {c2v1['status']}"),
        ("TR-NOTIFY@v1" in v2["invalidated"], f"impact list {v2['invalidated']}"),
        (stale["decision"] != ALLOW, f"stale refs {stale['decision']} {_codes(stale)}"),
        (fresh["decision"] != ALLOW, f"unapproved v2 {fresh['decision']} {_codes(fresh)}"),
    ])
    return (INVALIDATE if ok else ALLOW), ev


def g02r_reapproval_restores_progress(w: World) -> Tuple[str, List[str]]:
    """Positive control for G02: targeted re-approval lets the work item continue."""
    w.advance_to(Stage.SPIKE)
    w.make_c2()
    w.make_c1(requested_behavior="A signed-in user can disable optional notifications per channel")
    w.make_c2(feasibility_and_cost="Low; one migration plus per-channel column")
    decision = w.transition(Stage.STORIES)
    return ("ALLOW" if decision["decision"] == ALLOW else DENY), [f"{decision['decision']} {_codes(decision)}"]


def g03_implementer_replaces_uat(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.IMPLEMENTATION)
    rewrite = expect_error(lambda: w.svc.submit_contract(
        w.tok("impl_agent"), "C3", "AC-NOTIFY", w.c3_body()))
    w.test_files["UAT-002"] = b"def test_critical_delivered(app):\n    assert True\n"  # weakened test
    w.make_candidate()
    decision = w.transition(Stage.TEST_COVERAGE)
    ok, ev = _all([(rewrite == "ROLE_NOT_PERMITTED", f"manifest rewrite refused: {rewrite}"),
                   ("TEST_INTEGRITY_VIOLATION" in _codes(decision), f"{decision['decision']} {_codes(decision)}")])
    return (DENY if ok else ALLOW), ev


def g04_stale_or_wrong_manifest(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.IMPLEMENTATION)
    w.register_build("BLD-1")
    wrong = w.attestation("BLD-1", manifest_digest="sha256:" + "ab" * 32)
    wrong_code = expect_error(lambda: w.svc.ingest_attestation(wrong, w.sign(wrong)))
    w.attest("BLD-1")
    w.refs["BUILD"] = "BLD-1"
    # QA revises the locked manifest (new version); BLD-1 evidence is now stale.
    revised = b"def test_unknown_category_rejected(app):\n    assert app.reject('x')\n"
    w.test_files["UAT-004"] = revised
    body = w.c3_body()
    body["tests"] = [dict(t, content_digest=file_digest(revised)) if t["test_id"] == "UAT-004" else t
                     for t in body["tests"]]
    v2 = w.submit("uat_agent", "C3", "AC-NOTIFY", body)
    w.approve("po", v2)
    w.approve("qa", v2)
    decision = w.transition(Stage.TEST_COVERAGE, refs={**w.refs, "C3": ref(v2)})
    ok, ev = _all([(wrong_code == "MANIFEST_BINDING_MISMATCH", f"wrong-manifest attestation: {wrong_code}"),
                   (decision["decision"] == "BLOCK_WITH_REASONS", f"stale build {decision['decision']} {_codes(decision)}")])
    return (DENY if ok else ALLOW), ev


def g05_required_uat_absent(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.IMPLEMENTATION)
    w.register_build("BLD-1")
    w.attest("BLD-1", skip_tests=("UAT-004",))
    w.refs["BUILD"] = "BLD-1"
    decision = w.transition(Stage.TEST_COVERAGE)
    ok = "REQUIRED_TEST_RESULT_MISSING" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def g06_tampered_signature(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.IMPLEMENTATION)
    w.register_build("BLD-1")
    payload = w.attestation("BLD-1", outcome="FAILED")
    signature = w.sign(payload)
    forged = dict(payload, results=[dict(r, outcome="PASSED") for r in payload["results"]])
    tampered = expect_error(lambda: w.svc.ingest_attestation(forged, signature))
    unknown_key = w.attestation("BLD-1")
    wrong_key = expect_error(lambda: w.svc.ingest_attestation(unknown_key, w.sign(unknown_key, generate_private_key())))
    rogue = w.attestation("BLD-1", runner_id="rogue-runner")
    rogue_code = expect_error(lambda: w.svc.ingest_attestation(rogue, w.sign(rogue, generate_private_key())))
    garbage = expect_error(lambda: w.svc.ingest_attestation(w.attestation("BLD-1"), "not-base64!"))
    w.refs["BUILD"] = "BLD-1"
    decision = w.transition(Stage.TEST_COVERAGE)
    ok, ev = _all([
        (tampered == "INVALID_SIGNATURE", f"modified payload: {tampered}"),
        (wrong_key == "INVALID_SIGNATURE", f"unregistered key: {wrong_key}"),
        (rogue_code == "UNKNOWN_RUNNER", f"unknown runner: {rogue_code}"),
        (garbage == "INVALID_SIGNATURE", f"malformed signature: {garbage}"),
        (decision["decision"] != ALLOW, f"no accepted evidence -> {decision['decision']} {_codes(decision)}"),
    ])
    return (DENY if ok else ALLOW), ev


def g07_implementer_self_review(w: World) -> Tuple[str, List[str]]:
    w.add_principal("dev", "human", ["engineer", "reviewer"])
    w.advance_to(Stage.IMPLEMENTATION)
    w.make_candidate(registrant="dev")
    w.transition(Stage.TEST_COVERAGE)
    w.make_quality()  # isolate the review control
    code = expect_error(lambda: w.make_review(reviewer="dev"))
    decision = w.transition(Stage.DOCUMENTATION)
    ok, ev = _all([(code == "SEPARATION_OF_DUTIES", f"self review: {code}"),
                   (decision["decision"] != ALLOW, f"{decision['decision']} {_codes(decision)}")])
    return (DENY if ok else ALLOW), ev


def g08_all_valid(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.RELEASE_READY)
    manifest = w.svc.release_manifest(w.tok("rel_mgr"), w.work_item)
    ok = manifest["build_digest"] == w.builds["BLD-1"]["build_digest"]
    return ("ALLOW" if ok else DENY), [f"release {manifest['release_id']} -> {manifest['build_digest'][:19]}…"]


def g09_replay_attestation(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.IMPLEMENTATION)
    w.register_build("BLD-1")
    payload = w.attestation("BLD-1")
    signature = w.sign(payload)
    w.svc.ingest_attestation(payload, signature)
    replay = expect_error(lambda: w.svc.ingest_attestation(payload, signature))
    # A new build cannot borrow BLD-1's evidence: attestations are bound to one build.
    w.register_build("BLD-2", revision="00ddba11c0de")
    w.refs["BUILD"] = "BLD-2"
    decision = w.transition(Stage.TEST_COVERAGE)
    ok, ev = _all([(replay == "REPLAY_DETECTED", f"replay: {replay}"),
                   ("MISSING_EVIDENCE" in _codes(decision), f"borrowed evidence {decision['decision']} {_codes(decision)}")])
    return (DENY if ok else ALLOW), ev


def g10_concurrent_duplicate(w: World, threads: int = 8) -> Tuple[str, List[str]]:
    w.make_c0()
    same: List[Dict] = []
    barrier = threading.Barrier(threads)

    def same_key() -> None:
        barrier.wait()
        same.append(w.transition(Stage.EPIC, key="IDEM-SHARED"))

    workers = [threading.Thread(target=same_key) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    w.make_c1()
    distinct: List[Dict] = []
    barrier2 = threading.Barrier(threads)

    def own_key(i: int) -> None:
        barrier2.wait()
        distinct.append(w.transition(Stage.SPIKE, key=f"IDEM-OWN-{i}"))

    workers = [threading.Thread(target=own_key, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    stage = w.svc.get_work_item(w.tok("orchestrator"), w.work_item)["stage"]
    ok, ev = _all([
        (len({d["transition_id"] for d in same}) == 1, f"same key -> {len({d['transition_id'] for d in same})} decision(s)"),
        (sum(1 for d in same if not d["replayed"]) == 1, "exactly one evaluation committed"),
        (sum(1 for d in distinct if d["decision"] == ALLOW) == 1,
         f"distinct keys -> {sum(1 for d in distinct if d['decision'] == ALLOW)} ALLOW"),
        (stage == Stage.SPIKE.value, f"final stage {stage}"),
    ])
    return (IDEMPOTENT if ok else "DUPLICATE_COMMIT"), ev


def g11_criterion_not_covered(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    w.make_c3(drop_scenarios=("SC-4",))
    w.make_baseline()
    decision = w.transition(Stage.IMPLEMENTATION)
    ok = "SCENARIO_NOT_COVERED" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def g12_release_docs_missing(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.DOCUMENTATION)
    body = w.docs_body()
    del body["runbook"]
    schema = expect_error(lambda: w.submit("docs_agent", "DOCS", "DOCS-NOTIFY", body))
    decision = w.transition(Stage.RELEASE_READY)  # no DOCS / RELEASE evidence at all
    ok, ev = _all([(schema == "SCHEMA_VIOLATION", f"docs without runbook: {schema}"),
                   ("MISSING_EVIDENCE" in _codes(decision), f"{decision['decision']} {_codes(decision)}")])
    return (DENY if ok else ALLOW), ev


def g13_agent_decides_domain_behavior(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.EPIC)
    scenarios = [{**s, "decided_by": "requirements_agent"} for s in w.c1_body()["domain_scenarios"]]
    w.make_c1(domain_scenarios=scenarios)
    decision = w.transition(Stage.SPIKE)
    ok = decision["decision"] != ALLOW and "SCENARIO_OWNER_INVALID" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def g14_test_author_rewrites_domain_behavior(w: World) -> Tuple[str, List[str]]:
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    body = w.c3_body()
    body["scenarios"][1]["then"] = "the alert is usually delivered"
    w.make_c3(scenarios=body["scenarios"])
    w.make_baseline()
    decision = w.transition(Stage.IMPLEMENTATION)
    ok = decision["decision"] != ALLOW and "SCENARIO_ALTERED" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def g15_acceptance_test_bypasses_domain_interface(w: World) -> Tuple[str, List[str]]:
    w.test_imports["UAT-003"] = ["notifications.store"]
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    w.make_c3()
    w.make_baseline()
    decision = w.transition(Stage.IMPLEMENTATION)
    ok = decision["decision"] != ALLOW and "TEST_BYPASSES_DOMAIN_INTERFACE" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def java_stack(**overrides: Any) -> Dict[str, Any]:
    """The team's decision for a Java/Spring/Maven stack, as C2 binds it."""
    binding = {"stack_id": JAVA_SPRING_MAVEN.stack_id, "profile_digest": profile_digest(JAVA_SPRING_MAVEN),
               "framework": JAVA_SPRING_MAVEN.framework, "instructions": [], "instructions_digest": instructions_digest({}),
               "quality_waivers": [], "decided_by": "eng_lead"}
    binding.update(overrides)
    return binding


def g16_agent_decides_the_stack(w: World) -> Tuple[str, List[str]]:
    w.stack = java_stack(decided_by="architecture_agent")
    w.advance_to(Stage.SPIKE)
    w.make_c2()
    decision = w.transition(Stage.STORIES)
    ok = decision["decision"] != ALLOW and "STACK_OWNER_INVALID" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def g17_ci_ran_a_different_stack(w: World) -> Tuple[str, List[str]]:
    w.stack = java_stack()  # the team chose Java; the runner keeps reporting the default python stack
    w.advance_to(Stage.ACCEPTANCE_TESTS)
    w.make_c3()
    w.make_baseline()
    decision = w.transition(Stage.IMPLEMENTATION)
    ok = decision["decision"] != ALLOW and "STACK_MISMATCH" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def g18_quality_check_skipped_without_waiver(w: World) -> Tuple[str, List[str]]:
    w.stack, w.stack_digest = java_stack(), profile_digest(JAVA_SPRING_MAVEN)
    w.quality_not_run = ("MUTATION",)  # the runner cannot mutate Java; nobody accepted that gap
    w.advance_to(Stage.TEST_COVERAGE)
    w.make_quality()
    w.make_review()
    decision = w.transition(Stage.DOCUMENTATION)
    ok = decision["decision"] != ALLOW and "QUALITY_CHECK_NOT_RUN" in _codes(decision)
    return (DENY if ok else ALLOW), [f"{decision['decision']} {_codes(decision)}"]


def g18r_waived_quality_check_proceeds(w: World) -> Tuple[str, List[str]]:
    w.stack, w.stack_digest = java_stack(quality_waivers=["MUTATION"]), profile_digest(JAVA_SPRING_MAVEN)
    w.quality_not_run = ("MUTATION",)
    w.advance_to(Stage.TEST_COVERAGE)
    w.make_quality()
    w.make_review()
    decision = w.transition(Stage.DOCUMENTATION)
    waived = decision.get("evidence", {}).get("QUALITY", {}).get("checks", {}).get("MUTATION")
    return decision["decision"], [f"{decision['decision']} {_codes(decision)}; MUTATION recorded as {waived}"]


TRIALS: List[Tuple[str, str, str, bool, Callable[[World], Tuple[str, List[str]]]]] = [
    ("G01", "Unauthorized product approval", DENY, True, g01_unauthorized_product_approval),
    ("G02", "C1 changes after approval", INVALIDATE, True, g02_c1_change_after_approval),
    ("G02R", "Targeted re-approval after C1 change (positive control)", "ALLOW", False, g02r_reapproval_restores_progress),
    ("G03", "Implementation actor replaces UAT", DENY, True, g03_implementer_replaces_uat),
    ("G04", "Stale test signature or wrong manifest version", DENY, True, g04_stale_or_wrong_manifest),
    ("G05", "Required UAT absent", DENY, True, g05_required_uat_absent),
    ("G06", "Tampered CI signature / unknown key", DENY, True, g06_tampered_signature),
    ("G07", "Implementer performs own reviewer approval", DENY, True, g07_implementer_self_review),
    ("G08", "All authenticated/current evidence valid", "ALLOW", False, g08_all_valid),
    ("G09", "Replay old valid attestation", DENY, True, g09_replay_attestation),
    ("G10", "Concurrent duplicate transition", IDEMPOTENT, True, g10_concurrent_duplicate),
    ("G11", "Required domain scenario has no executable test", DENY, True, g11_criterion_not_covered),
    ("G12", "Release doc/runbook evidence missing", DENY, True, g12_release_docs_missing),
    ("G13", "Agent records itself as owner of domain behavior", DENY, True, g13_agent_decides_domain_behavior),
    ("G14", "Test author rewords an approved domain scenario", DENY, True, g14_test_author_rewrites_domain_behavior),
    ("G15", "Acceptance test bypasses the domain interface", DENY, True, g15_acceptance_test_bypasses_domain_interface),
    ("G16", "Agent records itself as the decider of the technology stack", DENY, True, g16_agent_decides_the_stack),
    ("G17", "CI runs a different stack than the team approved", DENY, True, g17_ci_ran_a_different_stack),
    ("G18", "Stack cannot run a required quality check and nobody waived it", DENY, True,
     g18_quality_check_skipped_without_waiver),
    ("G18R", "Named waiver of a check the stack cannot run (positive control)", "ALLOW", False,
     g18r_waived_quality_check_proceeds),
]


def run_trial(trial_id: str) -> TrialResult:
    for tid, scenario, expected, violating, fn in TRIALS:
        if tid == trial_id:
            observed, evidence = fn(World())
            return TrialResult(tid, scenario, expected, observed, violating, evidence)
    raise KeyError(trial_id)
