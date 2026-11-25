"""Decision-latency experiment: the same change at each risk tier, with simulated human response time.

The gate's clock is simulated. Every packet sent to people waits a fixed time
before it is answered; approvals of the same artifact version go out together
and are answered in parallel. That is a stated assumption, not a measurement:
the experiment shows how the number and placement of human decisions change
with the tier, and the gate's decision_latency report computes the proxies.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .examples import notifications as ex
from .orchestrator import ScriptedDesk, SliceEnvironment, SliceOrchestrator, SliceReport

START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class SimClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> str:
        return self.now.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


class TimedDesk:
    """Wraps a desk: each human step waits `minutes` on the simulated clock; parallel per artifact version."""

    def __init__(self, desk: Any, clock: SimClock, minutes: int = 120) -> None:
        self._desk, self._clock, self._minutes = desk, clock, minutes
        self._asked: set = set()
        # The decision schedule: one entry per human wait, in order, with how many people answer it in parallel.
        self.schedule: List[Dict[str, Any]] = []

    def _wait(self, key: Any) -> None:
        if key not in self._asked:
            self._asked.add(key)
            self._clock.advance(self._minutes)
            self.schedule.append({"step": "/".join(str(k) for k in key), "people": 1})
        else:
            self.schedule[-1 if self.schedule and self.schedule[-1]["step"] == "/".join(str(k) for k in key)
                          else next(i for i, e in enumerate(self.schedule)
                                    if e["step"] == "/".join(str(k) for k in key))]["people"] += 1

    def answer(self, principal_id: str, questions: List[Dict[str, Any]]) -> Dict[str, str]:
        self._wait(("answer",))
        return self._desk.answer(principal_id, questions)

    def define_scenarios(self, principal_id: str, candidates: List[Dict[str, Any]], answers: Dict[str, str]):
        return self._desk.define_scenarios(principal_id, candidates, answers)  # same sitting as the answers

    def declare_risk(self, principal_id: str, proposal: Dict[str, Any]):
        return self._desk.declare_risk(principal_id, proposal)

    def choose_stack(self, principal_id: str, proposal: Dict[str, Any]):
        self._wait(("stack",))
        return self._desk.choose_stack(principal_id, proposal)

    def decide(self, principal_id: str, role: str, packet: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        self._wait(("decide", packet["artifact_id"], packet["version"], bool(packet.get("why_now"))))
        return self._desk.decide(principal_id, role, packet)

    def review(self, principal_id: str, packet: Dict[str, Any]):
        self._wait(("review",))
        return self._desk.review(principal_id, packet)


def _threat_design(inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {**ex._design(inputs), "threat_model": ["a user changes another user's preferences"]}


def run_at_tier(tier: Optional[str], minutes: int = 120, review_risk: bool = True,
                threats: bool = False) -> SliceReport:
    """The notifications change at a declared tier (None: undeclared, the HIGH default)."""
    return run_with_schedule(tier, minutes, review_risk, threats)[0]


def run_with_schedule(tier: Optional[str], minutes: int = 120, review_risk: bool = True,
                      threats: bool = False) -> Tuple[SliceReport, List[Dict[str, Any]]]:
    """As run_at_tier, also returning the human decision schedule the run produced."""
    clock = SimClock()
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES, clock=clock)
    desk = ScriptedDesk(answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS if review_risk else {},
                        scenarios=ex.DOMAIN_SCENARIOS,
                        risk={"tier": tier, "rationale": "experiment"} if tier else None)
    model = ex.model() if review_risk else ex.model().with_override(
        "code_review_agent", "review", lambda inputs: {"findings": []})
    if threats:
        model = model.with_override("architecture_agent", "design", _threat_design)
    timed = TimedDesk(desk, clock, minutes)
    report = SliceOrchestrator(env, model, timed).release(ex.REQUEST)
    return report, timed.schedule


CASES = (("HIGH (default)", None, True, False), ("MEDIUM", "MEDIUM", True, False),
         ("LOW, reviewer accepts a risk", "LOW", True, False),
         ("LOW, clean review", "LOW", False, False),
         ("LOW, design lists a threat", "LOW", False, True))


def comparison(minutes: int = 120) -> List[Dict[str, Any]]:
    rows = []
    for label, tier, risk, threats in CASES:
        report = run_at_tier(tier, minutes, risk, threats)
        L = report.latency
        rows.append({"case": label, "released": report.completed, "effective_tier": L["risk"]["effective"],
                     "signals": [s["code"] for s in L["risk"]["signals"]],
                     "human_decisions": L["human_decisions"], "policy_decisions": L["policy_decisions"],
                     "critical_path_minutes": L["approval_queue_minutes"]["critical_path"],
                     "lead_time_minutes": L["lead_time_minutes"], "escalations": L["escalations"],
                     "catch_up": [f"{e['artifact']} {e['role']}" for e in report.escalations]})
    return rows


# --------------------------------------------------------------- sensitivity

# Human response times are unknown, so they are sampled. Log-normal: a median and a spread (sigma of the
# log); sigma 1.0 has a heavy right tail (the 90th percentile is about 3.6x the median).
DISTRIBUTIONS = tuple((median, sigma) for median in (30, 120, 480) for sigma in (0.5, 1.0))


def simulate_lead(schedule: List[Dict[str, Any]], median: float, sigma: float, rng: Any) -> float:
    """Waits run in sequence; the people asked in one wait answer in parallel, so it lasts as long as the slowest."""
    import math
    return sum(max(rng.lognormvariate(math.log(median), sigma) for _ in range(step["people"])) for step in schedule)


def sensitivity(samples: int = 2000, seed: int = 2026) -> Dict[str, Any]:
    import random
    import statistics
    cases = []
    for label, tier, risk, threats in CASES:
        report, schedule = run_with_schedule(tier, 1, risk, threats)
        cases.append({"case": label, "released": report.completed, "effective_tier": report.latency["risk"]["effective"],
                      "human_decisions": report.latency["human_decisions"],
                      "policy_authorizations": report.latency["policy_decisions"],
                      "waits": len(schedule), "people_asked": sum(s["people"] for s in schedule), "schedule": schedule})
    rows = []
    for median, sigma in DISTRIBUTIONS:
        base = None
        for c in cases:
            rng = random.Random(f"{seed}:{median}:{sigma}")  # common random numbers across cases
            leads = sorted(simulate_lead(c["schedule"], median, sigma, rng) for _ in range(samples))
            p50, p90 = statistics.median(leads), leads[int(0.9 * len(leads)) - 1]
            if base is None:
                base = p50
            rows.append({"median_minutes": median, "sigma": sigma, "case": c["case"],
                         "lead_p50_hours": round(p50 / 60, 1), "lead_p90_hours": round(p90 / 60, 1),
                         "reduction_vs_high_pct": round(100 * (1 - p50 / base))})
    return {"samples": samples, "seed": seed, "cases": cases, "rows": rows}
