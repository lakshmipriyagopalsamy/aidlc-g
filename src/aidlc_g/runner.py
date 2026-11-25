"""Trusted CI runner: executes the locked acceptance tests and signs what it saw.

The runner trusts nobody's description of the build. It checks out the exact
revision, recomputes the build digest and every test-file digest itself,
executes the tests, and signs those observations. A lying implementer or an
out-of-band test edit therefore shows up as a binding mismatch or a
TEST_INTEGRITY_VIOLATION at the gate.

M3 adds a quality run on the final branch revision: the coverage agent's
tests, mutation testing of the changed source files against the full suite,
a secret scan of changed files and a dependency scan against an advisory feed.
The runner measures; the gate's policy judges.

The stack is the team's decision (see stacks.py). Python runs pytest in-process
tooling; JVM and .NET stacks run the profile's build command and read its JUnit
XML. Every attestation carries the digest of the profile it executed.

M4 adds packaging: the merged revision's src/ tree becomes a deterministic
artifact in the content-addressed registry, described by signed provenance.

Isolation is a temporary directory plus a subprocess with a minimal
environment and a timeout. That is adequate for the scripted fixtures; it is
NOT a sandbox for untrusted model-generated code (see README limitations).
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .canonical import file_digest
from .crypto import PROVENANCE_DOMAIN, QUALITY_DOMAIN, sign_payload
from .mutation import generate_mutants
from .registry import ArtifactRegistry, deterministic_tar
from .contracts import StackProfile
from .service import GovernanceService
from .stacks import (DEFAULT_STACK, csharp_imports, expand_command, is_test_file, jvm_imports, profile_digest,
                     scan_jvm_or_nuget, test_name)
from .workspace import BUILD_PREFIX, Workspace, build_digest_of


SECRET_RULES = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("assigned-credential", re.compile(
        r"(?i)(?:api[_-]?key|secret|passw(?:or)?d|token)\w*\s*[:=]\s*['\"][^'\"\s]{12,}['\"]")),
]
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)$")


def scan_secrets(tree: Mapping[str, bytes], paths: List[str]) -> List[str]:
    """Findings name the file, line and rule - never the matched value."""
    findings = []
    for path in paths:
        if path not in tree:
            continue
        for lineno, line in enumerate(tree[path].decode("utf-8", "replace").splitlines(), 1):
            findings.extend(f"{path}:{lineno} {name}" for name, rule in SECRET_RULES if rule.search(line))
    return findings


DYNAMIC_IMPORT = "<dynamic>"  # same marker as stacks.DYNAMIC_IMPORT


def stack_imports(stack: StackProfile, tree: Mapping[str, bytes], path: str) -> List[str]:
    if stack.language == "python":
        return project_imports(tree, path)
    if stack.language in ("java", "kotlin"):
        return jvm_imports(tree, path)
    return csharp_imports(tree, path)


def project_imports(tree: Mapping[str, bytes], path: str) -> List[str]:
    """Project modules a test file imports, parsed statically.

    Project modules are the top-level packages and modules under src/. Imports of
    anything else (pytest, the standard library) are not the gate's concern. An
    import the parser cannot resolve - importlib, __import__, a relative import or
    unparsable source - is reported as DYNAMIC_IMPORT, which no interface allows.
    """
    roots = {p.split("/")[1].removesuffix(".py") for p in tree if p.startswith("src/") and p.count("/") >= 1}
    try:
        module = ast.parse(tree[path].decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return [DYNAMIC_IMPORT]
    found = set()
    for node in ast.walk(module):
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.add(DYNAMIC_IMPORT)
            elif node.module:
                names = [node.module]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            found.add(DYNAMIC_IMPORT)
        found.update(n for n in names if n.split(".")[0] in roots)
        if "importlib" in (n.split(".")[0] for n in names):
            found.add(DYNAMIC_IMPORT)
    return sorted(found)


def scan_dependencies(tree: Mapping[str, bytes], advisories: Mapping[Tuple[str, str], str]) -> Tuple[int, List[str]]:
    count, findings = 0, []
    for path in sorted(p for p in tree if p.startswith("src/") and p.rsplit("/", 1)[-1].startswith("requirements")
                       and p.endswith(".txt")):
        for lineno, raw in enumerate(tree[path].decode("utf-8", "replace").splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            count += 1
            m = _REQUIREMENT.match(line)
            if m is None:
                findings.append(f"{path}:{lineno} unpinned requirement {line!r}")
            elif (m.group(1).lower(), m.group(2)) in advisories:
                findings.append(f"{path}:{lineno} {m.group(1)}=={m.group(2)} {advisories[(m.group(1).lower(), m.group(2))]}")
    return count, findings


@dataclass
class RunRecord:
    build_id: str
    kind: str
    revision: str
    payload: Dict[str, Any]
    ingest: Dict[str, Any]
    stdout: str = ""
    errors: List[str] = field(default_factory=list)


class TrustedRunner:
    def __init__(self, gate: GovernanceService, workspace: Workspace, runner_id: str,
                 private_key: Any, token: str, timeout: float = 120.0,
                 advisories: Optional[Mapping[Tuple[str, str], str]] = None, max_mutants: int = 40,
                 registry: Optional[ArtifactRegistry] = None, stack: StackProfile = DEFAULT_STACK) -> None:
        self._gate = gate
        self.stack = stack
        self.stack_digest = profile_digest(stack)
        self._registry = registry
        self._advisories = dict(advisories or {})
        self._max_mutants = max_mutants
        self._workspace = workspace
        self.runner_id = runner_id
        self._key = private_key
        self._token = token
        self._timeout = timeout
        self._seq = 0
        self.history: List[RunRecord] = []

    def use_stack(self, stack: StackProfile) -> None:
        """Configure which stack this runner executes. Attestations carry its digest, so the gate sees any switch."""
        self.stack = stack
        self.stack_digest = profile_digest(stack)

    def execute(self, build_id: str) -> RunRecord:
        build = self._gate.get_build(self._token, build_id)
        manifest = self._gate.get_artifact(self._token, build["manifest_id"], build["manifest_version"])["body"]
        tree = self._workspace.snapshot(self._token, build["source_revision"])
        outcomes, stdout = self._run_tests(tree, [t["path"] for t in manifest["tests"]])
        self._seq += 1
        payload = {
            "attestation_id": f"ATT-{self.runner_id}-{build_id}-{self._seq}",
            "runner_id": self.runner_id,
            "build_id": build_id,
            "source_revision": build["source_revision"],
            "build_digest": build_digest_of(tree),  # recomputed, not copied from the registration
            "manifest_id": build["manifest_id"],
            "manifest_version": build["manifest_version"],
            "manifest_digest": build["manifest_digest"],
            "policy_version": self._gate.policy_version,
            "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "stack_digest": self.stack_digest,
            "results": [
                {"test_id": t["test_id"], "test_digest": file_digest(tree[t["path"]]), "outcome": outcomes[t["path"]],
                 "project_imports": stack_imports(self.stack, tree, t["path"])}
                for t in manifest["tests"] if t["path"] in tree
            ],
        }
        ingest = self._gate.ingest_attestation(payload, sign_payload(self._key, payload))
        record = RunRecord(build_id, build["kind"], build["source_revision"], payload, ingest, stdout)
        self.history.append(record)
        return record

    def quality(self, build_id: str, revision: str, base_revision: str) -> RunRecord:
        """Stage 7 run on ``revision`` (the branch head), diffed against ``base_revision`` (where the branch left main)."""
        build = self._gate.get_build(self._token, build_id)
        manifest = self._gate.get_artifact(self._token, build["manifest_id"], build["manifest_version"])["body"]
        tree = self._workspace.snapshot(self._token, revision)
        changed = self._workspace.changed_paths(self._token, base_revision, revision)
        uat = [t["path"] for t in manifest["tests"] if t["path"] in tree]
        coverage = sorted(p for p in tree if p.startswith("tests/coverage/") and is_test_file(self.stack, p))
        checks: List[Dict[str, Any]] = []

        outcomes, stdout = self._run_tests(tree, coverage)
        not_passed = [f"{p}: {o}" for p, o in sorted(outcomes.items()) if o != "PASSED"]
        checks.append({"check_id": "coverage-tests", "kind": "COVERAGE_TESTS",
                       "status": "PASS" if coverage and not not_passed else "FAIL",
                       "summary": f"{len(coverage) - len(not_passed)}/{len(coverage)} coverage test files passed"
                       if coverage else "no coverage tests were provided",
                       "metrics": {"files": len(coverage), "passed": len(coverage) - len(not_passed)},
                       "findings": not_passed})

        if self.stack.language == "python":
            checks.append(self._mutation(tree, changed, uat + coverage))
        else:
            checks.append({"check_id": "mutation", "kind": "MUTATION", "status": "NOT_RUN",
                           "summary": f"no mutation tool for {self.stack.language} in this runner "
                                      f"(PIT for the JVM or Stryker.NET would go here)", "metrics": {}, "findings": []})

        secrets = scan_secrets(tree, changed)
        checks.append({"check_id": "secret-scan", "kind": "SECRET_SCAN", "status": "FAIL" if secrets else "PASS",
                       "summary": f"{len(secrets)} potential secret(s) in {len(changed)} changed file(s)",
                       "metrics": {"files": len(changed), "findings": len(secrets)}, "findings": secrets})

        if self.stack.dependency_manifest == "requirements":
            deps, dep_findings = scan_dependencies(tree, self._advisories)
        else:
            deps, dep_findings = scan_jvm_or_nuget(tree, self.stack.dependency_manifest, self._advisories)
        checks.append({"check_id": "dependency-scan", "kind": "DEPENDENCY_SCAN",
                       "status": "FAIL" if dep_findings else "PASS",
                       "summary": f"{deps} declared dependenc{'y' if deps == 1 else 'ies'}, {len(dep_findings)} finding(s)",
                       "metrics": {"dependencies": deps, "findings": len(dep_findings)}, "findings": dep_findings})

        self._seq += 1
        payload = {
            "attestation_id": f"QATT-{self.runner_id}-{build_id}-{self._seq}",
            "runner_id": self.runner_id,
            "build_id": build_id,
            "source_revision": revision,
            "build_digest": build_digest_of(tree),
            "manifest_id": build["manifest_id"],
            "manifest_version": build["manifest_version"],
            "manifest_digest": build["manifest_digest"],
            "uat_digests": [{"test_id": t["test_id"], "test_digest": file_digest(tree[t["path"]])}
                            for t in manifest["tests"] if t["path"] in tree],
            "policy_version": self._gate.policy_version,
            "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "stack_digest": self.stack_digest,
            "checks": checks,
        }
        ingest = self._gate.ingest_quality_attestation(payload, sign_payload(self._key, payload, QUALITY_DOMAIN))
        record = RunRecord(build_id, "QUALITY", revision, payload, ingest, stdout)
        self.history.append(record)
        return record

    def _mutation(self, tree: Dict[str, bytes], changed: List[str], suite: List[str]) -> Dict[str, Any]:
        targets = [p for p in changed if p.startswith("src/") and p.endswith(".py") and p in tree]
        mutants = [m for p in targets for m in generate_mutants(p, tree[p].decode("utf-8"), self._max_mutants)]
        mutants = mutants[: self._max_mutants]

        def killed(mutant: Any) -> bool:
            results, _ = self._run_tests({**tree, mutant.path: mutant.source.encode()}, suite)
            return not results or any(o != "PASSED" for o in results.values())

        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
            verdicts = list(pool.map(killed, mutants))
        survivors = [m.description for m, k in zip(mutants, verdicts) if not k]
        return {"check_id": "mutation", "kind": "MUTATION", "status": "INFO",
                "summary": f"{len(mutants) - len(survivors)}/{len(mutants)} mutants killed in {len(targets)} changed file(s)",
                "metrics": {"mutants": len(mutants), "killed": len(mutants) - len(survivors)},
                "findings": survivors}

    def package(self, build_id: str, revision: str, name: str) -> RunRecord:
        """Build the deployable artifact from a merged revision and sign its provenance."""
        if self._registry is None:
            raise RuntimeError("runner has no artifact registry")
        build = self._gate.get_build(self._token, build_id)
        tree = self._workspace.snapshot(self._token, revision)
        sources = {p: c for p, c in tree.items() if p.startswith(BUILD_PREFIX)}
        data = deterministic_tar(sources)
        address = self._registry.put(self._token, data)
        self._seq += 1
        payload = {
            "provenance_id": f"PROV-{self.runner_id}-{build_id}-{self._seq}",
            "builder_id": self.runner_id,
            "build_id": build_id,
            "source_revision": revision,
            "build_digest": build_digest_of(tree),
            "artifact": {"name": name, "digest": address, "size": len(data)},
            "materials": [{"uri": f"repo://{p}", "digest": file_digest(c)} for p, c in sorted(sources.items())],
            "policy_version": self._gate.policy_version,
            "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        ingest = self._gate.ingest_provenance(payload, sign_payload(self._key, payload, PROVENANCE_DOMAIN))
        record = RunRecord(build["build_id"], "PACKAGE", revision, payload, ingest)
        self.history.append(record)
        return record

    def _run_tests(self, tree: Dict[str, bytes], test_paths: List[str]) -> Tuple[Dict[str, str], str]:
        paths = [p for p in test_paths if p in tree]
        if not paths:
            return {}, ""
        if self.stack.language != "python":
            return self._run_command(tree, paths)
        with tempfile.TemporaryDirectory(prefix="aidlc-g-run-") as tmp:
            root = Path(tmp).resolve()  # macOS: /var -> /private/var, else report paths don't match
            for rel, content in tree.items():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            (root / "pytest.ini").write_text("[pytest]\n")
            report = root / "report.xml"
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "src"),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(root / "pytest.ini"),
                     "--rootdir", str(root), "-o", "junit_family=xunit1", f"--junitxml={report}", *paths],
                    cwd=root, env=env, capture_output=True, text=True, timeout=self._timeout,
                )
                stdout = proc.stdout[-4000:]
            except subprocess.TimeoutExpired:
                return {p: "ERROR" for p in paths}, "timeout"
            return self._parse(report, paths), stdout

    def _run_command(self, tree: Dict[str, bytes], paths: List[str]) -> Tuple[Dict[str, str], str]:
        """JVM/.NET: run the stack's build command on a clean checkout and read the JUnit XML it writes."""
        with tempfile.TemporaryDirectory(prefix="aidlc-g-run-") as tmp:
            root = Path(tmp).resolve()
            for rel, content in tree.items():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            keep = ("PATH", "HOME", "JAVA_HOME", "MAVEN_OPTS", "GRADLE_USER_HOME", "DOTNET_ROOT", "DOTNET_CLI_HOME",
                    "NUGET_PACKAGES")
            env = {k: os.environ[k] for k in keep if k in os.environ}
            argv = expand_command(self.stack, [test_name(p) for p in paths])
            try:
                proc = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True,
                                      timeout=self.stack.timeout_seconds)
                stdout = (proc.stdout + proc.stderr)[-4000:]
            except subprocess.TimeoutExpired:
                return {p: "ERROR" for p in paths}, "timeout"
            except OSError as exc:  # build tool not installed
                return {p: "ERROR" for p in paths}, f"cannot run {argv[0]}: {exc}"
            return self._parse_junit(sorted(root.glob(self.stack.report_glob)), paths), stdout

    @staticmethod
    def _parse_junit(reports: List[Path], paths: List[str]) -> Dict[str, str]:
        """Map JUnit test cases to test files by class name (the file name); unmatched files are ERROR."""
        by_name: Dict[str, List[str]] = {}
        for p in paths:
            by_name.setdefault(test_name(p), []).append(p)
        per_file: Dict[str, List[str]] = {p: [] for p in paths}
        for report in reports:
            try:
                cases = list(ET.parse(report).getroot().iter("testcase"))
            except ET.ParseError:
                continue
            for case in cases:
                owners = by_name.get((case.get("classname") or "").rsplit(".", 1)[-1], [])
                if len(owners) != 1:  # unknown or ambiguous class name: never credited to a file
                    continue
                per_file[owners[0]].append("ERROR" if case.find("error") is not None else
                                           "FAILED" if case.find("failure") is not None else
                                           "SKIPPED" if case.find("skipped") is not None else "PASSED")
        return TrustedRunner._summarize(per_file)

    @staticmethod
    def _parse(report: Path, paths: List[str]) -> Dict[str, str]:
        per_file: Dict[str, List[str]] = {p: [] for p in paths}
        by_module = {p[:-3].replace("/", "."): p for p in paths if p.endswith(".py")}
        if report.exists():
            for case in ET.parse(report).getroot().iter("testcase"):
                path = case.get("file") or ""
                if path not in per_file:
                    path = by_module.get(case.get("classname") or "", "")
                if path not in per_file:
                    continue
                if case.find("error") is not None:
                    per_file[path].append("ERROR")
                elif case.find("failure") is not None:
                    per_file[path].append("FAILED")
                elif case.find("skipped") is not None:
                    per_file[path].append("SKIPPED")
                else:
                    per_file[path].append("PASSED")
        return TrustedRunner._summarize(per_file)

    @staticmethod
    def _summarize(per_file: Dict[str, List[str]]) -> Dict[str, str]:
        outcomes = {}
        for path, results in per_file.items():
            if not results:
                outcomes[path] = "ERROR"  # nothing collected: never treated as a pass
            elif "ERROR" in results:
                outcomes[path] = "ERROR"
            elif "FAILED" in results:
                outcomes[path] = "FAILED"
            elif all(r == "PASSED" for r in results):
                outcomes[path] = "PASSED"
            else:
                outcomes[path] = "SKIPPED"
        return outcomes
