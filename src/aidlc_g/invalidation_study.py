"""Dependency-aware invalidation study (RQ4): does a change invalidate exactly its dependents?

Part 1, targeted invalidation. From a work item at RELEASE_READY (every artifact, build and piece of
evidence present and approved), each case submits a new version of one artifact. The set the gate
invalidates is compared with the set an independent dependency table predicts. The study also counts
approvals that would still count afterwards (stale approvals) and the human re-approvals the change
implies, against two reference policies: no invalidation (a conventional repository, where approvals of
dependent artifacts stay valid) and restart-all (every approval is redone after any change).

Part 2, concurrency. A change to the design (C2) races a transition request on a fresh work item, many
times. An admitted transition must have been decided before the change committed, and afterwards the
gate must never re-admit the stage on the invalidated evidence.
"""

import threading
from typing import Any, Callable, Dict, List, Set

from .contracts import ContractType
from .lifecycle import Stage
from .policy import DEFAULT_POLICY
from .testkit import World

# Independent of the gate's edge table: what each artifact type depends on (Fig. dependency graph).
DEPENDS_ON: Dict[str, List[str]] = {
    "C0": [], "C1": ["C0"], "C2": ["C1"], "STORIES": ["C1", "C2"], "C3": ["C1", "C2", "STORIES"],
    "BUILD": ["C3"], "DOCS": ["BUILD"], "RELEASE": ["DOCS", "BUILD"],
}
ARTIFACT_IDS = {"C0": "INIT-NOTIFY", "C1": "BR-NOTIFY", "C2": "TR-NOTIFY", "STORIES": "STORIES-NOTIFY",
                "C3": "AC-NOTIFY", "DOCS": "DOCS-NOTIFY", "RELEASE": "REL-NOTIFY"}
BUILDS = ("BLD-STUB-1", "BLD-1")  # both bound to the locked C3


def expected_dependents(slot: str) -> Set[str]:
    out: Set[str] = set()
    frontier = [slot]
    while frontier:
        node = frontier.pop()
        for child, parents in DEPENDS_ON.items():
            if node in parents and child not in out:
                out.add(child)
                frontier.append(child)
    return out


def _nodes(slots: Set[str]) -> Set[str]:
    named = {ARTIFACT_IDS[s] for s in slots if s in ARTIFACT_IDS}
    if "BUILD" in slots:
        named |= {f"build:{b}" for b in BUILDS}
    return named


def _changes(w: World) -> Dict[str, Callable[[], Dict[str, Any]]]:
    stories = w.stories_body()
    stories["stories"][0]["title"] = "Silence optional notifications (revised)"
    return {
        "C0": lambda: w.submit("requirements_agent", "C0", "INIT-NOTIFY", w.c0_body(objective="Give users control over every optional notification channel")),
        "C1": lambda: w.submit("requirements_agent", "C1", "BR-NOTIFY", w.c1_body(requested_behavior="A signed-in user can disable optional notifications per channel")),
        "C2": lambda: w.submit("architecture_agent", "C2", "TR-NOTIFY", w.c2_body(operability="Feature flag with per-tenant rollout")),
        "STORIES": lambda: w.submit("story_agent", "STORIES", "STORIES-NOTIFY", stories),
        "C3": lambda: w.submit("uat_agent", "C3", "AC-NOTIFY", w.c3_body(proposed_scenarios=[{
            "proposal_id": "P-1", "given": "a user in quiet hours", "when": "an optional event occurs",
            "then": "it is held until morning", "rationale": "requested by support"}])),
        "DOCS": lambda: w.submit("docs_agent", "DOCS", "DOCS-NOTIFY", w.docs_body(monitoring_baseline="security alerts 100%; optional delivery tracks preference per channel")),
    }


def _invalid_nodes(w: World) -> Set[str]:
    db = w.svc._db
    arts = {r["artifact_id"] for r in db.execute("SELECT artifact_id FROM artifacts WHERE status = 'INVALIDATED'")}
    builds = {f"build:{r['build_id']}" for r in db.execute("SELECT build_id FROM builds WHERE valid = 0")}
    return arts | builds


def _stale_approvals(w: World, nodes: Set[str]) -> int:
    """Approvals of the dependents' (now outdated) versions that would still count if nothing were invalidated."""
    ids = [n for n in nodes if not n.startswith("build:")]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    return w.svc._db.execute(f"SELECT COUNT(*) AS n FROM approvals WHERE valid = 1 AND artifact_id IN ({marks})",
                             tuple(ids)).fetchone()["n"]


def _approvals_for(slots: Set[str]) -> int:
    types = {"C0": ContractType.C0, "C1": ContractType.C1, "C2": ContractType.C2, "STORIES": ContractType.STORIES,
             "C3": ContractType.C3, "DOCS": ContractType.DOCS, "RELEASE": ContractType.RELEASE}
    return sum(len(DEFAULT_POLICY.approvers[types[s]]) for s in slots if s in types)


def targeted_cases() -> List[Dict[str, Any]]:
    from .baseline_study import configured
    rows = []
    all_slots = {"C0", "C1", "C2", "STORIES", "C3", "DOCS", "RELEASE"}
    for slot in ("C0", "C1", "C2", "STORIES", "C3", "DOCS"):
        expected = _nodes(expected_dependents(slot))
        w = World()
        w.advance_to(Stage.RELEASE_READY)
        response = _changes(w)[slot]()
        observed = _invalid_nodes(w)
        stage_after = w.svc.reverify(w.admin, w.work_item)["decision"]
        # Reference policy: no invalidation (dependents keep their approvals).
        with configured(("dependency_invalidation",)):
            w2 = World()
            w2.advance_to(Stage.RELEASE_READY)
            _changes(w2)[slot]()
            stale_without = _stale_approvals(w2, expected)
        tp = len(observed & expected)
        rows.append({
            "changed": slot, "new_version": response["version"],
            "expected": sorted(expected), "invalidated": sorted(observed),
            "missed": sorted(expected - observed), "extra": sorted(observed - expected),
            "precision": tp / len(observed) if observed else 1.0, "recall": tp / len(expected) if expected else 1.0,
            "stale_approvals_aidlcg": _stale_approvals(w, expected),
            "stale_approvals_no_invalidation": stale_without,
            "reapprovals_targeted": _approvals_for({slot} | expected_dependents(slot)),
            "reapprovals_restart_all": _approvals_for(all_slots),
            "rebuild_needed": "BUILD" in expected_dependents(slot),
            "stage_reverify": stage_after,
        })
    return rows


def concurrency_cases(runs: int = 40, seed: int = 2026) -> Dict[str, Any]:
    import random
    import time
    rng = random.Random(seed)
    outcomes = {"transition_first": 0, "change_first": 0, "violations": 0, "readmitted": 0}
    for _ in range(runs):
        w = World()
        w.advance_to(Stage.DOCUMENTATION)
        w.prepare_for(Stage.RELEASE_READY) if hasattr(w, "prepare_for") else [getattr(w, m)() for m in
                                                                               dict(World.STEPS)[Stage.RELEASE_READY]]
        results: Dict[str, Any] = {}
        barrier = threading.Barrier(2)

        delays = (rng.uniform(0, 0.004), rng.uniform(0, 0.004))  # vary which request reaches the gate first

        def change() -> None:
            barrier.wait()
            time.sleep(delays[0])
            results["change"] = w.submit("architecture_agent", "C2", "TR-NOTIFY", w.c2_body(operability="Changed during review"))

        def transition() -> None:
            barrier.wait()
            time.sleep(delays[1])
            results["decision"] = w.transition(Stage.RELEASE_READY)
        threads = [threading.Thread(target=change), threading.Thread(target=transition)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seq = {r["event_type"]: r["seq"] for r in w.svc._db.execute(
            "SELECT seq, event_type FROM audit WHERE event_type IN ('CONTRACT_SUBMITTED', 'TRANSITION_EVALUATED') "
            "ORDER BY seq")}
        submitted = max(r["seq"] for r in w.svc._db.execute(
            "SELECT seq FROM audit WHERE event_type = 'CONTRACT_SUBMITTED'"))
        evaluated = max(r["seq"] for r in w.svc._db.execute(
            "SELECT seq FROM audit WHERE event_type = 'TRANSITION_EVALUATED'"))
        allowed = results["decision"]["decision"] == "ALLOW"
        if evaluated < submitted:
            outcomes["transition_first"] += 1
        else:
            outcomes["change_first"] += 1
        if allowed and evaluated > submitted:
            outcomes["violations"] += 1  # admitted on evidence already invalidated
        if w.svc.reverify(w.admin, w.work_item)["decision"] == "ALLOW":
            outcomes["readmitted"] += 1  # the stage still looks valid after the change: must never happen
        del seq
    outcomes["runs"] = runs
    return outcomes


def run(concurrency_runs: int = 40) -> Dict[str, Any]:
    cases = targeted_cases()
    return {
        "targeted": cases,
        "concurrency": concurrency_cases(concurrency_runs),
        "summary": {
            "cases": len(cases),
            "exact": sum(1 for c in cases if not c["missed"] and not c["extra"]),
            "stale_aidlcg": sum(c["stale_approvals_aidlcg"] for c in cases),
            "stale_no_invalidation": sum(c["stale_approvals_no_invalidation"] for c in cases),
            "reapprovals_targeted": sum(c["reapprovals_targeted"] for c in cases),
            "reapprovals_restart_all": sum(c["reapprovals_restart_all"] for c in cases),
        },
    }
