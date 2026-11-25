"""Governed repository: content-addressed commits, branches and per-role path permissions.

Identity comes from the gate (``whoami``), so the workspace never trusts a
caller-supplied role.

* Requirements discovery is read-only and every read returns a citation
  (uri, revision, digest) - section 5.5.
* The acceptance-test author cannot read ``src/`` - it writes tests from the
  approved contracts, not from the implementation (section 7.1).
* The implementer can read ``uat/`` but never write it.
* Protected branches (``main``) accept no direct commits. Only the merge
  service may fast-forward them, and only with a gate ALLOW merge decision
  for exactly that head, used once (M3).
"""

import hashlib
import posixpath
import threading
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from .canonical import digest, file_digest
from .errors import AuthorizationError, Conflict, NotFound, ValidationFailure
from .service import ALLOW, GovernanceService


@dataclass(frozen=True)
class PathRule:
    read: Tuple[str, ...] = ()
    write: Tuple[str, ...] = ()


ALL = ("",)

DEFAULT_ACL: Mapping[str, PathRule] = {
    "requirements_agent": PathRule(read=("docs/", "src/")),
    "architecture_agent": PathRule(read=("docs/", "src/"), write=("src/", "docs/adr/")),
    "story_planning_agent": PathRule(read=("docs/",)),
    "acceptance_test_agent": PathRule(read=("docs/", "uat/"), write=("uat/",)),
    "implementation_agent": PathRule(read=("docs/", "src/", "uat/"), write=("src/",)),
    "test_coverage_agent": PathRule(read=("docs/", "src/", "uat/", "tests/"), write=("tests/coverage/",)),
    "code_review_agent": PathRule(read=ALL),
    "documentation_agent": PathRule(read=("docs/", "src/"), write=("docs/",)),
    "release_evidence_agent": PathRule(read=("docs/",)),
    "operational_support_agent": PathRule(read=("docs/",)),
    "qa": PathRule(read=ALL, write=("uat/",)),
    "engineer": PathRule(read=ALL, write=("src/",)),
    "ci_runner": PathRule(read=ALL),
    "merge_service": PathRule(read=ALL),
}

BUILD_PREFIX = "src/"
MAIN = "main"


@dataclass(frozen=True)
class SourceExcerpt:
    """A cited, untrusted piece of repository content."""

    uri: str
    revision: str
    digest: str
    content: str
    trust: str = "untrusted"


def normalize_path(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValidationFailure("INVALID_PATH", f"{path!r} is not a relative repository path")
    if posixpath.normpath(path) != path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ValidationFailure("INVALID_PATH", f"{path!r} is not a normalized repository path")
    return path


def build_digest_of(tree: Mapping[str, bytes]) -> str:
    """Digest of the build inputs (everything under src/), independent of tests and docs."""
    return digest({p: file_digest(c) for p, c in sorted(tree.items()) if p.startswith(BUILD_PREFIX)})


def diff_paths(a: Mapping[str, bytes], b: Mapping[str, bytes]) -> List[str]:
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


class Workspace:
    def __init__(
        self,
        gate: GovernanceService,
        seed: Optional[Dict[str, bytes]] = None,
        acl: Mapping[str, PathRule] = DEFAULT_ACL,
        protected: Iterable[str] = (),
    ) -> None:
        self._gate = gate
        self._acl = acl
        self._protected: FrozenSet[str] = frozenset(protected)
        self._lock = threading.Lock()
        self._commits: Dict[str, Dict[str, object]] = {}
        tree = {normalize_path(p): bytes(c) for p, c in (seed or {}).items()}
        self._refs: Dict[str, str] = {MAIN: self._store(None, tree, "seed repository", "admin")}
        self._bases: Dict[str, str] = {}
        self._used_decisions: Set[str] = set()

    # ----------------------------------------------------------------- internals

    def _store(self, parent: Optional[str], tree: Dict[str, bytes], message: str, author: str) -> str:
        header = {"parent": parent, "author": author, "message": message,
                  "tree": {p: file_digest(c) for p, c in sorted(tree.items())}}
        revision = hashlib.sha256(digest(header).encode()).hexdigest()[:40]
        self._commits[revision] = {"parent": parent, "tree": dict(tree), "message": message, "author": author}
        return revision

    def _rules(self, token: str) -> Tuple[str, FrozenSet[str], PathRule]:
        who = self._gate.whoami(token)
        rules = [self._acl[r] for r in who["roles"] if r in self._acl]
        merged = PathRule(read=tuple(p for r in rules for p in r.read), write=tuple(p for r in rules for p in r.write))
        return who["principal_id"], frozenset(who["roles"]), merged

    @staticmethod
    def _allowed(path: str, prefixes: Tuple[str, ...]) -> bool:
        return any(path.startswith(prefix) for prefix in prefixes)

    def _resolve(self, revision: Optional[str], branch: Optional[str]) -> str:
        if revision is not None:
            return revision
        # A branch nobody has committed to yet is, by definition, still main.
        return self._refs.get(branch or MAIN, self._refs[MAIN])

    def _tree(self, revision: Optional[str], branch: Optional[str] = None) -> Tuple[str, Dict[str, bytes]]:
        rev = self._resolve(revision, branch)
        commit = self._commits.get(rev)
        if commit is None:
            raise NotFound("UNKNOWN_REVISION", rev)
        return rev, commit["tree"]  # type: ignore[return-value]

    def _require_role(self, token: str, roles: FrozenSet[str], action: str) -> str:
        principal, held, _ = self._rules(token)
        if not held & roles:
            raise AuthorizationError("ROLE_NOT_PERMITTED", f"{principal} may not {action}")
        return principal

    # -------------------------------------------------------------------- public

    @property
    def head(self) -> str:
        return self._refs[MAIN]

    def branch_head(self, branch: str) -> str:
        return self._resolve(None, branch)

    def branch_base(self, branch: str) -> str:
        return self._bases[branch]

    def list(self, token: str, prefix: str = "", revision: Optional[str] = None, branch: Optional[str] = None) -> List[str]:
        _, _, rule = self._rules(token)
        _, tree = self._tree(revision, branch)
        return sorted(p for p in tree if p.startswith(prefix) and self._allowed(p, rule.read))

    def read(self, token: str, path: str, revision: Optional[str] = None, branch: Optional[str] = None) -> SourceExcerpt:
        principal, _, rule = self._rules(token)
        path = normalize_path(path)
        if not self._allowed(path, rule.read):
            raise AuthorizationError("PATH_NOT_PERMITTED", f"{principal} may not read {path}", path=path)
        rev, tree = self._tree(revision, branch)
        if path not in tree:
            raise NotFound("UNKNOWN_PATH", path)
        content = tree[path]
        return SourceExcerpt(f"repo://{path}", rev, file_digest(content), content.decode("utf-8", "replace"))

    def commit(self, token: str, changes: Dict[str, Optional[bytes]], message: str, branch: str = MAIN) -> str:
        """Atomically apply changes (None deletes) to a branch, creating it from main if new."""
        principal, _, rule = self._rules(token)
        normalized = {normalize_path(p): c for p, c in changes.items()}
        denied = sorted(p for p in normalized if not self._allowed(p, rule.write))
        if denied:
            raise AuthorizationError("PATH_NOT_PERMITTED", f"{principal} may not write {denied}", paths=denied)
        if branch in self._protected:
            raise AuthorizationError("PROTECTED_BRANCH", f"{branch} only changes through a gated merge")
        with self._lock:
            if branch not in self._refs:
                self._refs[branch] = self._bases[branch] = self._refs[MAIN]
            _, tree = self._tree(None, branch)
            new_tree = dict(tree)
            for path, content in normalized.items():
                if content is None:
                    new_tree.pop(path, None)
                else:
                    new_tree[path] = bytes(content)
            self._refs[branch] = self._store(self._refs[branch], new_tree, message, principal)
            return self._refs[branch]

    def snapshot(self, token: str, revision: str) -> Dict[str, bytes]:
        """Full checkout for the trusted CI runner."""
        self._require_role(token, frozenset({"ci_runner"}), "check out full trees")
        _, tree = self._tree(revision)
        return dict(tree)

    def changed_paths(self, token: str, base: str, revision: str) -> List[str]:
        self._require_role(token, frozenset({"ci_runner", "merge_service"}), "diff revisions")
        return diff_paths(self._tree(base)[1], self._tree(revision)[1])

    def merge(self, token: str, branch: str, decision_id: str) -> str:
        """Fast-forward main to the branch head, if and only if the gate allowed exactly that head."""
        self._require_role(token, frozenset({"merge_service"}), "merge into protected branches")
        decision = self._gate.get_transition(token, decision_id)
        with self._lock:
            head = self._resolve(None, branch)
            if decision.get("to_stage") != "MERGE" or decision.get("decision") != ALLOW:
                raise AuthorizationError("MERGE_NOT_ALLOWED", f"gate decision {decision_id} is {decision.get('decision')}")
            if decision.get("branch") != branch or decision.get("head_revision") != head:
                raise Conflict("STALE_HEAD", "branch head moved after the gate decision")
            if decision_id in self._used_decisions:
                raise Conflict("REPLAY_DETECTED", f"merge decision {decision_id} was already used")
            if self._bases.get(branch) != self._refs[MAIN]:
                raise Conflict("NOT_FAST_FORWARD", "main moved since the branch was created; rebase and re-test")
            self._used_decisions.add(decision_id)
            self._refs[MAIN] = head
            return head

    def build_digest(self, revision: Optional[str] = None, branch: Optional[str] = None) -> str:
        _, tree = self._tree(revision, branch)
        return build_digest_of(tree)

    def author_of(self, revision: str) -> str:
        return self._commits[revision]["author"]  # type: ignore[return-value]

    def _force_commit_for_test(self, changes: Dict[str, bytes], author: str, branch: str = MAIN) -> str:
        """Simulates a compromised repository host writing around the ACLs (tests only)."""
        with self._lock:
            _, tree = self._tree(None, branch)
            self._refs[branch] = self._store(self._refs[branch], {**tree, **changes}, "out-of-band write", author)
            return self._refs[branch]
