"""Bounded explicit-state model checking of abstract ECLT component models.

Four small models abstract the gate's rules. Breadth-first search visits every reachable state within
the bounds and checks each model's safety properties in every state:

  Approvals (C1 <- C2 <- C3, stage transitions):
    P1 unauthorized transition prevention: a stage is admitted only if each contract it requires has an
       approval by a human holding the required role, who is not its author, given on the contract's
       current version and on the current versions of everything it depends on.
    P2 invalidation after change: no approval the gate still counts was given before a later change to
       an artifact it depends on.
    P3 separation of duties: no approval the gate counts comes from the author or from an agent.
  Attestations:
    P4 replay prevention: an attestation identifier is accepted at most once, and only for a build bound
       to the current acceptance manifest.
  Race:
    P5 concurrent transition safety: two racing requests for the same transition advance the stage once.
  Rollback:
    P6 human-only rollback: a rollback happens only after a human on-call decision.

Properties are judged from ground truth (version snapshots, identities, counts), independently of the
mechanism that enforces them (validity flags, used-id sets, compare-and-set, the broker's tool list).
Each mutant removes one mechanism, and the checker must find a counterexample for it, which shows the
properties are not satisfied vacuously. The properties concern disjoint parts of the state, and the
gate serializes all commands in one transaction each, so checking the components separately loses no
interleaving that matters to them. This establishes the properties for the abstract models within the
bounds; it is not a proof about the implementation, whose correspondence to the models rests on the
design and on the test suite.
"""

from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Dict, FrozenSet, Hashable, List, Optional, Tuple

# ------------------------------------------------------------------ search


def bfs(start: Hashable, successors: Callable[[Hashable], List[Tuple[str, Hashable]]],
        violations: Callable[[Hashable, Hashable], List[str]]) -> Dict[str, object]:
    seen = {start}
    queue = deque([(start, ())])
    transitions = 0
    while queue:
        s, path = queue.popleft()
        for action, nxt in successors(s):
            transitions += 1
            bad = violations(s, nxt)
            if bad:
                return {"holds": False, "violation": bad[0], "trace": list(path) + [action],
                        "states": len(seen), "transitions": transitions}
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + (action,)))
    return {"holds": True, "states": len(seen), "transitions": transitions}


# --------------------------------------------------------------- approvals

ARTS = ("C1", "C2", "C3")
UPSTREAM = {"C1": (), "C2": ("C1",), "C3": ("C1", "C2")}
REQUIRED = {"C1": "product_owner", "C2": "engineering_lead", "C3": "qa"}
PEOPLE = {"po": ("human", "product_owner"), "eng": ("human", "engineering_lead"), "qa": ("human", "qa"),
          "bot": ("agent", "product_owner")}  # an agent that was given a human role
AUTHOR = "author"  # the agent that drafts every contract
PEOPLE[AUTHOR] = ("human", "qa")  # worst case: the author also holds a role that could approve C3
STAGE_NEEDS = {1: ("C1",), 2: ("C1", "C2"), 3: ("C1", "C2", "C3")}
Approval = Tuple[str, str, Tuple[int, int, int], bool]  # artifact, person, version snapshot, still valid


@dataclass(frozen=True)
class ApprovalState:
    version: Tuple[int, int, int] = (1, 1, 1)
    approvals: FrozenSet[Approval] = frozenset()
    stage: int = 0


def _i(a: str) -> int:
    return ARTS.index(a)


def approval_model(invalidation: bool = True, role_and_kind_check: bool = True, author_check: bool = True,
                   max_version: int = 2) -> Dict[str, object]:
    def counted(s: ApprovalState, a: str) -> List[Approval]:
        return [x for x in s.approvals if x[0] == a and x[3] and x[2][_i(a)] == s.version[_i(a)]]

    def met(s: ApprovalState, stage: int) -> bool:
        return all(any(PEOPLE[x[1]][1] == REQUIRED[a] for x in counted(s, a)) for a in STAGE_NEEDS[stage])

    def successors(s: ApprovalState) -> List[Tuple[str, ApprovalState]]:
        out = []
        for a in ARTS:
            if s.version[_i(a)] < max_version:
                version = tuple(v + 1 if j == _i(a) else v for j, v in enumerate(s.version))
                affected = {a} | {d for d in ARTS if a in UPSTREAM[d]}
                approvals = s.approvals
                if invalidation:
                    approvals = frozenset((x[0], x[1], x[2], x[3] and x[0] not in affected) for x in approvals)
                out.append((f"revise {a}", replace(s, version=version, approvals=approvals)))
            for p, (kind, role) in PEOPLE.items():
                if role_and_kind_check and (kind != "human" or role != REQUIRED[a]):
                    continue
                if author_check and p == AUTHOR:
                    continue
                if any(y[0] == a and y[1] == p and y[2] == s.version for y in s.approvals):
                    continue
                out.append((f"approve {a} by {p}", replace(s, approvals=s.approvals | {(a, p, s.version, True)})))
        if s.stage < 3 and met(s, s.stage + 1):
            out.append((f"transition to stage {s.stage + 1}", replace(s, stage=s.stage + 1)))
        return out

    def violations(prev: ApprovalState, s: ApprovalState) -> List[str]:
        found = []
        if s.stage > prev.stage:
            for a in STAGE_NEEDS[s.stage]:
                deps = (a,) + UPSTREAM[a]
                if not any(x[0] == a and all(x[2][_i(d)] == s.version[_i(d)] for d in deps)
                           and PEOPLE[x[1]][0] == "human" and PEOPLE[x[1]][1] == REQUIRED[a] and x[1] != AUTHOR
                           for x in s.approvals):
                    found.append(f"P1 stage {s.stage} admitted without a valid approval of the current {a}")
        for a in ARTS:
            for x in counted(s, a):
                if any(x[2][_i(u)] != s.version[_i(u)] for u in UPSTREAM[a]):
                    found.append(f"P2 approval of {a} by {x[1]} still counts after an upstream change")
                if x[1] == AUTHOR or PEOPLE[x[1]][0] != "human":
                    found.append(f"P3 approval of {a} by {x[1]} (author or agent) counts")
        return found

    return bfs(ApprovalState(), successors, violations)


# ------------------------------------------------------------ attestations


@dataclass(frozen=True)
class AttestState:
    manifest: int = 1  # current locked acceptance-manifest version
    build: Optional[int] = None  # manifest version the build is bound to
    accepted: Tuple[Tuple[int, int], ...] = ()  # (attestation id, manifest version of the build) as accepted


def attestation_model(replay_check: bool = True, binding_check: bool = True, max_manifest: int = 2,
                      ids: int = 2) -> Dict[str, object]:
    def successors(s: AttestState) -> List[Tuple[str, AttestState]]:
        out = []
        if s.manifest < max_manifest:
            out.append(("new manifest version (invalidates the build)", replace(s, manifest=s.manifest + 1, build=None)))
        if s.build is None:
            out.append(("register build", replace(s, build=s.manifest)))
        if s.build is not None:
            used = {i for i, _ in s.accepted}
            for i in range(ids):
                if replay_check and i in used:
                    continue
                if binding_check and s.build != s.manifest:
                    continue
                out.append((f"accept attestation {i}", replace(s, accepted=s.accepted + ((i, s.build),))))
        if not binding_check and s.build is not None and s.manifest < max_manifest:
            out.append(("manifest changes, build kept", replace(s, manifest=s.manifest + 1)))
        return out

    def violations(prev: AttestState, s: AttestState) -> List[str]:
        found = []
        ids_seen = [i for i, _ in s.accepted]
        if len(ids_seen) != len(set(ids_seen)):
            found.append("P4 an attestation id was accepted twice")
        if len(s.accepted) > len(prev.accepted) and s.accepted[-1][1] != s.manifest:
            found.append("P4 an attestation was accepted for a build bound to an old manifest")
        return found

    return bfs(AttestState(), successors, violations)


# -------------------------------------------------------------------- race


def race_model(compare_and_set: bool = True, requests: int = 2) -> Dict[str, object]:
    """State: (stage, phases, admits). Each request evaluates (reads stage 0) and commits."""
    start = (0, tuple("idle" for _ in range(requests)), 0)

    def successors(s: Tuple[int, Tuple[str, ...], int]) -> List[Tuple[str, Tuple[int, Tuple[str, ...], int]]]:
        stage, phases, admits = s
        out = []
        for k, phase in enumerate(phases):
            nxt = list(phases)
            if compare_and_set and phase == "idle":  # one transaction: evaluate, check stage, commit
                nxt[k] = "done"
                ok = stage == 0
                out.append((f"request {k} {'admitted' if ok else 'refused'}",
                            (1 if ok else stage, tuple(nxt), admits + ok)))
            elif not compare_and_set and phase == "idle" and stage == 0:
                nxt[k] = "evaluated"
                out.append((f"request {k} evaluated", (stage, tuple(nxt), admits)))
            elif not compare_and_set and phase == "evaluated":
                nxt[k] = "done"
                out.append((f"request {k} committed without re-check", (stage + 1, tuple(nxt), admits + 1)))
        return out

    def violations(prev: Tuple, s: Tuple) -> List[str]:
        return ["P5 the same transition was admitted twice"] if s[2] > 1 else []

    return bfs(start, successors, violations)


# ---------------------------------------------------------------- rollback


def rollback_model(human_only: bool = True) -> Dict[str, object]:
    """State: (human decided, rolled back, rollback preceded by a decision)."""
    def successors(s: Tuple[bool, bool, bool]) -> List[Tuple[str, Tuple[bool, bool, bool]]]:
        decided, rolled, ok = s
        out = []
        if not decided:
            out.append(("human on-call decides", (True, rolled, ok)))
        if not rolled and decided:
            out.append(("gate authorizes rollback for the decision", (decided, True, True)))
        if not rolled and not human_only:
            out.append(("operations agent requests rollback", (decided, True, decided)))
        return out

    def violations(prev: Tuple, s: Tuple) -> List[str]:
        return ["P6 rollback without a human decision"] if s[1] and not s[2] else []

    return bfs((False, False, True), successors, violations)


# ------------------------------------------------------------------ driver

MODELS: Dict[str, Callable[..., Dict[str, object]]] = {
    "approvals (P1-P3)": approval_model, "attestations (P4)": attestation_model,
    "race (P5)": race_model, "rollback (P6)": rollback_model,
}

MUTANTS: Dict[str, Tuple[str, Dict[str, bool]]] = {
    "no invalidation on change": ("approvals (P1-P3)", {"invalidation": False}),
    "no role or human check on approval": ("approvals (P1-P3)", {"role_and_kind_check": False}),
    "author may approve own artifact": ("approvals (P1-P3)", {"author_check": False}),
    "replayed attestation accepted": ("attestations (P4)", {"replay_check": False}),
    "attestation not bound to current manifest": ("attestations (P4)", {"binding_check": False}),
    "transition commits without compare-and-set": ("race (P5)", {"compare_and_set": False}),
    "agent may request rollback": ("rollback (P6)", {"human_only": False}),
}


def run() -> Dict[str, object]:
    models = {name: fn() for name, fn in MODELS.items()}
    mutants = {}
    for name, (model, flags) in MUTANTS.items():
        r = MODELS[model](**flags)
        mutants[name] = {"model": model, "caught": not r["holds"], "violation": r.get("violation"),
                         "trace": r.get("trace", []), "states": r["states"]}
    return {"models": models, "mutants": mutants,
            "bounds": {"versions_per_artifact": 2, "attestation_ids": 2, "racing_requests": 2}}
