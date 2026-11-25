"""M2 acceptance vertical slice: agents drive one change from request to independently tested build.

    request -> C0 -> discovery + clarification -> domain scenarios (owner decides)
            -> C1 -> ADR/C2 + interface
            -> stories -> locked UAT (+ failing baseline) -> implementation
            -> trusted CI run -> TEST_COVERAGE

The orchestrator is deterministic code, not an agent (section 7). It moves
evidence between agents, asks humans for decisions through concise packets
(Template E), and requests transitions; the gate decides. A PAUSE or BLOCK
stops the slice and is reported - nothing is retried around it.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .agents import (
    AcceptanceTestAgent,
    ArchitectureAgent,
    CodeReviewAgent,
    DocumentationAgent,
    ReleaseEvidenceAgent,
    TestCoverageAgent,
    Model,
    ImplementationAgent,
    RequirementsAgent,
    StoryPlanningAgent,
    ToolBroker,
    TraceEntry,
)
from .contracts import ContractType, StackBinding, StackProfile
from .crypto import generate_private_key, public_key_b64
from .errors import GovernanceError
from .deploy import DeploymentService, Telemetry
from .lifecycle import Stage
from .pullrequest import MergeService, PullRequest
from .registry import ArtifactRegistry, deterministic_tar
from .runner import RunRecord, TrustedRunner
from .service import ALLOW, GovernanceService
from .stacks import BUILT_IN, DEFAULT_STACK, instruction_refs, instructions_digest, profile_digest, unsupported_checks
from .testkit import CAST, ref
from .workspace import Workspace

# Which registered human holds each approval role in this slice.
PEOPLE = {"sponsor": "sponsor", "product_owner": "po", "engineering_lead": "eng_lead", "qa": "qa",
          "sre": "sre", "reviewer": "reviewer", "release_manager": "rel_mgr"}

AUTHORIZES = {
    "C0": "the initiative and key result as stated; requirements work may begin",
    "C1": "the business meaning, scope and exceptions of this requirement, and each domain scenario as the "
          "behavior you decided; design may begin",
    "C2": "the selected design and technical requirements; story planning may begin",
    "STORIES": "this division of the approved scenarios into stories as the scope to test and build",
    "C3": "these tests as faithful executions of the approved scenarios, locked as the definition of done",
    "DOCS": "this documentation as current for the tested build; with it the change may be merged",
    "RELEASE": "deploying this exact artifact to staging then production, with the stated canary, "
               "health checks and pre-authorized rollback",
}


# ------------------------------------------------------------------ humans


class HumanDesk(Protocol):
    def answer(self, principal_id: str, questions: List[Dict[str, Any]]) -> Dict[str, str]:
        ...

    def define_scenarios(self, principal_id: str, candidates: List[Dict[str, Any]],
                         answers: Dict[str, str]) -> List[Dict[str, Any]]:
        """The domain owner states the behavior: Given/When/Then, rule and kind for each scenario.
        Candidates are an agent's suggestions; the owner may keep, rewrite, drop or add."""
        ...

    def decide(self, principal_id: str, role: str, packet: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        ...

    def declare_risk(self, principal_id: str, proposal: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """The engineering lead states how risky the change is: {"tier": LOW|MEDIUM|HIGH, "rationale": ...}.
        None declares nothing, and the policy default (HIGH: every approval) applies."""
        ...

    def choose_stack(self, principal_id: str, proposal: Dict[str, Any]) -> Dict[str, Any]:
        """The engineering lead confirms or changes the team's stack and names any accepted quality gaps.
        Returns {"stack_id": ..., "quality_waivers": [...]}; stack_id is one of proposal["options"]."""
        ...

    def review(self, principal_id: str, packet: Dict[str, Any]) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        """PR review from a Template I packet: decision plus a status for each finding it addresses."""
        ...


class ScriptedDesk:
    """Stands in for people in tests and demos; records every packet it was shown."""

    def __init__(self, answers: Optional[Dict[str, str]] = None,
                 decisions: Optional[Dict[Tuple[str, str], str]] = None,
                 resolutions: Optional[Dict[str, Dict[str, Any]]] = None, review_decision: str = "APPROVE",
                 scenarios: Optional[List[Dict[str, Any]]] = None,
                 stack_choice: Optional[Dict[str, Any]] = None,
                 risk: Optional[Dict[str, str]] = None) -> None:
        self.answers = dict(answers or {})
        self.risk = dict(risk) if risk else None
        # None: accept the team's proposed stack with no waivers.
        self.stack_choice = dict(stack_choice) if stack_choice else None
        self.stack_proposals: List[Dict[str, Any]] = []
        # The owner's own scenarios. None means the owner accepts every candidate as written.
        self.scenarios = None if scenarios is None else [dict(s) for s in scenarios]
        self.scenario_candidates: List[Dict[str, Any]] = []
        self.decisions = dict(decisions or {})
        self.resolutions = dict(resolutions or {})
        self.review_decision = review_decision
        self.review_packets: List[Dict[str, Any]] = []
        self.packets: List[Dict[str, Any]] = []
        self.questions: List[Dict[str, Any]] = []

    def answer(self, principal_id: str, questions: List[Dict[str, Any]]) -> Dict[str, str]:
        self.questions.extend(questions)
        return {q["item_id"]: self.answers[q["item_id"]] for q in questions if q["item_id"] in self.answers}

    def define_scenarios(self, principal_id: str, candidates: List[Dict[str, Any]],
                         answers: Dict[str, str]) -> List[Dict[str, Any]]:
        self.scenario_candidates.extend(candidates)
        if self.scenarios is not None:
            return [dict(s) for s in self.scenarios]
        return [{"scenario_id": c["candidate_id"], **{k: c[k] for k in ("rule", "kind", "given", "when", "then")}}
                for c in candidates]

    def decide(self, principal_id: str, role: str, packet: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        self.packets.append(packet)
        return self.decisions.get((principal_id, packet["artifact_id"]), "APPROVE"), None

    def declare_risk(self, principal_id: str, proposal: Dict[str, Any]) -> Optional[Dict[str, str]]:
        return dict(self.risk) if self.risk else None

    def choose_stack(self, principal_id: str, proposal: Dict[str, Any]) -> Dict[str, Any]:
        self.stack_proposals.append(proposal)
        return self.stack_choice or {"stack_id": proposal["stack"]["stack_id"], "quality_waivers": []}

    def review(self, principal_id: str, packet: Dict[str, Any]) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        self.review_packets.append(packet)
        found = {f["finding_id"] for f in packet["ai_review_findings"]}
        return self.review_decision, {k: v for k, v in self.resolutions.items() if k in found}


def _summary(ctype: str, body: Dict[str, Any]) -> List[str]:
    if ctype == "C0":
        return [body["objective"], f"Key result: {body['metric']} (owner {body['metric_owner']})"]
    if ctype == "C1":
        return [body["requested_behavior"],
                *[f"{s['scenario_id']} ({s['kind']}, decided by {s['decided_by']}): Given {s['given']}, "
                  f"when {s['when']}, then {s['then']}" for s in body["domain_scenarios"]]]
    if ctype == "C2":
        stack = body.get("stack")
        stack_line = ([f"Stack: {stack['stack_id']} ({stack['framework']}), decided by {stack['decided_by']}; "
                       f"waived checks: {', '.join(stack['quality_waivers']) or 'none'}; "
                       f"instructions: {', '.join(i['path'] for i in stack['instructions']) or 'none'}"]
                      if stack else [])
        return [*stack_line, f"Decision: {body['adr']['decision']}", f"Selected option: {body['selected_option']}",
                f"Domain interface: {', '.join(body['domain_interface'])}",
                *[f"{d['scenario_id']}: {d['design_response']}" for d in body["scenario_designs"]]]
    if ctype == "STORIES":
        return [f"{s['story_id']} {s['title']}: {', '.join(s['scenario_ids'])}" for s in body["stories"]]
    if ctype == "C3":
        return [*[f"{b['scenario_id']}: Given {b['given']}, when {b['when']}, then {b['then']} "
                  f"-> {', '.join(b['test_ids'])}" for b in body["scenarios"]],
                *[f"PROPOSED {p['proposal_id']} (needs the domain owner): Given {p['given']}, when {p['when']}, "
                  f"then {p['then']}" for p in body.get("proposed_scenarios", [])]]
    if ctype == "RELEASE":
        return [f"Tag {body['tag']} -> {body['deployment_target']}",
                f"Artifact {body.get('artifact_digest')} from merged {body.get('merged_revision')}",
                *[f"Health: {c['metric']} {c['op']} {c['threshold']} {c['unit']}" for c in body.get("health_checks", [])],
                f"Canary: {body['canary_plan']}", f"Rollback: {body['rollback_plan']}"]
    if ctype == "DOCS":
        return [f"Monitoring baseline: {body['monitoring_baseline']}",
                *[f"{k}: {body[k]['uri']}" for k in ("user_docs", "runbook", "release_notes", "alerts", "rollback_plan")]]
    return []


def decision_packet(view: Dict[str, Any], previous: Optional[Dict[str, Any]], role: str) -> Dict[str, Any]:
    """Template E: what a human needs to make a genuine, bounded decision."""
    body, ctype = view["body"], view["contract_type"]
    open_items = [i for key in ("open_questions", "unknowns") for i in body.get(key, [])
                  if i["blocking"] and i["status"] == "OPEN"]
    changed = sorted(k for k in set(body) | set(previous["body"] if previous else {})
                     if previous is not None and body.get(k) != previous["body"].get(k))
    return {
        "decision": f"Approve {ctype} {view['artifact_id']}@v{view['version']}",
        "artifact_id": view["artifact_id"],
        "version": view["version"],
        "your_role": role,
        "summary": _summary(ctype, body),
        "open_blocking_decisions": [i["question"] for i in open_items] or "NONE",
        "existing_system_findings": [f"{o['statement']} [{o['source_uri']}]" for o in body.get("existing_behavior", [])],
        "conflicts": body.get("conflicts", []),
        "changes_since_last_version": changed if previous else "first version",
        "what_approval_authorizes": AUTHORIZES.get(ctype, ctype),
        "what_approval_does_not_authorize": "code merge, deployment or release",
        "evidence_receipt": {"digest": view["digest"], "policy_version": None},
    }


# -------------------------------------------------------------- environment


@dataclass
class SliceEnvironment:
    gate: GovernanceService
    admin: str
    tokens: Dict[str, str]
    workspace: Workspace
    runner: TrustedRunner
    registry: ArtifactRegistry
    deployer: DeploymentService
    baseline_digest: str

    @classmethod
    def create(cls, seed: Dict[str, bytes], advisories: Optional[Dict[Tuple[str, str], str]] = None,
               telemetry: Optional[Telemetry] = None, stack: StackProfile = DEFAULT_STACK,
               clock: Optional[Callable[[], str]] = None) -> "SliceEnvironment":
        gate, admin = GovernanceService.bootstrap(clock=clock)
        tokens = {pid: gate.register_principal(admin, pid, pid, kind, roles)["token"] for pid, kind, roles in CAST}
        tokens["ci-runner-1"] = gate.register_principal(admin, "ci-runner-1", "CI runner", "service", ["ci_runner"])["token"]
        tokens["merge-bot"] = gate.register_principal(admin, "merge-bot", "Merge service", "service", ["merge_service"])["token"]
        key = generate_private_key()
        gate.register_runner(admin, "ci-runner-1", public_key_b64(key))
        tokens["deploy-bot"] = gate.register_principal(admin, "deploy-bot", "Deployment service", "service",
                                                       ["deploy_service"])["token"]
        workspace = Workspace(gate, seed, protected={"main"})
        registry = ArtifactRegistry(gate)
        runner = TrustedRunner(gate, workspace, "ci-runner-1", key, tokens["ci-runner-1"], advisories=advisories,
                               registry=registry, stack=stack)
        # What every environment runs today (the legacy code on main): the first rollback target.
        baseline = registry.put(tokens["ci-runner-1"],
                                deterministic_tar({p: c for p, c in seed.items() if p.startswith("src/")}))
        for name in gate.policy.environments:
            gate.register_environment(admin, name, baseline)
        deployer = DeploymentService(gate, registry, tokens["deploy-bot"], telemetry or healthy_telemetry)
        return cls(gate, admin, tokens, workspace, runner, registry, deployer, baseline)


def healthy_telemetry(environment: str, artifact_digest: str, phase: str) -> Dict[str, int]:
    return {"critical_alert_delivery_pct": 100, "error_rate_bp": 12}


def product_ids(key: str) -> Dict[str, str]:
    """Artifact ids for one product change, derived from a short key such as NOTIFY or INVOICE."""
    return {
        "C0": f"INIT-{key}", "C1": f"BR-{key}", "C2": f"TR-{key}", "STORIES": f"STORIES-{key}",
        "C3": f"AC-{key}", "BASELINE": "BLD-BASELINE-1", "BUILD": "BLD-1", "DOCS": f"DOCS-{key}", "PR": "PR-1",
        "RELEASE": f"REL-{key}-1",
    }


@dataclass
class ProductRequest:
    sponsor_request: Dict[str, Any]
    change_request: str
    ids: Dict[str, str] = field(default_factory=lambda: product_ids("NOTIFY"))
    artifact_name: str = "notifications.tar"  # what the trusted runner packages for release


@dataclass
class Engineering:
    """The team's technology choice: a stack profile and the instruction files agents must follow."""

    stack: StackProfile = DEFAULT_STACK
    instructions: Dict[str, str] = field(default_factory=dict)  # file name -> text
    options: Dict[str, StackProfile] = field(default_factory=lambda: dict(BUILT_IN))


@dataclass
class SliceReport:
    work_item_id: str
    completed: bool = False
    halted: Optional[Dict[str, Any]] = None
    transitions: List[Dict[str, Any]] = field(default_factory=list)
    approvals: List[Dict[str, Any]] = field(default_factory=list)
    clarifications: List[Dict[str, Any]] = field(default_factory=list)
    domain_decisions: List[Dict[str, Any]] = field(default_factory=list)
    revisions: Dict[str, str] = field(default_factory=dict)
    runs: List[RunRecord] = field(default_factory=list)
    traces: List[TraceEntry] = field(default_factory=list)
    traceability: List[Dict[str, Any]] = field(default_factory=list)
    final_stage: str = Stage.INITIATIVE.value
    pr_packet: Optional[Dict[str, Any]] = None
    merge: Optional[Dict[str, Any]] = None
    merged_revision: Optional[str] = None
    deployments: List[Dict[str, Any]] = field(default_factory=list)
    release_manifest: Optional[Dict[str, Any]] = None
    stack_decision: Optional[Dict[str, Any]] = None
    risk: Optional[Dict[str, Any]] = None  # declaration, as the lead made it
    escalations: List[Dict[str, Any]] = field(default_factory=list)  # catch-up decisions after a tier raise
    latency: Optional[Dict[str, Any]] = None

    def to_markdown(self) -> str:
        lines = [f"# AIDLC-G delivery slice — {self.work_item_id}", ""]
        if self.completed and self.release_manifest:
            lines.append(f"**Outcome:** released `{self.release_manifest['tag']}` — artifact "
                         f"`{self.release_manifest['artifact_digest'][7:19]}` promoted through "
                         f"{' → '.join(d['environment'] for d in self.deployments)} with every gate ALLOW.")
        elif self.completed and self.merged_revision:
            lines.append(f"**Outcome:** reached `{self.final_stage}` and merged into protected `main` at "
                         f"`{self.merged_revision[:12]}` with every gate ALLOW.")
        elif self.completed:
            lines.append(f"**Outcome:** reached `{self.final_stage}` with every gate ALLOW.")
        else:
            lines.append(f"**Outcome:** halted at `{self.final_stage}` — `{json.dumps(self.halted)}`")
        if self.stack_decision:
            d = self.stack_decision
            lines += ["", "## Technology stack (decided by the team)", "",
                      f"- `{d['stack_id']}`: {d['framework']}, decided by {d['decided_by']}",
                      f"- Instruction files: {', '.join(i['path'] for i in d['instructions']) or 'none'}",
                      f"- Accepted quality gaps: {', '.join(d['quality_waivers']) or 'none'}"]
        if self.latency:
            L = self.latency
            r = L["risk"]
            signals = ", ".join(f"{x['code']} ({x['tier']})" for x in r["signals"]) or "none"
            lines += ["", "## Risk tier and decision latency", "",
                      f"- Declared tier: `{r['declared']}`; effective: `{r['effective']}`; evidence signals: {signals}",
                      f"- Decisions by people: {L['human_decisions']}; policy authorizations: {L['policy_decisions']}"
                      + (f" ({', '.join(L['policy_authorized'])})" if L["policy_authorized"] else ""),
                      f"- Approval queue: {L['approval_queue_minutes']['total']} min in total, critical path "
                      f"{L['approval_queue_minutes']['critical_path']} min, longest single wait "
                      f"{L['approval_queue_minutes']['max']} min (target {L['target_minutes']})",
                      f"- Review loops {L['review_loops']}; escalations (PAUSE) {L['escalations']}; tier raises "
                      f"{L['risk_raises']}; rework churn {L['rework_churn']}; lead time {L['lead_time_minutes']} min"]
            if self.escalations:
                lines += [f"- Catch-up decision after the tier rose: {e['artifact']} by {e['role']} ({e['decision']})"
                          for e in self.escalations]
        lines += ["", "## Transitions", "", "| To | Decision | Reasons |", "|---|---|---|"]
        for t in self.transitions:
            lines.append(f"| {t['to_stage']} | {t['decision']} | {', '.join(t['reason_codes']) or '—'} |")
        lines += ["", "## Human decisions", "", "| Artifact | Role | Principal | Decision |", "|---|---|---|---|"]
        for a in self.approvals:
            lines.append(f"| {a['artifact']} | {a['role']} | {a['principal']} | {a['decision']} |")
        if self.clarifications:
            lines += ["", "## Clarifications (product owner)", "", "| Item | Question | Blocking | Answer |", "|---|---|---|---|"]
            for c in self.clarifications:
                lines.append(f"| {c['item_id']} | {c['question']} | {c['blocking']} | {c.get('answer') or '*open*'} |")
        if self.domain_decisions:
            lines += ["", "## Domain scenarios (decided by the domain owner)", "",
                      "| Scenario | Owner's decision | Kind | Given / When / Then |", "|---|---|---|---|"]
            for d in self.domain_decisions:
                lines.append(f"| {d['scenario_id']} | {d['decision']} | {d['kind']} | {d['text']} |")
        if self.traceability:
            lines += ["", "## Traceability", "",
                      "| Domain scenario | Story | Test | Locked digest | Stub build | Candidate |",
                      "|---|---|---|---|---|---|"]
            for r in self.traceability:
                lines.append(f"| {r['scenario_id']} {r['rule']} | {r['story_id']} | {r['test_id']} `{r['path']}` | "
                             f"`{r['digest'][7:19]}` | {r['baseline'] or '—'} | {r['candidate'] or '—'} |")
        if self.runs:
            lines += ["", "## Trusted CI runs", "", "| Build | Kind | Revision | Outcomes | Attestation |", "|---|---|---|---|---|"]
            for run in self.runs:
                if run.kind == "QUALITY":
                    outcomes = ", ".join(f"{c['kind']}={c['status']}" for c in run.payload["checks"])
                elif run.kind == "PACKAGE":
                    outcomes = f"artifact `{run.payload['artifact']['digest'][7:19]}` ({run.payload['artifact']['size']} bytes)"
                else:
                    outcomes = ", ".join(f"{r['test_id']}={r['outcome']}" for r in run.payload["results"])
                att = run.payload.get("attestation_id") or run.payload.get("provenance_id")
                lines.append(f"| {run.build_id} | {run.kind} | `{run.revision[:12]}` | {outcomes} | {att} |")
        quality = [r for r in self.runs if r.kind == "QUALITY"]
        if quality:
            lines += ["", "## Quality evidence (Stage 7, signed)", "", "| Check | Status | Summary | Findings |",
                      "|---|---|---|---|"]
            for c in quality[-1].payload["checks"]:
                lines.append(f"| {c['kind']} | {c['status']} | {c['summary']} | {'; '.join(c['findings']) or '—'} |")
        if self.pr_packet:
            p = self.pr_packet
            lines += ["", f"## Pull request {p['pull_request']['id']} (Template I)", "",
                      f"- Branch `{p['pull_request']['branch']}` → `main`; head `{p['pull_request']['head'][:12]}`",
                      f"- Business requirement {p['business_requirement']['id']} approved by "
                      f"{', '.join(p['business_requirement']['approvers'])}",
                      f"- Technical requirement {p['technical_requirement_and_adr']['id']} / "
                      f"{p['technical_requirement_and_adr']['adr']} approved by "
                      f"{', '.join(p['technical_requirement_and_adr']['approvers'])}",
                      f"- Protected UAT {p['protected_uat']['manifest']} `{p['protected_uat']['digest'][7:19]}`",
                      f"- Implementation `{p['implementation']['commit'][:12]}` by {p['implementation']['implementer']}",
                      f"- Docs updated: {', '.join(p['docs_updated']) or 'none'}",
                      f"- Human review: {p['human_review_outcome']}",
                      f"- Security findings: {p['security_findings'] or 'none'}",
                      "", "| AI finding | Severity | Status | Summary |", "|---|---|---|---|"]
            for f in p["ai_review_findings"]:
                owner = f" (owner {f['risk_owner']})" if f.get("risk_owner") else ""
                lines.append(f"| {f['finding_id']} | {f['severity']} | {f['status']}{owner} | {f['summary']} |")
        if self.merge:
            lines += ["", "## Protected-branch merge decision", "",
                      f"`{self.merge['decision']}` {', '.join(r['code'] for r in self.merge['reasons']) or ''} "
                      f"(decision {self.merge['transition_id']})"]
        if self.deployments:
            lines += ["", "## Deployments", "",
                      "| Environment | Decision | Canary | Outcome | Rollback target | Running after |", "|---|---|---|---|---|---|"]
            for d in self.deployments:
                canary = d["verdict"] or "—"
                if d["failed_checks"]:
                    canary += f" ({'; '.join(d['failed_checks'])})"
                lines.append(f"| {d['environment']} | {d['decision']} {', '.join(d['reason_codes'])} | {canary} | "
                             f"{d['outcome']} | `{(d['rollback_target'] or '—')[7:19]}` | `{(d['running_after'] or '—')[7:19]}` |")
        if self.release_manifest:
            m = self.release_manifest
            prov = m["provenance"]
            lines += ["", f"## Release manifest {m['release_id']} (Template J)", "",
                      f"- Tag `{m['tag']}`; tested commit `{m['source_revision'][:12]}`; merged revision "
                      f"`{m['merged_revision'][:12]}`",
                      f"- Build digest `{m['build_digest'][7:19]}`; artifact `{m['artifact_digest'][7:19]}` "
                      f"({prov['artifact']['size']} bytes) built by `{prov['builder_id']}`, provenance `{prov['provenance_id']}`",
                      f"- Acceptance manifest {m['acceptance_manifest']['artifact_id']}@v{m['acceptance_manifest']['version']}; "
                      f"attestation {', '.join(m['test_attestations'])}",
                      f"- Release decided by "
                      + (", ".join(a["approver_id"] for a in m["human_release_approvers"])
                         or "policy authorization under rule " + ", ".join(p["rule"] for p in m.get("policy_release_authorizations", []))),
                      f"- Health checks: " + "; ".join(f"{c['metric']} {c['op']} {c['threshold']} {c['unit']}"
                                                       for c in m["health_checks"]),
                      f"- Rollback: {m['rollback_plan']}",
                      f"- Deployment events: " + ", ".join(f"{d['environment']}:{d['event']}" for d in m["production_deployment"])]
        lines += ["", "## Agent trace (Template H audit)", "",
                  "| Agent | Task | Model | Input | Output | Sources | Tool calls |", "|---|---|---|---|---|---|---|"]
        for t in self.traces:
            lines.append(f"| {t.agent_id} | {t.task} | {t.model_id} | `{t.input_digest[7:19]}` | `{t.output_digest[7:19]}` | "
                         f"{len(t.sources)} | {', '.join(t.actions) or '—'} |")
        return "\n".join(lines) + "\n"


class _Halt(Exception):
    pass


class SliceOrchestrator:
    def __init__(self, env: SliceEnvironment, model: Model, desk: HumanDesk, work_item_id: str = "WI-NOTIFY-001",
                 engineering: Optional[Engineering] = None) -> None:
        self.env = env
        self.desk = desk
        self.engineering = engineering
        self.work_item = work_item_id
        self.refs: Dict[str, Any] = {}
        self.branch = f"work/{work_item_id}"

        def broker(spec: Any, agent_id: str) -> ToolBroker:
            return ToolBroker(spec, agent_id, env.tokens[agent_id], env.gate, env.workspace, self.branch)

        self._broker = broker
        self.coverage = TestCoverageAgent(model, broker(TestCoverageAgent.spec, "coverage_agent"))
        self.reviewer = CodeReviewAgent(model, broker(CodeReviewAgent.spec, "review_agent"))
        self.docs = DocumentationAgent(model, broker(DocumentationAgent.spec, "docs_agent"))
        self.release_agent = ReleaseEvidenceAgent(model, broker(ReleaseEvidenceAgent.spec, "release_agent"))
        self.merge_service = MergeService(env.gate, env.workspace, env.tokens["merge-bot"])

        self.requirements = RequirementsAgent(model, broker(RequirementsAgent.spec, "requirements_agent"))
        self.architecture = ArchitectureAgent(model, broker(ArchitectureAgent.spec, "architecture_agent"))
        self.planner = StoryPlanningAgent(model, broker(StoryPlanningAgent.spec, "story_agent"))
        self.tester = AcceptanceTestAgent(model, broker(AcceptanceTestAgent.spec, "uat_agent"))
        self.implementer = ImplementationAgent(model, broker(ImplementationAgent.spec, "impl_agent"))

    # ----------------------------------------------------------------- steps

    def _approve(self, view: Dict[str, Any], report: SliceReport) -> None:
        gate = self.env.gate
        previous = gate.get_artifact(self.env.admin, view["artifact_id"], view["version"] - 1) if view["version"] > 1 else None
        needed = gate.required_approvers(self.env.tokens["orchestrator"], self.work_item, view["contract_type"])
        for role in needed["policy_roles"]:
            report.approvals.append({"artifact": f"{view['artifact_id']}@v{view['version']}", "role": role,
                                     "principal": "policy", "decision": f"POLICY AUTHORIZATION ({needed['tier']} tier)"})
        for role in needed["roles"]:
            principal = PEOPLE[role]
            packet = decision_packet(view, previous, role)
            packet["evidence_receipt"]["policy_version"] = gate.policy_version
            decision, rationale = self.desk.decide(principal, role, packet)
            gate.approve(self.env.tokens[principal], view["artifact_id"], view["version"], view["digest"],
                         decision, as_role=role, rationale=rationale)
            report.approvals.append({"artifact": f"{view['artifact_id']}@v{view['version']}", "role": role,
                                     "principal": principal, "decision": decision})

    def _evaluate(self, stage: Stage, key: str) -> Dict[str, Any]:
        return self.env.gate.evaluate_transition(self.env.tokens["orchestrator"], self.work_item, stage.value,
                                                 dict(self.refs), self.env.gate.policy_version, key)

    def _catch_up(self, decision: Dict[str, Any], report: SliceReport) -> bool:
        """Evidence raised the risk tier: ask only the people the higher tier now needs, then re-ask the gate."""
        codes = {r["code"] for r in decision["reasons"]}
        if decision["decision"] == ALLOW or "RISK_TIER_RAISED" not in codes or codes - {"RISK_TIER_RAISED", "APPROVAL_MISSING"}:
            return False
        gate = self.env.gate
        for reason in (r for r in decision["reasons"] if r["code"] == "APPROVAL_MISSING"):
            view = gate.get_artifact(self.env.admin, reason["artifact"]["artifact_id"], reason["artifact"]["version"])
            previous = gate.get_artifact(self.env.admin, view["artifact_id"], view["version"] - 1) if view["version"] > 1 else None
            for role in reason["roles"]:
                principal = PEOPLE[role]
                packet = decision_packet(view, previous, role)
                packet["evidence_receipt"]["policy_version"] = gate.policy_version
                packet["why_now"] = next(r["message"] for r in decision["reasons"] if r["code"] == "RISK_TIER_RAISED")
                verdict, rationale = self.desk.decide(principal, role, packet)
                gate.approve(self.env.tokens[principal], view["artifact_id"], view["version"], view["digest"],
                             verdict, as_role=role, rationale=rationale)
                entry = {"artifact": f"{view['artifact_id']}@v{view['version']}", "role": role,
                         "principal": principal, "decision": verdict}
                report.approvals.append(entry)
                report.escalations.append(entry)
        return True

    def _advance(self, stage: Stage, report: SliceReport) -> None:
        decision = self._evaluate(stage, f"{self.work_item}:{stage.value}")
        if self._catch_up(decision, report):
            report.transitions.append({"to_stage": stage.value, "decision": decision["decision"],
                                       "reason_codes": [r["code"] for r in decision["reasons"]],
                                       "transition_id": decision["transition_id"]})
            decision = self._evaluate(stage, f"{self.work_item}:{stage.value}:after-escalation")
        report.transitions.append({"to_stage": stage.value, "decision": decision["decision"],
                                   "reason_codes": [r["code"] for r in decision["reasons"]],
                                   "transition_id": decision["transition_id"]})
        if decision["decision"] != ALLOW:
            report.halted = {"stage": stage.value, "decision": decision["decision"], "reasons": decision["reasons"]}
            raise _Halt()
        report.final_stage = stage.value

    def run(self, request: ProductRequest) -> SliceReport:
        gate, ids = self.env.gate, request.ids
        report = SliceReport(self.work_item)
        gate.open_work_item(self.env.tokens["orchestrator"], self.work_item)
        working = Stage.EPIC
        try:
            self._declare_risk(report)
            c0 = self.requirements.draft_intent(request.sponsor_request, ids["C0"])
            self._approve(c0, report)
            self.refs["C0"] = ref(c0)
            self._advance(Stage.EPIC, report)

            working = Stage.SPIKE
            sources = self.requirements.discover()
            plan = self.requirements.clarify(request.change_request, sources)
            answers = self.desk.answer(PEOPLE["product_owner"], [q.model_dump() for q in plan.questions])
            report.clarifications = [{**q.model_dump(), "answer": answers.get(q.item_id)} for q in plan.questions]
            owner = PEOPLE["product_owner"]
            candidates = [c.model_dump() for c in plan.candidate_scenarios]
            scenarios = [{**s, "decided_by": owner}
                         for s in self.desk.define_scenarios(owner, candidates, answers)]
            report.domain_decisions = self._domain_decisions(candidates, scenarios)
            c1 = self.requirements.draft_requirement(request.change_request, c0, plan, answers, sources, ids["C1"],
                                                     scenarios)
            self._approve(c1, report)
            self.refs["C1"] = ref(c1)
            self._advance(Stage.SPIKE, report)

            working = Stage.STORIES
            binding = self._decide_stack(report) if self.engineering is not None else None
            c2, report.revisions["interface"] = self.architecture.design(c1, ids["C2"], binding)
            self._approve(c2, report)
            self.refs["C2"] = ref(c2)
            self._advance(Stage.STORIES, report)

            working = Stage.ACCEPTANCE_TESTS
            stories = self.planner.plan(c1, c2, ids["STORIES"])
            self._approve(stories, report)
            self.refs["STORIES"] = ref(stories)
            self._advance(Stage.ACCEPTANCE_TESTS, report)

            working = Stage.IMPLEMENTATION
            c3, report.revisions["acceptance_tests"] = self.tester.author(c1, c2, stories, ids["C3"])
            self._approve(c3, report)
            self.refs["C3"] = ref(c3)
            c3 = gate.get_artifact(self.env.admin, ids["C3"])
            self.tester.register_baseline(c3, report.revisions["acceptance_tests"], ids["BASELINE"])
            self.env.runner.execute(ids["BASELINE"])
            self.refs["BASELINE"] = ids["BASELINE"]
            self._advance(Stage.IMPLEMENTATION, report)

            working = Stage.TEST_COVERAGE
            report.revisions["implementation"], _ = self.implementer.implement(c1, c2, stories, c3, ids["BUILD"])
            self.env.runner.execute(ids["BUILD"])
            self.refs["BUILD"] = ids["BUILD"]
            self._advance(Stage.TEST_COVERAGE, report)
            report.completed = True
        except _Halt:
            pass
        except GovernanceError as exc:
            report.halted = {"stage": working.value, "error": exc.code, "message": exc.message}
        self._collect(report, ids)
        return report

    def _declare_risk(self, report: SliceReport) -> None:
        policy = self.env.gate.policy
        proposal = {"tiers": {t: {c.value: list(policy.approvers_for(c, t)) for c in policy.approvers}
                              for t in ("LOW", "MEDIUM", "HIGH")},
                    "default": policy.default_risk_tier,
                    "raised_by": ["SECURITY_RELEVANT_DESIGN -> HIGH", "BLOCKING_FINDING_RAISED -> HIGH",
                                  "QUALITY_CHECK_WAIVED -> MEDIUM", "RISK_ACCEPTED -> MEDIUM",
                                  f"LARGE_CHANGE (> {policy.low_risk_max_changed_files} files) -> MEDIUM"]}
        lead = PEOPLE["engineering_lead"]
        choice = self.desk.declare_risk(lead, proposal)
        if choice:
            report.risk = self.env.gate.declare_risk_tier(self.env.tokens[lead], self.work_item, choice["tier"],
                                                          choice["rationale"])

    def _decide_stack(self, report: SliceReport) -> Dict[str, Any]:
        """The engineering lead decides the stack before design; the binding goes into C2 verbatim."""
        eng = self.engineering
        lead = PEOPLE["engineering_lead"]
        proposal = {
            "stack": eng.stack.model_dump(mode="json"),
            "cannot_run": list(unsupported_checks(eng.stack)),
            "instructions": sorted(eng.instructions),
            "options": {sid: {"framework": p.framework, "build_tool": p.build_tool, "language": p.language,
                              "cannot_run": list(unsupported_checks(p))} for sid, p in eng.options.items()},
        }
        choice = self.desk.choose_stack(lead, proposal)
        profile = eng.stack if choice["stack_id"] == eng.stack.stack_id else eng.options[choice["stack_id"]]
        self.env.runner.use_stack(profile)  # platform configuration follows the human decision
        binding = StackBinding(
            stack_id=profile.stack_id, profile_digest=profile_digest(profile), framework=profile.framework,
            instructions=instruction_refs(eng.instructions), instructions_digest=instructions_digest(eng.instructions),
            quality_waivers=sorted(set(choice.get("quality_waivers", []))), decided_by=lead,
        ).model_dump(mode="json")
        context = {"stack": {**profile.model_dump(mode="json"), "cannot_run": list(unsupported_checks(profile))},
                   "team_instructions": dict(eng.instructions)}
        for agent in (self.architecture, self.planner, self.tester, self.implementer, self.coverage, self.reviewer,
                      self.docs, self.release_agent):
            agent.context = context
        report.stack_decision = binding
        return binding

    def _collect(self, report: SliceReport, ids: Dict[str, str]) -> None:
        report.latency = self.env.gate.decision_latency(self.env.admin, self.work_item)
        report.runs = list(self.env.runner.history)
        report.traces = [t for agent in (self.requirements, self.architecture, self.planner, self.tester,
                                         self.implementer, self.coverage, self.reviewer, self.docs, self.release_agent)
                         for t in agent.traces]
        report.traceability = self._traceability(ids, report.runs)

    def deliver(self, request: ProductRequest) -> SliceReport:
        """M3: run the M2 slice, then coverage, quality evidence, review, docs and a gated merge to main."""
        report = self.run(request)
        if not report.completed:
            return report
        report.completed = False
        gate, ids, ws = self.env.gate, request.ids, self.env.workspace
        working = Stage.DOCUMENTATION.value
        try:
            base = ws.branch_base(self.branch)
            c1, c2, c3 = (gate.get_artifact(self.env.admin, ids[s]) for s in ("C1", "C2", "C3"))
            build = gate.get_build(self.env.admin, ids["BUILD"])
            changed = ws.changed_paths(self.env.tokens["merge-bot"], base, build["source_revision"])
            report.revisions["coverage_tests"] = self.coverage.add_tests(c2, c3, changed)
            self.env.runner.quality(ids["BUILD"], report.revisions["coverage_tests"], base)
            reviewed = ws.changed_paths(self.env.tokens["merge-bot"], base, report.revisions["coverage_tests"])
            self.reviewer.review(c2, ids["BUILD"], reviewed, "REV-AGENT-1")

            pr = self.merge_service.open(ids["PR"], self.work_item, self.branch, ids["BUILD"], self.refs)
            decision, resolutions = self.desk.review(PEOPLE["reviewer"], self.merge_service.packet(pr))
            findings = []
            for f in self.merge_service.packet(pr)["ai_review_findings"]:
                findings.append({**f, **resolutions.get(f["finding_id"], {})})
            gate.record_review(self.env.tokens[PEOPLE["reviewer"]], "REV-HUMAN-1", ids["BUILD"], decision, findings)
            self._advance(Stage.DOCUMENTATION, report)

            working = "MERGE"
            docs, report.revisions["docs"] = self.docs.document(c1, c2, ids["BUILD"], changed, ids["DOCS"])
            self._approve(docs, report)
            decision = self.merge_service.merge(pr, ref(docs), f"{self.work_item}:merge")
            report.merge = {k: decision[k] for k in ("transition_id", "decision", "reasons", "head_revision")}
            report.pr_packet = self.merge_service.packet(pr, ref(docs))
            if decision["decision"] != ALLOW:
                report.halted = {"stage": "MERGE", "decision": decision["decision"], "reasons": decision["reasons"]}
            else:
                report.merged_revision = pr.merged_revision
                report.completed = True
        except _Halt:
            pass
        except GovernanceError as exc:
            report.halted = {"stage": working, "error": exc.code, "message": exc.message}
        self._collect(report, ids)
        return report

    def release(self, request: ProductRequest) -> SliceReport:
        """M4: deliver, then package with provenance, approve the release and deploy staging -> production."""
        report = self.deliver(request)
        if not report.completed:
            return report
        report.completed = False
        gate, ids, env = self.env.gate, request.ids, self.env
        working = Stage.RELEASE_READY.value
        try:
            env.runner.package(ids["BUILD"], report.merged_revision, request.artifact_name)
            docs = gate.get_artifact(env.admin, ids["DOCS"])
            release = self.release_agent.draft(docs, ids["BUILD"], ids["RELEASE"])
            self._approve(release, report)
            self.refs["DOCS"], self.refs["RELEASE"] = ref(docs), ref(release)
            self._advance(Stage.RELEASE_READY, report)
            release = gate.get_artifact(env.admin, ids["RELEASE"])
            for name in gate.policy.environments:
                working = f"DEPLOY:{name}"
                d = env.deployer.deploy(self.work_item, release, name, f"{self.work_item}:deploy:{name}")
                canary = next((e for e in d.events if e["event"] == "CANARY"), {})
                report.deployments.append({
                    "environment": name, "outcome": d.outcome, "error": d.error,
                    "decision": d.decision["decision"] if d.decision else "—",
                    "reason_codes": [r["code"] for r in d.decision["reasons"]] if d.decision else [d.error or ""],
                    "verdict": canary.get("verdict"), "failed_checks": canary.get("failed_checks", []),
                    "rollback_target": d.decision["rollback_target"] if d.decision else None,
                    "running_after": d.running_after,
                })
                if d.outcome != "PROMOTED":
                    report.halted = {"stage": working, "outcome": d.outcome, "error": d.error,
                                     "reasons": d.decision["reasons"] if d.decision else [],
                                     "failed_checks": canary.get("failed_checks", [])}
                    break
            else:
                report.release_manifest = gate.release_manifest(env.tokens[PEOPLE["release_manager"]], self.work_item)
                report.final_stage = "RELEASED"
                report.completed = True
        except _Halt:
            pass
        except GovernanceError as exc:
            report.halted = {"stage": working, "error": exc.code, "message": exc.message}
        self._collect(report, ids)
        return report

    @staticmethod
    def _domain_decisions(candidates: List[Dict[str, Any]], scenarios: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """What the owner did with each agent suggestion, and what the owner added unprompted."""
        def text(s: Dict[str, Any]) -> str:
            return f"Given {s['given']}, when {s['when']}, then {s['then']}"

        final = {s["scenario_id"]: s for s in scenarios}
        rows = []
        for c in candidates:
            s = final.get(c["candidate_id"])
            if s is None:
                rows.append({"scenario_id": c["candidate_id"], "decision": "DROPPED (agent proposal)",
                             "kind": c["kind"], "text": text(c)})
            else:
                same = all(s[k] == c[k] for k in ("rule", "kind", "given", "when", "then"))
                rows.append({"scenario_id": s["scenario_id"], "decision": "KEPT" if same else "REWRITTEN",
                             "kind": s["kind"], "text": text(s)})
        proposed = {c["candidate_id"] for c in candidates}
        rows += [{"scenario_id": s["scenario_id"], "decision": "ADDED BY OWNER", "kind": s["kind"], "text": text(s)}
                 for s in scenarios if s["scenario_id"] not in proposed]
        return rows

    def _traceability(self, ids: Dict[str, str], runs: List[RunRecord]) -> List[Dict[str, Any]]:
        if "STORIES" not in self.refs or "C3" not in self.refs:
            return []
        requirement = self.env.gate.get_artifact(self.env.admin, ids["C1"])["body"]
        stories = self.env.gate.get_artifact(self.env.admin, ids["STORIES"])["body"]
        manifest = self.env.gate.get_artifact(self.env.admin, ids["C3"])["body"]
        tests = {t["test_id"]: t for t in manifest["tests"]}
        story_of = {sid: s["story_id"] for s in stories["stories"] for sid in s["scenario_ids"]}
        bindings = {b["scenario_id"]: b for b in manifest["scenarios"]}
        outcome = {run.kind: {r["test_id"]: r["outcome"] for r in run.payload["results"]}
                   for run in runs if "results" in run.payload}
        rows = []
        for scenario in requirement["domain_scenarios"]:
            binding = bindings.get(scenario["scenario_id"])
            for tid in (binding["test_ids"] if binding else []):
                rows.append({
                    "scenario_id": scenario["scenario_id"], "rule": scenario["rule"],
                    "story_id": story_of.get(scenario["scenario_id"], "—"), "test_id": tid,
                    "path": tests[tid]["path"], "digest": tests[tid]["content_digest"],
                    "baseline": outcome.get("BASELINE_STUB", {}).get(tid),
                    "candidate": outcome.get("CANDIDATE", {}).get(tid),
                })
        return rows
