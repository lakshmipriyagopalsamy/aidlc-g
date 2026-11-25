"""M5 study harness: grounded incident packets in reproducible simulations (sections 12.2, 14, 24).

Design:
* Ground truth (cause class + subject) is fixed by the simulator before triage.
* Conditions isolate evidence sources while holding the reasoner fixed:
  full | no_release_evidence | no_runbooks, plus a naive "blame the latest deploy" baseline.
* Scores are computed against the predeclared truth, never edited after a run.
* "Simulated time to restore" is a stated cost model (parameters below), not measured human time.

The default reasoner is deterministic and rule-based. Results characterise how
evidence availability changes triage under that reasoner and validate the
harness; they are not evidence about LLM capability. Pass ``model=ClaudeModel()``
to run the same protocol against a live model.
"""

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .agents import Model, ModelError
from .errors import GovernanceError
from .ops import EXPECTED_FIRST_STEP, LABELS, OPS_TOOLS, OperationalSupportAgent, OpsStore, Scenario, ops_broker, simulate
from .orchestrator import SliceEnvironment, SliceOrchestrator
from .triage import NaiveTriageModel, ReferenceTriageModel

# Stated cost model for simulated time to restore (minutes). Change these and the conclusions may change.
BASE_MINUTES = {"RELEASE_REGRESSION": 20, "CONFIG_CHANGE": 20, "PROVIDER_OUTAGE": 35, "CAPACITY": 30}
FALSE_LEAD_MINUTES = 25
UNHELPFUL_FIRST_STEP_MINUTES = 10

CONDITIONS: Dict[str, Tuple[Callable[[], Model], frozenset]] = {
    "full": (ReferenceTriageModel, OPS_TOOLS),
    "no_release_evidence": (ReferenceTriageModel, OPS_TOOLS - {"ops.deployments", "gate.release"}),
    "no_runbooks": (ReferenceTriageModel, OPS_TOOLS - {"repo.read"}),
    "no_config_history": (ReferenceTriageModel, OPS_TOOLS - {"ops.config_changes"}),
    "no_conversation_history": (ReferenceTriageModel, OPS_TOOLS - {"ops.conversations"}),
    "naive_baseline": (NaiveTriageModel, OPS_TOOLS),
}


@dataclass
class ScenarioScore:
    scenario_id: str
    label: str
    condition: str
    rejected: Optional[str]
    top1_correct: bool
    correct_rank: Optional[int]
    false_leads: int
    first_step_useful: bool
    change_assessed_correctly: bool
    review_items: int
    review_words: int
    sim_minutes_to_restore: int


def _words(packet: Dict[str, Any]) -> int:
    texts = [s["statement"] for s in packet["observed_symptoms"]] + \
            [h["statement"] + " " + h["how_to_confirm"] for h in packet["working_hypotheses"]] + \
            [s["rationale"] for s in packet["first_diagnostic_steps"]] + \
            [r["guidance"] for r in packet["runbook_evidence"]] + \
            [m["similarity_note"] for m in packet["matching_incidents"]] + packet["unknowns"] + \
            [c["note"] for c in packet.get("related_conversations", [])] + \
            [c["statement"] for c in packet.get("conflicting_signals", [])] + \
            ([packet["situation"]["statement"]] if packet.get("situation") else [])
    return sum(len(t.split()) for t in texts)


def score(packet: Optional[Dict[str, Any]], rejected: Optional[str], scenario: Scenario, condition: str) -> ScenarioScore:
    truth = scenario.truth
    if packet is None:
        n = 0
        return ScenarioScore(scenario.scenario_id, truth.label, condition, rejected, False, None, n, False, False, 0, 0,
                             BASE_MINUTES[truth.cause_class] + FALSE_LEAD_MINUTES + UNHELPFUL_FIRST_STEP_MINUTES)
    hyps = sorted(packet["working_hypotheses"], key=lambda h: h["rank"])
    rank = next((h["rank"] for h in hyps if (h["cause_class"], h["subject"]) == (truth.cause_class, truth.subject)), None)
    false_leads = (rank - 1) if rank else len(hyps)
    useful = packet["first_diagnostic_steps"][0]["action"] == EXPECTED_FIRST_STEP[truth.cause_class]
    if truth.cause_class == "RELEASE_REGRESSION":
        change_ok = rank == 1 and any(c["change_id"] == truth.subject for c in packet["recent_changes"])
    else:
        change_ok = hyps[0]["cause_class"] != "RELEASE_REGRESSION"
    items = sum(len(packet.get(k, [])) for k in ("observed_symptoms", "recent_changes", "matching_incidents",
                                                 "related_conversations", "runbook_evidence", "working_hypotheses",
                                                 "conflicting_signals", "first_diagnostic_steps", "unknowns"))
    minutes = BASE_MINUTES[truth.cause_class] + FALSE_LEAD_MINUTES * false_leads + \
        (0 if useful else UNHELPFUL_FIRST_STEP_MINUTES)
    return ScenarioScore(scenario.scenario_id, truth.label, condition, None, rank == 1, rank, false_leads, useful,
                         change_ok, items, _words(packet), minutes)


def wilson(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class StudyResult:
    params: Dict[str, Any]
    scores: List[ScenarioScore] = field(default_factory=list)

    def summary(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for cond in self.params["conditions"]:
            for label in [*LABELS, "ALL"]:
                rows = [s for s in self.scores if s.condition == cond and (label == "ALL" or s.label == label)]
                if not rows:
                    continue
                n, k = len(rows), sum(s.top1_correct for s in rows)
                out.setdefault(cond, {})[label] = {
                    "n": n, "top1_correct": k, "top1_accuracy": k / n, "top1_ci95": wilson(k, n),
                    "mean_false_leads": statistics.mean(s.false_leads for s in rows),
                    "first_step_useful_rate": sum(s.first_step_useful for s in rows) / n,
                    "change_assessed_rate": sum(s.change_assessed_correctly for s in rows) / n,
                    "mean_review_items": statistics.mean(s.review_items for s in rows),
                    "mean_review_words": statistics.mean(s.review_words for s in rows),
                    "median_sim_minutes_to_restore": statistics.median(s.sim_minutes_to_restore for s in rows),
                    "rejected_packets": sum(1 for s in rows if s.rejected),
                }
        return out


def released_environment() -> Tuple[SliceEnvironment, Dict[str, Any]]:
    """Run the full M2-M4 slice once; incidents then happen against its real release."""
    from .examples import notifications as ex

    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
    report = SliceOrchestrator(env, ex.model(), ex.desk()).release(ex.REQUEST)
    if not report.completed:
        raise RuntimeError(f"release slice did not complete: {report.halted}")
    m = report.release_manifest
    return env, {"release_id": m["release_id"], "artifact_digest": m["artifact_digest"],
                 "work_item_id": report.work_item_id, "tag": m["tag"], "git_commit": m["merged_revision"]}


def run_study(per_class: int = 10, seed: int = 2026, conditions: Optional[List[str]] = None,
              model: Optional[Model] = None, env: Optional[SliceEnvironment] = None,
              release: Optional[Dict[str, Any]] = None, run_tag: str = "") -> StudyResult:
    if env is None or release is None:
        env, release = released_environment()
    names = conditions or list(CONDITIONS)
    result = StudyResult({"per_class": per_class, "seed": seed, "conditions": names, "labels": list(LABELS),
                          "base_minutes": BASE_MINUTES, "false_lead_minutes": FALSE_LEAD_MINUTES,
                          "unhelpful_first_step_minutes": UNHELPFUL_FIRST_STEP_MINUTES,
                          "model": (model.model_id if model else "per-condition default")})
    for label in LABELS:
        for i in range(per_class):
            scenario = simulate(label, seed * 1000 + i, release, env.baseline_digest)
            for cond in names:
                factory, tools = CONDITIONS[cond]
                store = OpsStore(env.gate, scenario)
                broker = ops_broker(tools, "ops_agent", env.tokens["ops_agent"], env.gate, env.workspace, store)
                agent = OperationalSupportAgent(model or factory(), broker)
                incident_id = f"INC{run_tag}-{cond}-{scenario.scenario_id}".replace("_", "-")
                try:
                    packet, rejected = agent.triage(incident_id), None
                except ModelError as exc:
                    packet, rejected = None, exc.code
                result.scores.append(score(packet, rejected, scenario, cond))
    return result


def incident_drill(env: SliceEnvironment, release: Dict[str, Any], seed: int = 7) -> Dict[str, Any]:
    """Section 12.1 end to end on the released slice: packet -> human decision -> rollback -> resolution -> postmortem."""
    scenario = simulate("RELEASE_REGRESSION", seed, release, env.baseline_digest)
    incident_id = f"INC-DRILL-{seed}"
    store = OpsStore(env.gate, scenario)
    agent = OperationalSupportAgent(ReferenceTriageModel(),
                                    ops_broker(OPS_TOOLS, "ops_agent", env.tokens["ops_agent"], env.gate, env.workspace, store))
    packet = agent.triage(incident_id)
    top = sorted(packet["working_hypotheses"], key=lambda h: h["rank"])[0]
    sre, gate = env.tokens["sre"], env.gate
    action = "ROLLBACK" if top["cause_class"] == "RELEASE_REGRESSION" else "NONE"
    shown = [e for e in gate.get_incident(sre, incident_id)["events"] if e["event"] == "PACKET"][-1]
    gate.record_incident(sre, incident_id, "DECISION", {
        "packet_digest": shown["payload_digest"], "outcome": "NEEDS_DEEPER_ACTION" if action != "NONE"
        else "ISSUE_UNDERSTOOD", "hypothesis_id": top["hypothesis_id"], "diagnostic_steps_taken": [packet["first_diagnostic_steps"][0]["step_id"]],
        "action": action, "rationale": f"On-call accepted {top['hypothesis_id']} after {packet['first_diagnostic_steps'][0]['action']}"})
    rollback = None
    if action == "ROLLBACK":
        decision = gate.evaluate_incident_rollback(sre, incident_id, "production", f"{incident_id}:rollback")
        if decision["decision"] == "ALLOW":
            rollback = env.deployer.execute_rollback(decision)
    gate.record_incident(sre, incident_id, "RESOLVED", {
        "determined_cause": {"cause_class": scenario.truth.cause_class, "subject": scenario.truth.subject},
        "minutes_to_restore": 18, "summary": "Rolled back to the previous artifact; delivery recovered"})
    gate.record_incident(env.tokens["eng_lead"], incident_id, "POSTMORTEM", {
        "summary": "Category casing regression in should_deliver reached production",
        "contributing_factors": ["no acceptance test for mixed-case categories", "canary traffic lacked the case"],
        "linked_release": release["release_id"],
        "action_items": ["add a mixed-case category acceptance test (new C3 version)", "add the case to canary traffic"],
        "runbook_updates": packet["postmortem_updates"] or ["none"]})
    return {"incident_id": incident_id, "packet": packet, "rollback": rollback,
            "record": gate.get_incident(sre, incident_id),
            "production_now": gate.get_environment(sre, "production")["current_digest"]}


def to_markdown(result: StudyResult, drill: Optional[Dict[str, Any]] = None) -> str:
    p, summary = result.params, result.summary()
    lines = ["# AIDLC-G M5 incident-support study", "",
             f"{p['per_class']} scenarios per cause class × {len(p['labels'])} classes × {len(p['conditions'])} conditions; "
             f"seed {p['seed']}; reasoner: {p['model']}.", "",
             "> Synthetic, seeded simulations with predeclared ground truth. The default reasoner is a deterministic "
             "rule-based stand-in: these results show how evidence availability changes triage under a fixed reasoner "
             "and validate the harness. They are not evidence about LLM capability or real MTTR (§12.2). "
             "The simulator and the reference reasoner share an author, so high full-condition accuracy is by "
             "construction; the informative signal is the difference between conditions. "
             f"Simulated time to restore = base by class {p['base_minutes']} + {p['false_lead_minutes']} per false lead "
             f"+ {p['unhelpful_first_step_minutes']} if the first step is not the runbook's step.", "",
             "## All classes", "",
             "| Condition | Top-1 correct (95% CI) | False leads | First step useful | Change assessed | "
             "Review items / words | Sim. restore (median min) | Rejected |",
             "|---|---|---|---|---|---|---|---|"]
    for cond, rows in summary.items():
        a = rows["ALL"]
        lo, hi = a["top1_ci95"]
        lines.append(f"| {cond} | {a['top1_correct']}/{a['n']} = {a['top1_accuracy']:.2f} ({lo:.2f}–{hi:.2f}) | "
                     f"{a['mean_false_leads']:.2f} | {a['first_step_useful_rate']:.2f} | {a['change_assessed_rate']:.2f} | "
                     f"{a['mean_review_items']:.1f} / {a['mean_review_words']:.0f} | {a['median_sim_minutes_to_restore']:.0f} | "
                     f"{a['rejected_packets']} |")
    lines += ["", "## Top-1 accuracy by cause class", "", "| Condition | " + " | ".join(p["labels"]) + " |",
              "|---|" + "---|" * len(p["labels"])]
    for cond, rows in summary.items():
        lines.append(f"| {cond} | " + " | ".join(f"{rows[l]['top1_correct']}/{rows[l]['n']}" for l in p["labels"]) + " |")
    if drill:
        events = [e["event"] for e in drill["record"]["events"]]
        lines += ["", "## Incident drill on the released slice", "",
                  f"- Incident `{drill['incident_id']}`: {' → '.join(events)}",
                  f"- Top hypothesis: {drill['packet']['working_hypotheses'][0]['statement']} "
                  f"({drill['packet']['working_hypotheses'][0]['confidence']})",
                  f"- First step: {drill['packet']['first_diagnostic_steps'][0]['action']} "
                  f"{drill['packet']['first_diagnostic_steps'][0]['target']}",
                  f"- Rollback: {drill['rollback'].outcome if drill['rollback'] else 'not taken'}; production now runs "
                  f"`{drill['production_now'][7:19]}`",
                  f"- Packet recorded by `{drill['record']['events'][0]['recorded_by']}`; decision, resolution and "
                  f"postmortem by humans ({', '.join(e['recorded_by'] for e in drill['record']['events'][1:])})"]
    return "\n".join(lines) + "\n"


def to_json(result: StudyResult) -> Dict[str, Any]:
    return {"params": result.params, "summary": result.summary(), "scores": [asdict(s) for s in result.scores]}
