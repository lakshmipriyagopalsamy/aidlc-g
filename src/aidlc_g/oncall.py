"""On-call buddy: a chat-first front end for read-only incident support (extends milestone M5).

A production alert lands in a chat channel. An on-call engineer asks the bot in
that thread what is happening - from a phone if need be. The bot acknowledges,
runs a read-only investigation over four evidence paths, and replies in the
thread with a grounded explanation. The engineer then decides what happens next.

    alert -> thread -> question -> investigation -> cited reply -> human decision

Evidence paths (all read-only, see ``ops.OpsStore``):
* knowledge: runbooks from the governed repository, past incidents;
* conversation history: prior operational threads, only in channels the asking
  engineer can see;
* runtime platform: metrics, logs, deployments with release tags and git
  commits, configuration changes (credential values redacted);
* delivery evidence: the gated release record, its tests and the domain
  scenarios its health checks protect.

Everything here is tool-neutral. ``ChatChannel`` is the port a real chat bot
adapter implements; ``FakeChatChannel`` stands in for tests. The bot has no
production authority of its own:

* A chat message counts only if the chat platform verified who sent it and
  that person is linked to a gate principal. Display names prove nothing.
* A decision must quote the receipt of the investigation the engineer was
  shown. The gate refuses a decision on anything but the latest packet.
* Decisions and rollbacks are recorded with the engineer's own identity, so the
  gate's human-only, role-checked incident rules apply unchanged. The bot never
  holds a rollback capability; the deployment service carries out a rollback
  only on an ALLOW decision from the gate.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Protocol

from .deploy import DeploymentService
from .errors import GovernanceError
from .service import ALLOW, GovernanceService

OUTCOMES = {"noisy": "FALSE_OR_NOISY", "understood": "ISSUE_UNDERSTOOD", "deeper": "NEEDS_DEEPER_ACTION"}
ACTIONS = {"none": "NONE", "rollback": "ROLLBACK", "failover": "FAILOVER", "scale": "SCALE"}
RECEIPT_CHARS = 8
_DECIDE = re.compile(
    r"^\s*decide\s+(?P<outcome>noisy|understood|deeper)"
    r"(?:\s+(?P<hypothesis>H\d+))?(?:\s+(?P<action>none|rollback|failover|scale))?"
    r"\s+receipt\s+(?P<receipt>[0-9a-f]{%d})(?:\s+(?P<rationale>.+))?\s*$" % RECEIPT_CHARS, re.IGNORECASE)


# ------------------------------------------------------------------- chat port


@dataclass(frozen=True)
class ChatEvent:
    channel: str
    thread_id: str
    user_id: str  # the chat platform's user id, not a display name
    text: str
    verified: bool  # the adapter checked the platform's request signature


class ChatChannel(Protocol):
    def post(self, channel: str, thread_id: Optional[str], text: str) -> str:
        """Post a message; return its thread id (a new one when ``thread_id`` is None)."""
        ...


class FakeChatChannel:
    """In-memory chat for tests and demos; records every post."""

    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []

    def post(self, channel: str, thread_id: Optional[str], text: str) -> str:
        thread = thread_id or f"{channel}/t{len(self.posts) + 1}"
        self.posts.append({"channel": channel, "thread_id": thread, "text": text})
        return thread

    def thread(self, thread_id: str) -> List[str]:
        return [p["text"] for p in self.posts if p["thread_id"] == thread_id]


# -------------------------------------------------------------------- identity


@dataclass(frozen=True)
class LinkedUser:
    principal_id: str
    token: str  # stands in for a delegated, single-sign-on-backed credential
    channels: FrozenSet[str]


class IdentityDirectory:
    """Chat accounts linked to gate principals by an administrator (in production: SSO account linking)."""

    def __init__(self) -> None:
        self._links: Dict[str, LinkedUser] = {}

    def link(self, chat_user_id: str, principal_id: str, token: str, channels: FrozenSet[str]) -> None:
        self._links[chat_user_id] = LinkedUser(principal_id, token, frozenset(channels))

    def resolve(self, event: ChatEvent) -> Optional[LinkedUser]:
        return self._links.get(event.user_id) if event.verified else None


# ------------------------------------------------------------------------ bot


Investigator = Callable[[str, FrozenSet[str], Optional[int]], Dict[str, Any]]


@dataclass
class _Thread:
    incident_id: str
    alert: Dict[str, Any]
    investigations: int = 0
    decided: bool = False
    replies: List[str] = field(default_factory=list)


class OnCallBuddy:
    """Chat front end. ``investigate(incident_id, visible_channels, asked_at_minute)`` files and returns a packet."""

    def __init__(self, gate: GovernanceService, chat: ChatChannel, directory: IdentityDirectory,
                 investigate: Investigator, deployer: Optional[DeploymentService] = None) -> None:
        self._gate = gate
        self._chat = chat
        self._directory = directory
        self._investigate = investigate
        self._deployer = deployer
        self._threads: Dict[str, _Thread] = {}

    # --- alerts ---------------------------------------------------------------

    def on_alert(self, channel: str, alert: Dict[str, Any]) -> str:
        text = (f":rotating_light: {alert['severity']} {alert['service']} ({alert['environment']}): "
                f"{alert['metric']} alert {alert['alert_id']}. Ask me in this thread: \"what is happening?\"")
        thread_id = self._chat.post(channel, None, text)
        self._threads[thread_id] = _Thread(f"INC-{alert['alert_id']}", dict(alert))
        return thread_id

    # --- messages -------------------------------------------------------------

    def on_message(self, event: ChatEvent, asked_at_minute: Optional[int] = None) -> str:
        thread = self._threads.get(event.thread_id)
        if thread is None:
            return self._reply(event, "I only answer in alert threads I opened.")
        user = self._directory.resolve(event)
        if user is None:
            return self._reply(event, "I can only act for verified chat users linked to an on-call identity. "
                                      "Nothing was investigated or recorded.")
        if event.channel not in user.channels:
            return self._reply(event, "You are not a member of this channel's on-call scope.")
        command = _DECIDE.match(event.text)
        if command:
            return self._decide(event, thread, user, command)
        if event.text.strip().lower().startswith("decide"):
            return self._reply(event, "Format: decide <noisy|understood|deeper> [H#] [none|rollback|failover|scale] "
                                      f"receipt <{RECEIPT_CHARS} hex> [why]")
        return self._ask(event, thread, user, asked_at_minute)

    def _ask(self, event: ChatEvent, thread: _Thread, user: LinkedUser, asked_at_minute: Optional[int]) -> str:
        if thread.decided:
            return self._reply(event, f"{thread.incident_id} is already decided; open a new incident to re-investigate.")
        a = thread.alert
        self._chat.post(event.channel, event.thread_id,
                        f"On it. Read-only investigation of {a['service']} in {a['environment']} for alert "
                        f"{a['alert_id']}; history limited to channels you can see.")
        try:
            packet = self._investigate(thread.incident_id, user.channels, asked_at_minute)
        except GovernanceError as exc:
            return self._reply(event, f"I could not complete the investigation ({exc.code}). Nothing was recorded "
                                      "as a decision; follow the runbook.")
        thread.investigations += 1
        receipt = self._latest_packet(user, thread)["payload_digest"][7:7 + RECEIPT_CHARS]
        return self._reply(event, render(packet, receipt))

    def _decide(self, event: ChatEvent, thread: _Thread, user: LinkedUser, m: "re.Match[str]") -> str:
        if thread.investigations == 0:
            return self._reply(event, "Ask me what is happening first; decisions name an investigation.")
        latest = self._latest_packet(user, thread)
        if not latest["payload_digest"][7:].startswith(m.group("receipt").lower()):
            return self._reply(event, "That receipt is not the latest investigation in this thread "
                                      f"(latest: {latest['payload_digest'][7:7 + RECEIPT_CHARS]}). Re-read it, then decide.")
        outcome = OUTCOMES[m.group("outcome").lower()]
        action = ACTIONS[(m.group("action") or "none").lower()]
        decision = {"packet_digest": latest["payload_digest"], "outcome": outcome,
                    "hypothesis_id": m.group("hypothesis").upper() if m.group("hypothesis") else None,
                    "diagnostic_steps_taken": [], "action": action,
                    "rationale": m.group("rationale") or f"{outcome.lower().replace('_', ' ')} (decided in chat)"}
        try:
            self._gate.record_incident(user.token, thread.incident_id, "DECISION", decision)
        except GovernanceError as exc:
            return self._reply(event, f"The gate refused the decision: {exc.code}. {exc.message}")
        thread.decided = True
        lines = [f"Recorded: {outcome} by {user.principal_id} on investigation {m.group('receipt').lower()}"
                 + (f", action {action}" if action != "NONE" else "") + "."]
        if action == "ROLLBACK":
            lines.append(self._rollback(user, thread))
        elif action != "NONE":
            lines.append(f"{action} is recorded as your decision; carry it out through your normal change process.")
        return self._reply(event, " ".join(lines))

    def _rollback(self, user: LinkedUser, thread: _Thread) -> str:
        env = thread.alert["environment"]
        verdict = self._gate.evaluate_incident_rollback(user.token, thread.incident_id, env,
                                                        f"{thread.incident_id}:rollback")
        if verdict["decision"] != ALLOW:
            return f"Rollback not allowed: {', '.join(r['code'] for r in verdict['reasons'])}."
        if self._deployer is None:
            return f"Rollback allowed by the gate (decision {verdict['transition_id']}); no deployment service attached."
        done = self._deployer.execute_rollback(verdict)
        return f"Rollback {done.outcome}; {env} now runs {str(done.running_after)[7:19]}."

    def _latest_packet(self, user: LinkedUser, thread: _Thread) -> Dict[str, Any]:
        events = self._gate.get_incident(user.token, thread.incident_id)["events"]
        return [e for e in events if e["event"] == "PACKET"][-1]

    def _reply(self, event: ChatEvent, text: str) -> str:
        self._chat.post(event.channel, event.thread_id, text)
        thread = self._threads.get(event.thread_id)
        if thread is not None:
            thread.replies.append(text)
        return text


# ------------------------------------------------------------------ rendering


def _age(packet: Dict[str, Any], ids: List[str]) -> str:
    ages = [packet["evidence_age_minutes"][i] for i in ids if i in packet.get("evidence_age_minutes", {})]
    return f" (newest {min(ages)} min old)" if ages else ""


def render(packet: Dict[str, Any], receipt: str) -> str:
    """The thread reply: situation, live state, history, next checks, gaps, and how to decide."""
    hyps = sorted(packet["working_hypotheses"], key=lambda h: h["rank"])
    out: List[str] = []
    if packet.get("situation"):
        s = packet["situation"]
        out += ["*Situation*", f"{s['statement']}{_age(packet, s['evidence'])}"]
    out.append("*Likely cause (hypotheses, not a root cause)*")
    for h in hyps[:3]:
        out.append(f"{h['hypothesis_id']} [{h['confidence']}] {h['statement']}. Confirm: {h['how_to_confirm']}. "
                   f"Evidence: {', '.join(h['supporting'][:4])}{_age(packet, h['supporting'])}")
    rc = packet.get("release_check")
    if rc:
        out.append("*Release check*")
        out.append(f"{rc['status']}: {rc['detail']}"
                   + (f" (tag {rc['running_tag']}, commit {str(rc['running_commit'])[:12]})" if rc.get("running_tag") else ""))
        for s in rc.get("scenarios_at_risk", []):
            out.append(f"At risk: {s['scenario_id']} \"{s['rule']}\" decided by {s['decided_by']}; "
                       f"locked tests {', '.join(s['tests']) or 'none'}; watched by {s['health_check']}")
    if packet["recent_changes"]:
        out.append("*Recent changes*")
        for c in packet["recent_changes"]:
            flag = "" if c.get("through_gated_release") else "  NOT through a gated release"
            out.append(f"{c.get('kind', 'DEPLOYMENT').lower()} {c['change_id']} at minute {c['deployed_at_minute']}{flag}")
    history = [f"{m['incident_id']}: {m['similarity_note']}" for m in packet["matching_incidents"]]
    history += [f"thread {c['thread_id']} in #{c['channel']}: {c['note']}" for c in packet.get("related_conversations", [])]
    if history:
        out += ["*Historical context*", *history]
    if packet.get("conflicting_signals"):
        out += ["*Conflicting signals*", *[c["statement"] for c in packet["conflicting_signals"]]]
    out.append("*Next checks (read-only)*")
    out += [f"{s['step_id']}: {s['action']} {s['target']} - {s['rationale']}" for s in packet["first_diagnostic_steps"]]
    if packet["unknowns"]:
        out += ["*Gaps*", *packet["unknowns"]]
    # Suggest the action that fits the leading hypothesis; the engineer may still choose any.
    action = {"RELEASE_REGRESSION": " rollback", "PROVIDER_OUTAGE": " failover", "CAPACITY": " scale"}.get(
        hyps[0]["cause_class"], "")
    out += ["*You decide*",
            f"decide noisy receipt {receipt}  |  decide understood H1 receipt {receipt}  |  "
            f"decide deeper H1{action} receipt {receipt}",
            f"Nothing changes in production without your decision. Escalation owner: {packet['escalation_owner']}."]
    return "\n".join(out)


def simulated_investigator(gate: GovernanceService, workspace: Any, ops_token: str, scenario: Any,
                           model: Any = None, tools: Optional[FrozenSet[str]] = None) -> Investigator:
    """Investigations over one simulated scenario, scoped per question to the asker's channels."""
    from .ops import OPS_TOOLS, OperationalSupportAgent, OpsStore, ops_broker
    from .triage import ReferenceTriageModel

    store = OpsStore(gate, scenario)

    def investigate(incident_id: str, channels: FrozenSet[str], asked_at_minute: Optional[int]) -> Dict[str, Any]:
        broker = ops_broker(tools or OPS_TOOLS, "ops_agent", ops_token, gate, workspace, store, channels)
        agent = OperationalSupportAgent(model or ReferenceTriageModel(), broker)
        return agent.triage(incident_id, asked_at_minute=asked_at_minute)

    return investigate
