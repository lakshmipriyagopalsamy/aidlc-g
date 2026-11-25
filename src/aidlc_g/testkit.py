"""Synthetic world for tests and trials: the section 13 notification-preference walkthrough.

All data here is author-created smoke data (section 24.2): it exercises gate
logic and must not be reported as empirical evidence.
"""

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .canonical import file_digest
from .stacks import DEFAULT_STACK_DIGEST
from .crypto import PROVENANCE_DOMAIN, QUALITY_DOMAIN, generate_private_key, public_key_b64, sign_payload
from .lifecycle import STAGE_ORDER, Stage
from .policy import AGENT, DEFAULT_POLICY, HUMAN, SERVICE, Policy
from .service import ALLOW, GovernanceService

CAST: List[Tuple[str, str, List[str]]] = [
    ("sponsor", HUMAN, ["sponsor"]),
    ("po", HUMAN, ["product_owner"]),
    ("eng_lead", HUMAN, ["engineering_lead"]),
    ("qa", HUMAN, ["qa"]),
    ("reviewer", HUMAN, ["reviewer"]),
    ("sre", HUMAN, ["sre"]),
    ("rel_mgr", HUMAN, ["release_manager"]),
    ("orchestrator", SERVICE, ["orchestrator"]),
    ("requirements_agent", AGENT, ["requirements_agent"]),
    ("architecture_agent", AGENT, ["architecture_agent"]),
    ("story_agent", AGENT, ["story_planning_agent"]),
    ("uat_agent", AGENT, ["acceptance_test_agent"]),
    ("impl_agent", AGENT, ["implementation_agent"]),
    ("review_agent", AGENT, ["code_review_agent"]),
    ("coverage_agent", AGENT, ["test_coverage_agent"]),
    ("ops_agent", AGENT, ["operational_support_agent"]),
    ("docs_agent", AGENT, ["documentation_agent"]),
    ("release_agent", AGENT, ["release_evidence_agent"]),
]

# Protected UAT files (Template G), keyed by test id: (path, kind, content).
UAT_FILES: Dict[str, Tuple[str, str, bytes]] = {
    "UAT-001": ("uat/test_optional_suppressed.py", "UAT",
                b"def test_optional_suppressed(app):\n"
                b"    app.disable_optional('u1')\n"
                b"    app.emit('u1', 'marketing')\n"
                b"    assert app.delivered('u1') == []\n"),
    "UAT-002": ("uat/test_critical_delivered.py", "UAT",
                b"def test_critical_delivered(app):\n"
                b"    app.disable_optional('u1')\n"
                b"    app.emit('u1', 'security')\n"
                b"    assert app.delivered('u1') == ['security']\n"),
    "UAT-003": ("uat/test_cannot_modify_other_user.py", "NEGATIVE",
                b"def test_cannot_modify_other_user(app):\n"
                b"    assert app.as_user('u1').set_pref('u2', optional=False).status == 403\n"
                b"    assert app.prefs('u2').optional is True\n"),
    "UAT-004": ("uat/test_unknown_category_rejected.py", "BOUNDARY",
                b"def test_unknown_category_rejected(app):\n"
                b"    assert app.as_user('u1').set_pref('u1', category='nope').status == 422\n"),
}

# Domain scenarios as the product owner decided them (C1): business language only.
SCENARIOS = [
    ("SC-1", "optional notifications follow the user's choice", "NORMAL",
     "a user who turned off optional notifications", "a marketing message is sent to them",
     "they do not receive it", ["UAT-001"]),
    ("SC-2", "critical alerts are never silenced", "NORMAL",
     "a user who turned off optional notifications", "a security alert is raised for their account",
     "they receive the alert", ["UAT-002"]),
    ("SC-3", "only the account holder changes their preferences", "NEGATIVE",
     "two users, Ana and Ben", "Ana tries to turn off Ben's optional notifications",
     "the change is refused and Ben's preferences stay as they were", ["UAT-003"]),
    ("SC-4", "only known notification categories exist", "BOUNDARY",
     "any user", "a notification arrives in a category the product does not define",
     "it is rejected as unknown", ["UAT-004"]),
]
DOMAIN_OWNER = "po"

STORY_SCENARIOS = {"STORY-1": ["SC-1", "SC-2"], "STORY-2": ["SC-3", "SC-4"]}

DOC = {"uri": "docs://placeholder", "digest": file_digest(b"doc")}


def ref(view: Dict[str, Any]) -> Dict[str, Any]:
    return {"artifact_id": view["artifact_id"], "version": view["version"], "digest": view["digest"]}


class World:
    """A fresh gate service with the full cast, one runner and one work item."""

    def __init__(self, policy: Policy = DEFAULT_POLICY, work_item_id: str = "WI-NOTIFY-001") -> None:
        self.svc, self.admin = GovernanceService.bootstrap(policy=policy)
        self.tokens: Dict[str, str] = {}
        for pid, kind, roles in CAST:
            self.add_principal(pid, kind, roles)
        self.add_principal("merge-bot", SERVICE, ["merge_service"])
        self.add_principal("deploy-bot", SERVICE, ["deploy_service"])
        self.merged_head: Optional[str] = None
        self.provenance: Optional[Dict[str, Any]] = None
        self.runner_id = "ci-runner-1"
        self.runner_key = generate_private_key()
        self.svc.register_runner(self.admin, self.runner_id, public_key_b64(self.runner_key))
        self.test_files: Dict[str, bytes] = {tid: content for tid, (_, _, content) in UAT_FILES.items()}
        # What the runner would report each test importing; override to simulate a boundary bypass.
        self.test_imports: Dict[str, List[str]] = {}
        # The team's stack decision bound into C2 (None: default python-pytest), the stack the
        # simulated runner reports executing, and quality checks it reports it could not run.
        self.stack: Optional[Dict[str, Any]] = None
        self.stack_digest: str = DEFAULT_STACK_DIGEST
        self.quality_not_run: Tuple[str, ...] = ()
        self.refs: Dict[str, Any] = {}
        self.views: Dict[str, Dict[str, Any]] = {}
        self.builds: Dict[str, Dict[str, Any]] = {}
        self.work_item = work_item_id
        self.latencies: List[float] = []
        self._seq = 0
        self.svc.open_work_item(self.tok("orchestrator"), work_item_id)

    # ---------------------------------------------------------------- helpers

    def tok(self, principal_id: str) -> str:
        return self.tokens[principal_id]

    def add_principal(self, principal_id: str, kind: str, roles: List[str]) -> str:
        token = self.svc.register_principal(self.admin, principal_id, principal_id, kind, roles)["token"]
        self.tokens[principal_id] = token
        return token

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def submit(self, author: str, ctype: str, artifact_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        view = self.svc.submit_contract(self.tok(author), ctype, artifact_id, body)
        self.views[ctype] = view
        return view

    def approve(self, approver: str, view: Dict[str, Any], decision: str = "APPROVE",
                as_role: Optional[str] = None) -> Dict[str, Any]:
        return self.svc.approve(self.tok(approver), view["artifact_id"], view["version"], view["digest"],
                                decision, as_role=as_role)

    # ------------------------------------------------------------------ bodies

    def c0_body(self, **overrides: Any) -> Dict[str, Any]:
        body = {
            "initiative_id": "INIT-NOTIFY",
            "objective": "Give users control over optional notifications",
            "justification": "Users report unwanted optional notifications",
            "key_result_id": "KR-NOTIFY-1",
            "metric": "Share of active users who adjust notification settings",
            "measurement_source": "product analytics settings events",
            "metric_owner": "sponsor",
            "baseline_status": "OPEN_DECISION",
            "target_status": "OPEN_DECISION",
            "open_questions": [{
                "item_id": "Q-BASELINE", "question": "What is the current baseline?",
                "blocking": False, "status": "OPEN",
            }],
        }
        body.update(overrides)
        return body

    def c1_body(self, **overrides: Any) -> Dict[str, Any]:
        source = "repo://app/api/notifications.py"
        body = {
            "requirement_id": "BR-NOTIFY",
            "parent_okr": self.refs["C0"],
            "requested_behavior": "A signed-in user can disable optional notifications",
            "actors_and_permissions": ["signed-in user may change only their own preferences"],
            "normal_flow": "User disables optional notifications; optional events are not delivered",
            "alternates_and_failures": ["security-critical alerts are still delivered"],
            "in_scope": ["optional notification categories"],
            "out_of_scope": ["security-critical alerts"],
            "existing_behavior": [{
                "statement": "A preference endpoint exists for email opt-out",
                "classification": "CURRENT_BEHAVIOR", "source_uri": source,
            }],
            "requested_change": "Extend preferences to all optional categories",
            "domain_scenarios": [
                {"scenario_id": sid, "rule": rule, "kind": kind, "given": g, "when": w, "then": th,
                 "decided_by": DOMAIN_OWNER}
                for sid, rule, kind, g, w, th, _ in SCENARIOS
            ],
            "unknowns": [{
                "item_id": "U-CRITICAL", "question": "Which categories are security-critical?",
                "blocking": True, "status": "RESOLVED", "resolution": "security and account-recovery",
            }],
            "source_evidence": [{
                "uri": source, "revision": "9f1c2ab", "digest": file_digest(b"notifications.py"),
                "accessed_at": "2026-10-30T00:00:00Z", "permission_scope": "read_only",
            }],
        }
        body.update(overrides)
        return body

    def c2_body(self, **overrides: Any) -> Dict[str, Any]:
        body = {
            "technical_requirement_id": "TR-NOTIFY",
            "source_business_requirement": self.refs["C1"],
            "current_architecture": "Preferences stored per user in the settings service",
            "proposed_options": [
                {"name": "extend-store", "summary": "Add a category map to existing preferences"},
                {"name": "new-service", "summary": "Introduce a dedicated preference service"},
            ],
            "selected_option": "extend-store",
            "adr": {
                "adr_id": "ADR-NOTIFY", "title": "Extend existing preference storage",
                "status": "PROPOSED", "context": "Existing endpoint covers email only",
                "decision_drivers": ["minimal migration", "backward compatibility"],
                "decision": "Extend the existing store with a category map",
                "consequences": "Schema migration with default-on categories",
                "security_and_operability": "Owner-only writes; alert on delivery failures",
            },
            "scenario_designs": [{"scenario_id": sid, "design_response": f"PreferenceService handles {rule}"}
                                 for sid, rule, *_ in SCENARIOS],
            "domain_interface": ["notifications"],
            "operability": "Feature flag with rollback to email-only behaviour",
            "feasibility_and_cost": "Low; one migration",
        }
        if self.stack is not None:
            body["stack"] = dict(self.stack)
        body.update(overrides)
        return body

    def stories_body(self, **overrides: Any) -> Dict[str, Any]:
        body = {
            "story_set_id": "STORIES-NOTIFY",
            "business_requirement": self.refs["C1"],
            "technical_requirement": self.refs["C2"],
            "stories": [
                {"story_id": sid, "title": sid, "description": f"{sid} of the notification change",
                 "adr_section": "ADR-NOTIFY#decision",
                 "scenario_ids": list(scenario_ids)}
                for sid, scenario_ids in STORY_SCENARIOS.items()
            ],
        }
        body.update(overrides)
        return body

    def c3_body(self, drop_scenarios: Tuple[str, ...] = (), **overrides: Any) -> Dict[str, Any]:
        bound = [s for s in SCENARIOS if s[0] not in drop_scenarios]
        test_ids = {t for s in bound for t in s[6]}
        body = {
            "acceptance_contract_id": "AC-NOTIFY",
            "business_requirement": self.refs["C1"],
            "technical_requirement": self.refs["C2"],
            "story_set": self.refs["STORIES"],
            "scenarios": [
                {"scenario_id": sid, "given": g, "when": w, "then": th, "test_ids": list(tests)}
                for sid, _, _, g, w, th, tests in bound
            ],
            "tests": [
                {"test_id": tid, "path": path, "kind": kind, "content_digest": file_digest(content)}
                for tid, (path, kind, content) in UAT_FILES.items() if tid in test_ids
            ],
        }
        body.update(overrides)
        return body

    def docs_body(self, **overrides: Any) -> Dict[str, Any]:
        body = {
            "docs_id": "DOCS-NOTIFY",
            "build_id": self.refs["BUILD"],
            "user_docs": DOC, "runbook": DOC, "release_notes": DOC, "alerts": DOC, "rollback_plan": DOC,
            "monitoring_baseline": "optional-notification delivery rate per category; security alerts 100%",
        }
        body.update(overrides)
        return body

    def release_body(self, **overrides: Any) -> Dict[str, Any]:
        build = self.builds[self.refs["BUILD"]]
        body = {
            "release_id": "REL-NOTIFY-1",
            "tag": "v1.4.0",
            "build_id": build["build_id"],
            "source_revision": build["source_revision"],
            "build_digest": build["build_digest"],
            "documentation": self.refs["DOCS"],
            "deployment_target": "production",
            "canary_plan": "5% for 1h; abort if delivery-failure alert fires",
            "rollback_plan": "toggle feature flag; redeploy previous tag",
            "merged_revision": self.provenance["source_revision"] if self.provenance else None,
            "artifact_digest": self.provenance["artifact"]["digest"] if self.provenance else None,
            "provenance_id": self.provenance["provenance_id"] if self.provenance else None,
            "health_checks": [{"metric": "critical_alert_delivery_pct", "op": "GTE", "threshold": 100,
                               "unit": "percent"}],
        }
        body.update(overrides)
        return body

    # --------------------------------------------------------------- evidence

    def make_c0(self) -> None:
        view = self.submit("requirements_agent", "C0", "INIT-NOTIFY", self.c0_body())
        self.approve("sponsor", view)
        self.refs["C0"] = ref(view)

    def make_c1(self, **overrides: Any) -> None:
        view = self.submit("requirements_agent", "C1", "BR-NOTIFY", self.c1_body(**overrides))
        self.approve("po", view)
        self.refs["C1"] = ref(view)

    def make_c2(self, **overrides: Any) -> None:
        view = self.submit("architecture_agent", "C2", "TR-NOTIFY", self.c2_body(**overrides))
        self.approve("eng_lead", view)
        self.refs["C2"] = ref(view)

    def make_stories(self) -> None:
        view = self.submit("story_agent", "STORIES", "STORIES-NOTIFY", self.stories_body())
        self.approve("eng_lead", view)
        self.approve("po", view)
        self.refs["STORIES"] = ref(view)

    def make_c3(self, drop_scenarios: Tuple[str, ...] = (), **overrides: Any) -> None:
        view = self.submit("uat_agent", "C3", "AC-NOTIFY", self.c3_body(drop_scenarios, **overrides))
        self.approve("po", view)
        self.approve("qa", view)
        self.refs["C3"] = ref(view)

    def register_build(self, build_id: str, kind: str = "CANDIDATE", registrant: str = "impl_agent",
                       revision: Optional[str] = None) -> Dict[str, Any]:
        record = self.svc.register_build(
            self.tok(registrant), build_id, kind, revision or file_digest(build_id.encode())[7:19],
            file_digest(b"artifact:" + build_id.encode()), self.refs["C3"],
        )
        self.builds[build_id] = record
        return record

    def attestation(self, build_id: str, outcome: str = "PASSED",
                    skip_tests: Tuple[str, ...] = (), **overrides: Any) -> Dict[str, Any]:
        build = self.builds[build_id]
        manifest = build["manifest"]
        in_manifest = set(self._manifest_test_ids(build_id))
        payload = {
            "attestation_id": self.next_id("ATT"),
            "runner_id": self.runner_id,
            "build_id": build_id,
            "source_revision": build["source_revision"],
            "build_digest": build["build_digest"],
            "manifest_id": manifest["artifact_id"],
            "manifest_version": manifest["version"],
            "manifest_digest": manifest["digest"],
            "policy_version": self.svc.policy_version,
            "issued_at": "2026-10-30T12:00:00Z",
            "stack_digest": self.stack_digest,
            "results": [
                {"test_id": tid, "test_digest": file_digest(content), "outcome": outcome,
                 "project_imports": list(self.test_imports.get(tid, ["notifications"]))}
                for tid, content in sorted(self.test_files.items())
                if tid not in skip_tests and tid in in_manifest
            ],
        }
        payload.update(overrides)
        return payload

    def _manifest_test_ids(self, build_id: str) -> List[str]:
        m = self.builds[build_id]["manifest"]
        body = self.svc.get_artifact(self.admin, m["artifact_id"], m["version"])["body"]
        return [t["test_id"] for t in body["tests"]]

    def sign(self, payload: Dict[str, Any], key: Any = None) -> str:
        return sign_payload(key or self.runner_key, payload)

    def attest(self, build_id: str, outcome: str = "PASSED", **kwargs: Any) -> Dict[str, Any]:
        payload = self.attestation(build_id, outcome, **kwargs)
        return self.svc.ingest_attestation(payload, self.sign(payload))

    def make_baseline(self) -> None:
        self.register_build("BLD-STUB-1", kind="BASELINE_STUB", registrant="uat_agent")
        self.attest("BLD-STUB-1", outcome="FAILED")
        self.refs["BASELINE"] = "BLD-STUB-1"

    def make_candidate(self, build_id: str = "BLD-1", registrant: str = "impl_agent") -> None:
        self.register_build(build_id, registrant=registrant)
        self.attest(build_id, outcome="PASSED")
        self.refs["BUILD"] = build_id

    def quality_attestation(self, build_id: Optional[str] = None, **overrides: Any) -> Dict[str, Any]:
        build = self.builds[build_id or self.refs["BUILD"]]
        manifest = build["manifest"]
        body = self.svc.get_artifact(self.admin, manifest["artifact_id"], manifest["version"])["body"]
        payload = {
            "attestation_id": self.next_id("QATT"),
            "runner_id": self.runner_id,
            "build_id": build["build_id"],
            "source_revision": build["source_revision"],
            "build_digest": build["build_digest"],
            "manifest_id": manifest["artifact_id"],
            "manifest_version": manifest["version"],
            "manifest_digest": manifest["digest"],
            "uat_digests": [{"test_id": t["test_id"], "test_digest": file_digest(self.test_files[t["test_id"]])}
                            for t in body["tests"]],
            "policy_version": self.svc.policy_version,
            "issued_at": "2026-10-30T12:00:00Z",
            "stack_digest": self.stack_digest,
            "checks": [
                {"check_id": "cov", "kind": "COVERAGE_TESTS", "status": "PASS", "summary": "3/3 passed",
                 "metrics": {"tests": 3, "passed": 3}, "findings": []},
                {"check_id": "mut", "kind": "MUTATION", "status": "INFO", "summary": "10/10 killed",
                 "metrics": {"mutants": 10, "killed": 10}, "findings": []},
                {"check_id": "sec", "kind": "SECRET_SCAN", "status": "PASS", "summary": "no secrets", "metrics": {}, "findings": []},
                {"check_id": "dep", "kind": "DEPENDENCY_SCAN", "status": "PASS", "summary": "no advisories", "metrics": {}, "findings": []},
            ],
        }
        for check in payload["checks"]:
            if check["kind"] in self.quality_not_run:
                check.update(status="NOT_RUN", summary="no tool for this stack", metrics={}, findings=[])
        payload.update(overrides)
        return payload

    def make_quality(self, **overrides: Any) -> Dict[str, Any]:
        payload = self.quality_attestation(**overrides)
        return self.svc.ingest_quality_attestation(payload, sign_payload(self.runner_key, payload, QUALITY_DOMAIN))

    def make_review(self, reviewer: str = "reviewer") -> None:
        self.svc.record_review(self.tok(reviewer), self.next_id("REV"), self.refs["BUILD"], "APPROVE", [])

    def make_docs(self) -> None:
        view = self.submit("docs_agent", "DOCS", "DOCS-NOTIFY", self.docs_body())
        self.approve("po", view)
        self.approve("sre", view)
        self.refs["DOCS"] = ref(view)

    def make_merge(self, changed: Tuple[str, ...] = ()) -> Dict[str, Any]:
        build = self.builds[self.refs["BUILD"]]
        decision = self.svc.evaluate_merge(self.tok("merge-bot"), self.work_item, "work/x", build["source_revision"],
                                           build["source_revision"], list(changed), {"DOCS": self.refs["DOCS"]},
                                           self.next_id("MRG"))
        if decision["decision"] == ALLOW:
            self.merged_head = build["source_revision"]
        return decision

    def provenance_payload(self, **overrides: Any) -> Dict[str, Any]:
        build = self.builds[self.refs["BUILD"]]
        payload = {
            "provenance_id": self.next_id("PROV"),
            "builder_id": self.runner_id,
            "build_id": build["build_id"],
            "source_revision": self.merged_head or build["source_revision"],
            "build_digest": build["build_digest"],
            "artifact": {"name": "notifications.tar", "digest": file_digest(b"artifact-bytes:" + build["build_id"].encode()),
                         "size": 1024},
            "materials": [{"uri": "repo://src/notifications/__init__.py", "digest": file_digest(b"src")}],
            "policy_version": self.svc.policy_version,
            "issued_at": "2026-10-30T12:00:00Z",
        }
        payload.update(overrides)
        return payload

    def make_provenance(self, **overrides: Any) -> Dict[str, Any]:
        payload = self.provenance_payload(**overrides)
        record = self.svc.ingest_provenance(payload, sign_payload(self.runner_key, payload, PROVENANCE_DOMAIN))
        self.provenance = payload
        return record

    def make_release(self) -> None:
        view = self.submit("release_agent", "RELEASE", "REL-NOTIFY", self.release_body())
        self.approve("rel_mgr", view)
        self.refs["RELEASE"] = ref(view)

    STEPS: List[Tuple[Stage, Tuple[str, ...]]] = [
        (Stage.EPIC, ("make_c0",)),
        (Stage.SPIKE, ("make_c1",)),
        (Stage.STORIES, ("make_c2",)),
        (Stage.ACCEPTANCE_TESTS, ("make_stories",)),
        (Stage.IMPLEMENTATION, ("make_c3", "make_baseline")),
        (Stage.TEST_COVERAGE, ("make_candidate",)),
        (Stage.DOCUMENTATION, ("make_quality", "make_review")),
        (Stage.RELEASE_READY, ("make_docs", "make_merge", "make_provenance", "make_release")),
    ]

    # ------------------------------------------------------------- transitions

    def transition(self, target: Stage, requester: str = "orchestrator", refs: Optional[Dict[str, Any]] = None,
                   key: Optional[str] = None, policy_version: Optional[str] = None) -> Dict[str, Any]:
        started = time.perf_counter()
        decision = self.svc.evaluate_transition(
            self.tok(requester), self.work_item, target.value,
            dict(self.refs) if refs is None else refs,
            policy_version or self.svc.policy_version,
            key or self.next_id("IDEM"),
        )
        self.latencies.append(time.perf_counter() - started)
        return decision

    def advance_to(self, target: Stage) -> None:
        """Produce evidence and transition stage by stage until the work item is at target."""
        for stage, makers in self.STEPS:
            if STAGE_ORDER.index(stage) > STAGE_ORDER.index(target):
                return
            for maker in makers:
                getattr(self, maker)()
            decision = self.transition(stage)
            if decision["decision"] != ALLOW:
                raise RuntimeError(f"setup could not enter {stage.value}: {decision['reasons']}")

    def prepare(self, target: Stage) -> None:
        """Advance to the stage before target, then produce target's evidence without transitioning."""
        previous = STAGE_ORDER[STAGE_ORDER.index(target) - 1]
        self.advance_to(previous)
        for stage, makers in self.STEPS:
            if stage == target:
                for maker in makers:
                    getattr(self, maker)()


def expect_error(fn: Callable[[], Any]) -> Optional[str]:
    """Run fn; return the GovernanceError code it raised, or None if it succeeded."""
    from .errors import GovernanceError

    try:
        fn()
    except GovernanceError as exc:
        return exc.code
    return None
