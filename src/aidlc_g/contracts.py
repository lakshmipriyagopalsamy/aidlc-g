"""M0 contract schemas: C0-C3 plus the story, documentation and release records.

Field sets follow compendium Part III templates A-D, F, J. Schemas reject
unknown fields so a caller cannot smuggle unreviewed content into a digest.
Agents draft most fields and the gate validates, digests and stores them.

The exception is domain behavior. C1 carries the domain scenarios: concrete
Given/When/Then examples in business language, each owned by a named human
domain owner. Agents may propose candidates, but only the owner's decision
puts a scenario in C1. Everything downstream traces to those scenarios: C2
says how each is met, stories partition them, and C3 binds each one to
executable tests without rewording it.

The same holds for the technology. The team decides the stack (language,
framework, build tool) and writes instruction files for the agents; C2
binds that decision by digest, and every CI attestation names the stack it
actually ran, so neither an agent nor a runner can quietly switch it.
"""

from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

NonEmpty = Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Revision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]
ModuleName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")]


class ContractType(str, Enum):
    C0 = "C0"  # business intent / OKRs
    C1 = "C1"  # business requirements
    C2 = "C2"  # technical requirements + ADR
    STORIES = "STORIES"  # traceable implementation units (stage 4)
    C3 = "C3"  # acceptance evidence manifest
    DOCS = "DOCS"  # user + operational documentation (stage 8)
    RELEASE = "RELEASE"  # release record (after stage 8)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(_Strict):
    artifact_id: Identifier
    version: int = Field(ge=1)
    digest: Digest


class OpenItem(_Strict):
    """An unresolved business or technical decision; never silently resolved."""

    item_id: Identifier
    question: NonEmpty
    blocking: bool
    status: Literal["OPEN", "RESOLVED", "ACCEPTED_WITH_SCOPE"]
    resolution: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def _resolution_recorded(self) -> "OpenItem":
        if self.status != "OPEN" and self.resolution is None:
            raise ValueError(f"{self.item_id}: a closed item must record its resolution")
        return self


class SourceEvidence(_Strict):
    """Citation produced by governed read-only discovery (section 5.5)."""

    uri: NonEmpty
    revision: NonEmpty
    digest: Digest
    accessed_at: NonEmpty
    permission_scope: Literal["read_only"]


class Observation(_Strict):
    statement: NonEmpty
    classification: Literal["CURRENT_BEHAVIOR", "PRIOR_DECISION", "STAKEHOLDER_CLAIM", "PROPOSED_FUTURE"]
    source_uri: NonEmpty


class _Contract(_Strict):
    def references(self) -> Dict[str, ArtifactRef]:
        """Upstream contracts this one derives from, keyed by expected type."""
        return {}

    def build_reference(self) -> Optional[str]:
        return None


# --- C0 -----------------------------------------------------------------------


class C0BusinessIntent(_Contract):
    initiative_id: Identifier
    objective: NonEmpty
    justification: NonEmpty
    key_result_id: Identifier
    metric: NonEmpty
    measurement_source: NonEmpty
    metric_owner: NonEmpty
    baseline_status: Literal["KNOWN", "OPEN_DECISION"]
    baseline: Optional[NonEmpty] = None
    target_status: Literal["KNOWN", "OPEN_DECISION"]
    target: Optional[NonEmpty] = None
    timeframe: Optional[NonEmpty] = None
    out_of_scope: List[NonEmpty] = []
    dependencies: List[NonEmpty] = []
    open_questions: List[OpenItem] = []

    @model_validator(mode="after")
    def _no_invented_metrics(self) -> "C0BusinessIntent":
        # Section 5.1: missing baselines stay explicit open decisions.
        for name in ("baseline", "target"):
            status, value = getattr(self, f"{name}_status"), getattr(self, name)
            if status == "KNOWN" and value is None:
                raise ValueError(f"{name}_status is KNOWN but no {name} value was supplied")
            if status == "OPEN_DECISION" and value is not None:
                raise ValueError(f"{name} is an open decision; a value must not be supplied")
        return self


# --- C1 -----------------------------------------------------------------------


class ScenarioText(_Strict):
    """Behavior in the language of the domain: no class, endpoint or table names."""

    given: NonEmpty
    when: NonEmpty
    then: NonEmpty

    def text(self) -> Dict[str, str]:
        return {"given": self.given, "when": self.when, "then": self.then}


class DomainScenario(ScenarioText):
    scenario_id: Identifier
    rule: NonEmpty  # the business rule this example illustrates
    kind: Literal["NORMAL", "NEGATIVE", "BOUNDARY"]
    # The human who decided this behavior. The gate requires that person to hold a
    # domain-owner role and to have approved this exact C1 version.
    decided_by: Identifier


class C1BusinessRequirement(_Contract):
    requirement_id: Identifier
    parent_okr: ArtifactRef
    requested_behavior: NonEmpty
    actors_and_permissions: List[NonEmpty] = Field(min_length=1)
    preconditions: List[NonEmpty] = []
    normal_flow: NonEmpty
    alternates_and_failures: List[NonEmpty] = []
    in_scope: List[NonEmpty] = Field(min_length=1)
    out_of_scope: List[NonEmpty] = []
    quality_expectations: List[NonEmpty] = []
    existing_behavior: List[Observation] = []
    requested_change: NonEmpty
    domain_scenarios: List[DomainScenario] = Field(min_length=1)
    conflicts: List[NonEmpty] = []
    unknowns: List[OpenItem] = []
    source_evidence: List[SourceEvidence] = []

    @model_validator(mode="after")
    def _observations_are_cited(self) -> "C1BusinessRequirement":
        cited = {s.uri for s in self.source_evidence}
        for obs in self.existing_behavior:
            if obs.source_uri not in cited:
                raise ValueError(f"observation cites {obs.source_uri!r}, which is not in source_evidence")
        ids = [s.scenario_id for s in self.domain_scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("domain scenario ids must be unique")
        return self

    def scenarios(self) -> Dict[str, DomainScenario]:
        return {s.scenario_id: s for s in self.domain_scenarios}

    def references(self) -> Dict[str, ArtifactRef]:
        return {"C0": self.parent_okr}


# --- C2 + ADR -----------------------------------------------------------------


class DesignOption(_Strict):
    name: Identifier
    summary: NonEmpty
    benefits: List[NonEmpty] = []
    costs: List[NonEmpty] = []
    risks: List[NonEmpty] = []


class ArchitectureDecisionRecord(_Strict):
    adr_id: Identifier
    title: NonEmpty
    status: Literal["PROPOSED", "ACCEPTED", "SUPERSEDED"]
    context: NonEmpty
    decision_drivers: List[NonEmpty] = Field(min_length=1)
    decision: NonEmpty
    consequences: NonEmpty
    security_and_operability: NonEmpty


Language = Literal["python", "java", "kotlin", "csharp"]
WaivableCheck = Literal["MUTATION", "DEPENDENCY_SCAN"]


class StackProfile(_Strict):
    """How a technology stack builds and tests. The trusted runner executes exactly this."""

    stack_id: Identifier
    language: Language
    framework: NonEmpty  # e.g. "Spring Boot 3", "Micronaut 4", "ASP.NET Core 8"
    build_tool: NonEmpty  # e.g. "Maven", "Gradle", "dotnet"
    test_file_glob: NonEmpty  # basename pattern of a test file, e.g. "*Test.java"
    # Empty for python: the runner's built-in pytest executor. Otherwise argv run in the checkout;
    # "{names}" becomes the selected test names, "{each}" expands each_args once per name.
    test_command: List[NonEmpty] = []
    name_format: NonEmpty = "{name}"
    name_separator: NonEmpty = ","
    each_args: List[NonEmpty] = []
    report_glob: NonEmpty = "report.xml"  # JUnit XML written by the test command
    timeout_seconds: int = Field(default=600, ge=1, le=7200)
    dependency_manifest: Literal["requirements", "maven", "gradle", "nuget"] = "requirements"
    build_notes: List[NonEmpty] = []  # layout rules agents must follow, e.g. test source roots


class InstructionRef(_Strict):
    path: NonEmpty
    digest: Digest


class StackBinding(_Strict):
    """The team's technology decision, bound into C2. An engineering lead makes it, not an agent."""

    stack_id: Identifier
    profile_digest: Digest
    framework: NonEmpty
    instructions: List[InstructionRef] = []
    instructions_digest: Digest
    # Checks the runner cannot perform on this stack, accepted by name as a known gap.
    quality_waivers: List[WaivableCheck] = []
    decided_by: Identifier


class ScenarioDesign(_Strict):
    scenario_id: Identifier
    design_response: NonEmpty  # how the selected design makes this scenario true


class C2TechnicalRequirement(_Contract):
    technical_requirement_id: Identifier
    source_business_requirement: ArtifactRef
    current_architecture: NonEmpty
    proposed_options: List[DesignOption] = Field(min_length=1)
    selected_option: Identifier
    adr: ArchitectureDecisionRecord
    scenario_designs: List[ScenarioDesign] = Field(min_length=1)
    # The public modules through which acceptance tests exercise the domain. Tests that
    # import any other project module are testing internals, not behavior.
    domain_interface: List[ModuleName] = Field(min_length=1)
    api_or_data_contracts: List[NonEmpty] = []
    quality_attributes: List[NonEmpty] = []
    threat_model: List[NonEmpty] = []
    operability: NonEmpty
    feasibility_and_cost: NonEmpty
    new_product_decisions_required: List[NonEmpty] = []
    stack: Optional[StackBinding] = None  # absent: the default python-pytest stack

    @model_validator(mode="after")
    def _selected_option_exists(self) -> "C2TechnicalRequirement":
        names = [o.name for o in self.proposed_options]
        if len(names) != len(set(names)):
            raise ValueError("design option names must be unique")
        if self.selected_option not in names:
            raise ValueError(f"selected_option {self.selected_option!r} is not one of the proposed options")
        designed = [d.scenario_id for d in self.scenario_designs]
        if len(designed) != len(set(designed)):
            raise ValueError("each scenario has at most one design response")
        return self

    def references(self) -> Dict[str, ArtifactRef]:
        return {"C1": self.source_business_requirement}


# --- Stories ------------------------------------------------------------------


class Story(_Strict):
    """A unit of delivery. Stories divide the domain scenarios; they never add behavior."""

    story_id: Identifier
    title: NonEmpty
    description: NonEmpty
    adr_section: NonEmpty
    scenario_ids: List[Identifier] = Field(min_length=1)


class StoryPlan(_Contract):
    story_set_id: Identifier
    business_requirement: ArtifactRef
    technical_requirement: ArtifactRef
    stories: List[Story] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "StoryPlan":
        story_ids = [s.story_id for s in self.stories]
        if len(story_ids) != len(set(story_ids)):
            raise ValueError("story ids must be unique")
        planned = self.scenario_ids()
        if len(planned) != len(set(planned)):
            raise ValueError("each domain scenario belongs to exactly one story")
        return self

    def scenario_ids(self) -> List[str]:
        return [sid for s in self.stories for sid in s.scenario_ids]

    def references(self) -> Dict[str, ArtifactRef]:
        return {"C1": self.business_requirement, "C2": self.technical_requirement}


# --- C3 -----------------------------------------------------------------------


class ScenarioBinding(ScenarioText):
    """One approved C1 scenario, repeated word for word, bound to the tests that execute it."""

    scenario_id: Identifier
    test_ids: List[Identifier] = Field(min_length=1)


class ProposedScenario(ScenarioText):
    """Behavior the test author believes is missing. It goes back to the domain owner;
    it is never tested until the owner adds it to C1."""

    proposal_id: Identifier
    rationale: NonEmpty


class TestDefinition(_Strict):
    __test__ = False  # not a pytest class

    test_id: Identifier
    path: NonEmpty
    content_digest: Digest
    kind: Literal["UAT", "NEGATIVE", "BOUNDARY"]


class C3AcceptanceManifest(_Contract):
    acceptance_contract_id: Identifier
    business_requirement: ArtifactRef
    technical_requirement: ArtifactRef
    story_set: ArtifactRef
    scenarios: List[ScenarioBinding] = Field(min_length=1)
    tests: List[TestDefinition] = Field(min_length=1)
    proposed_scenarios: List[ProposedScenario] = []

    @model_validator(mode="after")
    def _consistent(self) -> "C3AcceptanceManifest":
        test_ids = [t.test_id for t in self.tests]
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("test ids must be unique within a manifest")
        bound = [b.scenario_id for b in self.scenarios]
        if len(bound) != len(set(bound)):
            raise ValueError("each scenario is bound once within a manifest")
        declared = set(test_ids)
        referenced = set()
        for b in self.scenarios:
            unknown = set(b.test_ids) - declared
            if unknown:
                raise ValueError(f"{b.scenario_id} references undeclared tests {sorted(unknown)}")
            referenced.update(b.test_ids)
        orphans = declared - referenced
        if orphans:
            raise ValueError(f"tests {sorted(orphans)} are not traced to any scenario")
        return self

    def references(self) -> Dict[str, ArtifactRef]:
        return {
            "C1": self.business_requirement,
            "C2": self.technical_requirement,
            "STORIES": self.story_set,
        }


# --- Documentation and release -------------------------------------------------


class DocRef(_Strict):
    uri: NonEmpty
    digest: Digest


class ReleaseDocumentation(_Contract):
    docs_id: Identifier
    build_id: Identifier
    user_docs: DocRef
    runbook: DocRef
    release_notes: DocRef
    alerts: DocRef
    rollback_plan: DocRef
    api_docs: Optional[DocRef] = None
    monitoring_baseline: NonEmpty

    def build_reference(self) -> Optional[str]:
        return self.build_id


class HealthCheck(_Strict):
    """Machine-checkable monitoring baseline. Integer thresholds: the canonical form has no floats."""

    metric: Identifier
    op: Literal["GTE", "LTE"]
    threshold: int
    unit: NonEmpty
    # Domain scenarios (C1) this check watches in production. On-call support uses it to say
    # which decided behavior an alert puts at risk; the gate checks the ids exist in C1.
    protects: List[Identifier] = []

    def holds(self, value: int) -> bool:
        return value >= self.threshold if self.op == "GTE" else value <= self.threshold


class ReleaseRecord(_Contract):
    release_id: Identifier
    tag: NonEmpty
    build_id: Identifier
    source_revision: Revision  # the exact independently tested commit
    build_digest: Digest
    documentation: ArtifactRef
    deployment_target: NonEmpty
    canary_plan: NonEmpty
    rollback_plan: NonEmpty
    # M4 release provenance (required by policy when require_release_provenance is on).
    merged_revision: Optional[Revision] = None  # main revision the artifact was built from
    artifact_digest: Optional[Digest] = None
    provenance_id: Optional[Identifier] = None
    health_checks: List[HealthCheck] = []

    def references(self) -> Dict[str, ArtifactRef]:
        return {"DOCS": self.documentation}

    def build_reference(self) -> Optional[str]:
        return self.build_id


CONTRACT_MODELS = {
    ContractType.C0: C0BusinessIntent,
    ContractType.C1: C1BusinessRequirement,
    ContractType.C2: C2TechnicalRequirement,
    ContractType.STORIES: StoryPlan,
    ContractType.C3: C3AcceptanceManifest,
    ContractType.DOCS: ReleaseDocumentation,
    ContractType.RELEASE: ReleaseRecord,
}


# --- Machine evidence ----------------------------------------------------------


class _StrictEvidence(BaseModel):
    # Strict: the signed bytes must be exactly what was validated, no coercion.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TestResult(_StrictEvidence):
    __test__ = False

    test_id: Identifier
    test_digest: Digest  # digest of the test file the runner actually executed
    outcome: Literal["PASSED", "FAILED", "ERROR", "SKIPPED"]
    # Project modules the test file imports, as the runner parsed them; "<dynamic>" marks
    # an import the runner cannot resolve statically. Required, so an omission cannot pass.
    project_imports: List[NonEmpty]


class AttestationPayload(_StrictEvidence):
    attestation_id: Identifier
    runner_id: Identifier
    build_id: Identifier
    source_revision: Revision
    build_digest: Digest
    manifest_id: Identifier
    manifest_version: int = Field(ge=1)
    manifest_digest: Digest
    policy_version: NonEmpty
    issued_at: NonEmpty
    stack_digest: Digest  # the stack profile the runner actually executed
    results: List[TestResult] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_results(self) -> "AttestationPayload":
        ids = [r.test_id for r in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate test results in attestation")
        return self


class TestDigest(_StrictEvidence):
    __test__ = False

    test_id: Identifier
    test_digest: Digest


class QualityCheck(_StrictEvidence):
    check_id: Identifier
    kind: Literal["COVERAGE_TESTS", "MUTATION", "SECRET_SCAN", "DEPENDENCY_SCAN"]
    # MUTATION reports INFO: the runner measures, the gate's policy judges the score.
    # NOT_RUN: this stack has no tool for the check; only a named waiver in C2 accepts that.
    status: Literal["PASS", "FAIL", "INFO", "NOT_RUN"]
    summary: NonEmpty
    metrics: Dict[str, int] = {}
    findings: List[NonEmpty] = []


class QualityAttestationPayload(_StrictEvidence):
    """Stage 7 evidence. May run on a later revision than the build if only tests differ:
    the recomputed build digest must still equal the registered one."""

    attestation_id: Identifier
    runner_id: Identifier
    build_id: Identifier
    source_revision: Revision
    build_digest: Digest
    manifest_id: Identifier
    manifest_version: int = Field(ge=1)
    manifest_digest: Digest
    uat_digests: List[TestDigest] = Field(min_length=1)
    policy_version: NonEmpty
    issued_at: NonEmpty
    stack_digest: Digest
    checks: List[QualityCheck] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_checks(self) -> "QualityAttestationPayload":
        kinds = [c.kind for c in self.checks]
        if len(kinds) != len(set(kinds)):
            raise ValueError("one check per kind")
        return self


class Material(_StrictEvidence):
    uri: NonEmpty
    digest: Digest


class ArtifactDescriptor(_StrictEvidence):
    name: NonEmpty
    digest: Digest
    size: int = Field(ge=0)


class ProvenancePayload(_StrictEvidence):
    """SLSA-style build provenance: which builder made which artifact from which source."""

    provenance_id: Identifier
    builder_id: Identifier
    build_id: Identifier
    source_revision: Revision
    build_digest: Digest
    artifact: ArtifactDescriptor
    materials: List[Material] = Field(min_length=1)
    policy_version: NonEmpty
    issued_at: NonEmpty


class ReviewFinding(_Strict):
    finding_id: Identifier
    severity: Literal["BLOCKING", "ADVISORY"]
    summary: NonEmpty
    status: Literal["OPEN", "RESOLVED", "RISK_ACCEPTED"]
    risk_owner: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def _risk_owner(self) -> "ReviewFinding":
        if self.status == "RISK_ACCEPTED" and self.risk_owner is None:
            raise ValueError(f"{self.finding_id}: a risk-accepted finding needs a named risk owner")
        return self


# --- Incidents (Template K, milestone M5) ------------------------------------------

CauseClass = Literal["RELEASE_REGRESSION", "CONFIG_CHANGE", "PROVIDER_OUTAGE", "CAPACITY", "UNKNOWN"]
READ_ONLY_DIAGNOSTICS = (
    "query_metrics", "search_logs", "compare_release_manifest", "open_runbook",
    "inspect_trace", "check_provider_status", "diff_configuration",
)


class CitedStatement(_Strict):
    statement: NonEmpty
    evidence: List[NonEmpty] = Field(min_length=1)


class RecentChange(_Strict):
    change_id: NonEmpty
    deployed_at_minute: int
    environment: NonEmpty
    kind: Literal["DEPLOYMENT", "CONFIGURATION"] = "DEPLOYMENT"
    # Filled by trusted code: did this change reach production through a gated release?
    # A configuration change, or a deployment with no gated release record, did not.
    through_gated_release: Optional[bool] = None
    linked_evidence: Dict[str, str] = {}
    evidence: List[NonEmpty] = Field(min_length=1)


class RelatedConversation(_Strict):
    """A prior operational thread, cited as history. Chat text is data, never instructions."""

    thread_id: NonEmpty
    channel: NonEmpty
    note: NonEmpty
    evidence: List[NonEmpty] = Field(min_length=1)


class ScenarioAtRisk(_Strict):
    scenario_id: Identifier
    rule: NonEmpty
    behavior: NonEmpty  # the owner's Given/When/Then, verbatim
    decided_by: Identifier
    tests: List[Identifier] = []
    health_check: NonEmpty


class ReleaseCheck(_Strict):
    """What the runtime platform says is running, compared with the gated release record (trusted code)."""

    status: Literal["MATCH", "MISMATCH", "NOT_GATED", "UNAVAILABLE"]
    running_release: Optional[NonEmpty] = None
    running_tag: Optional[NonEmpty] = None
    running_commit: Optional[NonEmpty] = None
    running_artifact: Optional[NonEmpty] = None
    gated_release: Optional[NonEmpty] = None
    detail: NonEmpty
    scenarios_at_risk: List[ScenarioAtRisk] = []


class MatchingIncident(_Strict):
    incident_id: NonEmpty
    similarity_note: NonEmpty
    evidence: List[NonEmpty] = Field(min_length=1)


class RunbookEvidence(_Strict):
    evidence_id: NonEmpty
    guidance: NonEmpty


class Hypothesis(_Strict):
    """A ranked working hypothesis - never a root-cause assertion."""

    hypothesis_id: Identifier
    rank: int = Field(ge=1)
    cause_class: CauseClass
    subject: NonEmpty
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    statement: NonEmpty
    supporting: List[NonEmpty] = Field(min_length=1)
    contradicting: List[NonEmpty] = []
    how_to_confirm: NonEmpty


class DiagnosticStep(_Strict):
    step_id: Identifier
    action: Literal[READ_ONLY_DIAGNOSTICS]  # type: ignore[valid-type]
    target: NonEmpty
    rationale: NonEmpty


class IncidentPacket(_Strict):
    incident_id: Identifier
    alert_source: NonEmpty
    situation: Optional[CitedStatement] = None
    observed_symptoms: List[CitedStatement] = Field(min_length=1)
    recent_changes: List[RecentChange] = []
    matching_incidents: List[MatchingIncident] = []
    related_conversations: List[RelatedConversation] = []
    runbook_evidence: List[RunbookEvidence] = []
    working_hypotheses: List[Hypothesis] = Field(min_length=1)
    conflicting_signals: List[CitedStatement] = []
    first_diagnostic_steps: List[DiagnosticStep] = Field(min_length=1)
    unknowns: List[NonEmpty] = []
    escalation_owner: NonEmpty
    restricted_actions: List[NonEmpty]
    postmortem_updates: List[NonEmpty] = []
    # Trusted enrichment, never model output: when the investigation ran, how old each
    # cited piece of evidence was at that moment, and the gated-release cross-check.
    investigated_at_minute: Optional[int] = None
    evidence_age_minutes: Dict[str, int] = {}
    release_check: Optional[ReleaseCheck] = None

    @model_validator(mode="after")
    def _ranks(self) -> "IncidentPacket":
        ranks = sorted(h.rank for h in self.working_hypotheses)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("hypothesis ranks must be 1..n without gaps or ties")
        return self


class IncidentDecision(_Strict):
    # The exact packet the responder was shown; a decision on an older packet is refused.
    packet_digest: Digest
    outcome: Literal["FALSE_OR_NOISY", "ISSUE_UNDERSTOOD", "NEEDS_DEEPER_ACTION"]
    hypothesis_id: Optional[Identifier] = None
    diagnostic_steps_taken: List[Identifier] = []
    action: Literal["NONE", "ROLLBACK", "FAILOVER", "SCALE"]
    rationale: NonEmpty

    @model_validator(mode="after")
    def _coherent(self) -> "IncidentDecision":
        if self.outcome == "FALSE_OR_NOISY" and self.action != "NONE":
            raise ValueError("a false or noisy alert takes no production action")
        if self.outcome != "FALSE_OR_NOISY" and self.hypothesis_id is None:
            raise ValueError("name the hypothesis the decision rests on")
        return self


class DeterminedCause(_Strict):
    cause_class: CauseClass
    subject: NonEmpty


class IncidentResolution(_Strict):
    determined_cause: DeterminedCause  # decided by humans, after diagnosis
    minutes_to_restore: int = Field(ge=0)
    summary: NonEmpty


class Postmortem(_Strict):
    summary: NonEmpty
    contributing_factors: List[NonEmpty] = Field(min_length=1)
    linked_release: Optional[Identifier] = None
    action_items: List[NonEmpty] = Field(min_length=1)
    runbook_updates: List[NonEmpty] = []
