"""Deterministic stand-ins for the triage model (milestone M5 study harness).

``ReferenceTriageModel`` is a transparent rule-based reasoner over the evidence
the agent retrieved. It exists so the study can vary *evidence availability*
(release provenance, runbooks) while holding reasoning fixed - exactly the
separation section 12.2 asks for. It is not an LLM and its accuracy says
nothing about any LLM; run the same harness with ClaudeModel for that.

``NaiveTriageModel`` is the "blame the latest deploy" heuristic baseline.
"""

import re
import statistics
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from .agents import parse_model_output

RUNBOOK_KEYWORDS = {"RELEASE_REGRESSION": "after a release", "CONFIG_CHANGE": "configuration",
                    "PROVIDER_OUTAGE": "provider", "CAPACITY": "queue"}
CONFIRM = {
    "RELEASE_REGRESSION": "Compare error signatures before and after the deployment against the release manifest",
    "CONFIG_CHANGE": "Diff the configuration before and after the change and check it against the runbook's safe values",
    "PROVIDER_OUTAGE": "Check the provider status page and send-latency for the provider",
    "CAPACITY": "Compare request rate and queue depth with the previous week",
    "UNKNOWN": "Widen the log search window and inspect traces",
}


def changepoint(series: List[int], kind: str) -> Optional[int]:
    """First minute where the metric leaves its baseline for two consecutive minutes."""
    if len(series) < 22:
        return None
    base = statistics.median(series[:20])
    tests = {
        "down": lambda v: v < base - max(1, base * 0.02),
        "up3": lambda v: v > 3 * max(base, 1),
        "up5": lambda v: v > max(50, 5 * base),
        "up2": lambda v: v > 2 * base,
    }[kind]
    for t in range(20, len(series) - 1):
        if tests(series[t]) and tests(series[t + 1]):
            return t
    return None


def _kinds(ids: List[str]) -> int:
    return len({i.split(":", 1)[0] for i in ids})


class ReferenceTriageModel:
    model_id = "reference-triage-v1"

    def generate(self, role: str, task: str, inputs: Dict[str, Any], output_model: Type[BaseModel]) -> BaseModel:
        return parse_model_output(output_model, self.triage(inputs))

    def triage(self, x: Dict[str, Any]) -> Dict[str, Any]:
        alert, metrics, logs = x["alert"], x["metrics"], x["logs"]
        m_alert = metrics[alert["metric"]]
        onset = changepoint(m_alert["series"], "down") or alert["fired_at_minute"] - 3
        provider_cp = changepoint(metrics["provider_error_bp"]["series"], "up5")
        rate_cp = changepoint(metrics["request_rate"]["series"], "up2")
        near = lambda t: t is not None and abs(t - onset) <= 3  # noqa: E731
        app_errors = [l for l in logs if l["component"] == "notifications-app" and l["level"] == "ERROR"
                      and l["minute"] >= onset - 2]
        provider_logs = [l for l in logs if l["component"] == "smtp-provider" and l["minute"] >= onset - 2]
        queue_logs = [l for l in logs if l["component"] == "delivery-queue" and l["minute"] >= onset - 2]
        provider_signal = near(provider_cp) or bool(provider_logs)
        configs = x.get("config_changes")

        candidates: List[Dict[str, Any]] = []
        for c in configs or []:
            if onset - 15 <= c["minute"] <= onset:
                linked = [l["evidence_id"] for l in logs if l["attrs"].get("config_key") == c["key"]
                          and l["minute"] >= c["minute"]]
                candidates.append(self._hyp("CONFIG_CHANGE", c["key"], 1 + (2 if linked else 0),
                                            [c["evidence_id"], m_alert["evidence_id"], *linked[:3]], [],
                                            f"Degradation began {onset - c['minute']} min after {c['key']} was changed"))
        if x["deployments"] is not None:
            for d in x["deployments"]:
                if onset - 15 <= d["minute"] <= onset:
                    linked = [l["evidence_id"] for l in app_errors
                              if l["attrs"].get("artifact") == d["artifact_digest"][7:19] and l["minute"] >= d["minute"]]
                    contra = ([l["evidence_id"] for l in provider_logs[:3]] +
                              ([metrics["request_rate"]["evidence_id"]] if near(rate_cp) else []))
                    score = 1 + (2 if linked else 0) - (2 if contra and not linked else 0)
                    candidates.append(self._hyp("RELEASE_REGRESSION", d["release_id"], score,
                                                [d["evidence_id"], m_alert["evidence_id"], *linked[:3]], contra,
                                                f"Degradation began {onset - d['minute']} min after {d['release_id']} was deployed"))
        elif app_errors and not any(h["cause_class"] == "CONFIG_CHANGE" for h in candidates):
            artifact = app_errors[0]["attrs"].get("artifact", "unknown")
            candidates.append(self._hyp("RELEASE_REGRESSION", f"unidentified artifact {artifact}", 2,
                                        [m_alert["evidence_id"], *[l["evidence_id"] for l in app_errors[:3]]], [],
                                        "New application errors appeared with the degradation; the release is not identifiable"))
        if provider_signal:
            providers = {l["attrs"].get("provider") for l in provider_logs} - {None}
            support = [metrics["provider_error_bp"]["evidence_id"]] if near(provider_cp) else []
            support += [l["evidence_id"] for l in provider_logs[:3]]
            candidates.append(self._hyp("PROVIDER_OUTAGE", sorted(providers)[0] if providers else "email provider",
                                        1 + _kinds(support), support, [],
                                        "Provider errors rose together with the delivery drop"))
        if near(rate_cp):
            support = [metrics["request_rate"]["evidence_id"], *[l["evidence_id"] for l in queue_logs[:3]]]
            candidates.append(self._hyp("CAPACITY", "traffic", 1 + _kinds(support), support, [],
                                        "Request rate rose sharply at the onset"))
        if not candidates:
            candidates.append(self._hyp("UNKNOWN", "unexplained degradation", 0, [m_alert["evidence_id"]], [],
                                        "No correlated change, provider or traffic signal was found"))
        candidates.sort(key=lambda h: -h["_score"])
        for rank, h in enumerate(candidates, 1):
            h["rank"], h["hypothesis_id"] = rank, f"H{rank}"
            supporting_kinds = _kinds(h["supporting"])
            h["confidence"] = ("HIGH" if supporting_kinds >= 2 and not h["contradicting"] and rank == 1
                               else "MEDIUM" if supporting_kinds >= 2 else "LOW")
            del h["_score"]

        runbook = x["runbook_sections"] or []
        top = candidates[0]
        sections = {c: next((s for s in runbook if RUNBOOK_KEYWORDS.get(c, "~") in s["title"].lower()), None)
                    for c in {h["cause_class"] for h in candidates[:2]}}
        steps = []
        for h in candidates[:2]:
            section = sections.get(h["cause_class"])
            m = re.search(r"First diagnostic step: (\w+) (\S+)", section["text"]) if section else None
            if m:
                steps.append({"step_id": f"S{len(steps) + 1}", "action": m.group(1), "target": m.group(2),
                              "rationale": f"Runbook step for {h['hypothesis_id']} ({section['title']})"})
        if not steps:
            steps.append({"step_id": "S1", "action": "search_logs", "target": alert["service"],
                          "rationale": "No runbook guidance retrieved; start from service logs"})

        recent = [{"change_id": d["release_id"], "deployed_at_minute": d["minute"], "environment": d["environment"],
                   "evidence": [d["evidence_id"]] + ([x["releases"][d["release_id"]]["evidence_id"]]
                                                     if x["releases"] and d["release_id"] in x["releases"] else [])}
                  for d in (x["deployments"] or []) if onset - 60 <= d["minute"] <= onset]
        recent += [{"change_id": c["change_id"], "deployed_at_minute": c["minute"], "environment": c["environment"],
                    "kind": "CONFIGURATION", "evidence": [c["evidence_id"]]}
                   for c in (configs or []) if onset - 60 <= c["minute"] <= onset]
        recent.sort(key=lambda r: r["deployed_at_minute"])
        # Competing explanations the responder should weigh, not hide.
        conflicts = [{"statement": f"Competing signal ({h['hypothesis_id']}): {h['statement']}",
                      "evidence": h["supporting"][:3]}
                     for h in candidates[1:] if _kinds(h["supporting"]) >= 2]
        conflicts += [{"statement": f"Evidence against {top['hypothesis_id']}: {len(top['contradicting'])} item(s)",
                       "evidence": top["contradicting"][:3]}] if top["contradicting"] else []
        # Prior threads are history: cited for context, never followed and never used to rank.
        threads = [{"thread_id": c["thread_id"], "channel": c["channel"],
                    "note": f"earlier thread on a similar {top['cause_class'].lower().replace('_', ' ')} issue",
                    "evidence": [c["evidence_id"]]}
                   for c in (x.get("conversations") or []) if top["cause_class"] in c.get("topics", [])]
        situation = {"statement": f"{alert['metric']} for {alert['service']} in {alert['environment']} "
                                  f"({alert['severity']}) has been below baseline since minute {onset}; "
                                  f"leading hypothesis: {top['statement']} ({top['confidence']} confidence)",
                     "evidence": [alert["evidence_id"], m_alert["evidence_id"]]}
        symptoms = [{"statement": f"{alert['metric']} fell below baseline from minute {onset}",
                     "evidence": [alert["evidence_id"], m_alert["evidence_id"]]}]
        if changepoint(metrics["error_rate_bp"]["series"], "up3") is not None:
            symptoms.append({"statement": "error_rate_bp rose above three times its baseline",
                             "evidence": [metrics["error_rate_bp"]["evidence_id"]]})
        return {
            "situation": situation,
            "observed_symptoms": symptoms,
            "recent_changes": recent,
            "related_conversations": threads,
            "conflicting_signals": conflicts,
            "matching_incidents": [{"incident_id": i["incident_id"], "similarity_note": i["summary"],
                                    "evidence": [i["evidence_id"]]}
                                   for i in (x["incidents"] or []) if top["cause_class"] in i["tags"]],
            "runbook_evidence": [{"evidence_id": s["evidence_id"], "guidance": s["text"].splitlines()[0]}
                                 for s in sections.values() if s],
            "working_hypotheses": candidates,
            "first_diagnostic_steps": steps,
            "unknowns": list(x["unknowns"]) + ["no traces were retrieved"],
            "postmortem_updates": [] if sections.get(top["cause_class"]) else
            [f"add a runbook section for {top['cause_class'].lower().replace('_', ' ')}"],
        }

    @staticmethod
    def _hyp(cause: str, subject: str, score: float, supporting: List[str], contradicting: List[str],
             statement: str) -> Dict[str, Any]:
        return {"cause_class": cause, "subject": subject, "_score": score, "statement": statement,
                "supporting": supporting, "contradicting": contradicting, "how_to_confirm": CONFIRM[cause]}


class NaiveTriageModel:
    """Baseline: whatever deployed last is to blame."""

    model_id = "naive-latest-deploy"

    def generate(self, role: str, task: str, inputs: Dict[str, Any], output_model: Type[BaseModel]) -> BaseModel:
        alert, metric = inputs["alert"], inputs["metrics"][inputs["alert"]["metric"]]
        deployments = sorted(inputs["deployments"] or [], key=lambda d: d["minute"])
        if deployments:
            d = deployments[-1]
            hyp = {"hypothesis_id": "H1", "rank": 1, "cause_class": "RELEASE_REGRESSION", "subject": d["release_id"],
                   "confidence": "HIGH", "statement": f"{d['release_id']} was the most recent deployment",
                   "supporting": [d["evidence_id"], metric["evidence_id"]], "contradicting": [],
                   "how_to_confirm": CONFIRM["RELEASE_REGRESSION"]}
        else:
            hyp = {"hypothesis_id": "H1", "rank": 1, "cause_class": "UNKNOWN", "subject": "unexplained degradation",
                   "confidence": "LOW", "statement": "No deployment information", "supporting": [metric["evidence_id"]],
                   "contradicting": [], "how_to_confirm": CONFIRM["UNKNOWN"]}
        return parse_model_output(output_model, {
            "observed_symptoms": [{"statement": f"{alert['metric']} alert fired",
                                   "evidence": [alert["evidence_id"], metric["evidence_id"]]}],
            "working_hypotheses": [hyp],
            "first_diagnostic_steps": [{"step_id": "S1", "action": "search_logs", "target": alert["service"],
                                        "rationale": "generic first step"}],
        })
