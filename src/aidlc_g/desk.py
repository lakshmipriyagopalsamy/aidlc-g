"""Terminal desk: a person answers, writes the domain scenarios and decides, at the keyboard.

The orchestrator stops at every human step and calls the desk. ScriptedDesk
answers from a script; TerminalDesk asks you. In this simulation one person
plays every role (sponsor, product owner, engineering lead, QA, reviewer,
release manager), and the desk names the role before each decision so you know
whose call you are making. The gate still checks every decision, so typing
"approve" does not let anything through that the evidence does not support.

Input and output are injectable so tests can drive the desk with scripted lines.
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

KINDS = ("NORMAL", "NEGATIVE", "BOUNDARY")
FIELDS = ("rule", "given", "when", "then")


class DeskClosed(Exception):
    """The person stopped the session (end of input or Ctrl-C). Nothing after this point was decided."""


class TerminalDesk:
    def __init__(self, read: Callable[[str], str] = input, write: Callable[[str], None] = print) -> None:
        self._read = read
        self._write = write
        self.log: List[Dict[str, Any]] = []  # every human decision, in order, for the report

    # ------------------------------------------------------------ prompting

    def _ask(self, prompt: str, default: Optional[str] = None) -> str:
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        try:
            value = self._read(shown).strip()
        except (EOFError, KeyboardInterrupt):
            raise DeskClosed() from None
        return value or (default or "")

    def _require(self, prompt: str, default: Optional[str] = None) -> str:
        while True:
            value = self._ask(prompt, default)
            if value:
                return value
            self._write("  an answer is needed here")

    def _choose(self, prompt: str, options: Dict[str, str], default: Optional[str] = None) -> str:
        menu = "  ".join(f"[{k}]{label}" for k, label in options.items())
        while True:
            value = self._ask(f"{prompt}  {menu}", default).lower()
            if value in options:
                return value
            self._write(f"  choose one of: {', '.join(options)}")

    def _heading(self, text: str) -> None:
        self._write("")
        self._write(f"=== {text}")

    # ------------------------------------------------------- clarification

    def answer(self, principal_id: str, questions: List[Dict[str, Any]]) -> Dict[str, str]:
        if not questions:
            return {}
        self._heading(f"Clarifying questions from the requirements agent (you answer as {principal_id})")
        self._write("Leave an answer blank to keep the question open. An open blocking question stops the work.")
        answers: Dict[str, str] = {}
        for q in questions:
            tag = "blocking" if q["blocking"] else "non-blocking"
            self._write(f"\n{q['item_id']} ({tag}): {q['question']}")
            value = self._ask("  your answer")
            if value:
                answers[q["item_id"]] = value
        self.log.append({"step": "clarification", "principal": principal_id, "answers": dict(answers)})
        return answers

    # ---------------------------------------------------- domain scenarios

    def _show_scenario(self, sid: str, s: Dict[str, Any]) -> None:
        self._write(f"\n{sid}  {s['kind']}  rule: {s['rule']}")
        self._write(f"  Given {s['given']}")
        self._write(f"  When  {s['when']}")
        self._write(f"  Then  {s['then']}")

    def _edit(self, current: Dict[str, Any]) -> Dict[str, Any]:
        edited = {f: self._require(f"  {f}", current.get(f)) for f in FIELDS}
        kind = self._require("  kind (NORMAL / NEGATIVE / BOUNDARY)", current.get("kind")).upper()
        while kind not in KINDS:
            kind = self._require("  kind must be NORMAL, NEGATIVE or BOUNDARY", current.get("kind")).upper()
        edited["kind"] = kind
        return edited

    def define_scenarios(self, principal_id: str, candidates: List[Dict[str, Any]],
                         answers: Dict[str, str]) -> List[Dict[str, Any]]:
        self._heading(f"Domain scenarios: you decide the behavior (you are the domain owner, {principal_id})")
        self._write("These Given/When/Then statements become the acceptance tests word for word. Agents may not change them.")
        self._write("Keep, rewrite or drop each suggestion, then add any the agent missed.")
        self._write("The gate needs at least one NEGATIVE or BOUNDARY scenario: what must NOT happen, or the edge cases.")
        chosen: List[Dict[str, Any]] = []
        for c in candidates:
            self._show_scenario(c["candidate_id"], c)
            action = self._choose("  ", {"k": "eep", "r": "ewrite", "d": "rop"}, "k")
            if action == "k":
                chosen.append({"scenario_id": c["candidate_id"], **{k: c[k] for k in (*FIELDS, "kind")}})
            elif action == "r":
                chosen.append({"scenario_id": c["candidate_id"], **self._edit(c)})
        while True:
            if not any(s["kind"] in ("NEGATIVE", "BOUNDARY") for s in chosen):
                self._write("\nNo NEGATIVE or BOUNDARY scenario yet; the gate will block C1 without one.")
            more = self._choose("\nAdd a scenario of your own?", {"y": "es", "n": "o"}, "n")
            if more == "n":
                break
            used = {s["scenario_id"] for s in chosen} | {c["candidate_id"] for c in candidates}
            sid = next(f"SC-{n}" for n in range(1, len(used) + 2) if f"SC-{n}" not in used)
            self._write(f"New scenario {sid}:")
            chosen.append({"scenario_id": sid, **self._edit({"kind": "NORMAL"})})
        self.log.append({"step": "domain_scenarios", "principal": principal_id,
                         "scenarios": [dict(s) for s in chosen]})
        return chosen

    # ------------------------------------------------------------ approvals

    def _show_list(self, label: str, items: Any) -> None:
        if not items or items == "NONE":
            self._write(f"{label}: none")
            return
        if isinstance(items, str):
            self._write(f"{label}: {items}")
            return
        self._write(f"{label}:")
        for item in items:
            self._write(f"  - {item}")

    def decide(self, principal_id: str, role: str, packet: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        self._heading(f"{packet['decision']}  (you act as {role}, principal {principal_id})")
        if packet.get("why_now"):
            self._write(f"Asked now because {packet['why_now']}")
        self._show_list("Summary", packet["summary"])
        self._show_list("Open blocking decisions", packet["open_blocking_decisions"])
        self._show_list("What the agent found in the existing system", packet["existing_system_findings"])
        self._show_list("Conflicts", packet["conflicts"])
        self._show_list("Changed since the last version", packet["changes_since_last_version"])
        self._write(f"Approving authorizes: {packet['what_approval_authorizes']}")
        self._write(f"It does not authorize: {packet['what_approval_does_not_authorize']}")
        self._write(f"Evidence receipt: {packet['evidence_receipt']['digest'][:19]}...")
        action = self._choose("Your decision", {"a": "pprove", "c": "hanges requested", "r": "eject"})
        decision = {"a": "APPROVE", "c": "REQUEST_CHANGES", "r": "REJECT"}[action]
        rationale = None if decision == "APPROVE" else self._require("  why (recorded with the decision)")
        self.log.append({"step": "approval", "artifact": f"{packet['artifact_id']}@v{packet['version']}",
                         "role": role, "principal": principal_id, "decision": decision, "rationale": rationale})
        return decision, rationale

    # ----------------------------------------------------------------- risk

    def declare_risk(self, principal_id: str, proposal: Dict[str, Any]) -> Optional[Dict[str, str]]:
        self._heading(f"Risk tier: how much human sign-off this change needs (you act as engineering_lead, {principal_id})")
        self._write("Approvals cost time. A lower tier lets policy rules approve steps the gate's evidence already "
                    "covers; intent (C0), domain scenarios (C1) and acceptance tests (C3) always stay with people.")
        for tier, needs in proposal["tiers"].items():
            human = sum(len(v) for v in needs.values())
            policy = [c for c, v in needs.items() if not v]
            self._write(f"  {tier:6} {human} human approvals" + (f"; policy approves {', '.join(policy)}" if policy else ""))
        self._show_list("The gate raises the tier by itself when evidence shows", proposal["raised_by"])
        choice = self._choose("Tier", {"l": "ow", "m": "edium", "h": "igh", "s": "kip (policy default "
                                                                                  f"{proposal['default']})"}, "s")
        if choice == "s":
            self.log.append({"step": "risk", "principal": principal_id, "tier": None})
            return None
        tier = {"l": "LOW", "m": "MEDIUM", "h": "HIGH"}[choice]
        rationale = self._require("  why this tier fits (recorded)")
        self.log.append({"step": "risk", "principal": principal_id, "tier": tier, "rationale": rationale})
        return {"tier": tier, "rationale": rationale}

    # ---------------------------------------------------------------- stack

    def choose_stack(self, principal_id: str, proposal: Dict[str, Any]) -> Dict[str, Any]:
        stack = proposal["stack"]
        self._heading(f"Technology stack: the team decides (you act as engineering_lead, principal {principal_id})")
        self._write(f"Proposed in the product pack: {stack['stack_id']} - {stack['framework']}, {stack['build_tool']}")
        self._show_list("Build rules the agents must follow", stack["build_notes"])
        self._show_list("Team instruction files given to every agent", proposal["instructions"] or "none")
        self._write("Agents build on this stack and cannot change it; the gate checks CI ran exactly this stack.")
        choice = self._choose("Stack", {"a": "ccept", "o": "ther built-in stack"}, "a")
        stack_id = stack["stack_id"]
        cannot_run = list(proposal["cannot_run"])
        if choice == "o":
            options = list(proposal["options"])
            for n, sid in enumerate(options, 1):
                o = proposal["options"][sid]
                self._write(f"  {n}. {sid}: {o['framework']}, {o['build_tool']} ({o['language']})")
            while True:
                picked = self._require("  number")
                if picked.isdigit() and 1 <= int(picked) <= len(options):
                    stack_id = options[int(picked) - 1]
                    cannot_run = list(proposal["options"][stack_id]["cannot_run"])
                    break
                self._write(f"  choose 1 to {len(options)}")
        waivers: List[str] = []
        for check in cannot_run:
            self._write(f"\nThe trusted runner cannot run {check} on {stack_id}.")
            accept = self._choose(f"  Accept going without {check}? It is recorded in C2 as your named decision",
                                  {"y": "es", "n": "o"}, "n")
            if accept == "y":
                waivers.append(check)
            else:
                self._write(f"  Without a waiver the gate will block the merge (QUALITY_CHECK_NOT_RUN).")
        self.log.append({"step": "stack", "principal": principal_id, "stack_id": stack_id, "quality_waivers": waivers})
        return {"stack_id": stack_id, "quality_waivers": waivers}

    # --------------------------------------------------------------- review

    def review(self, principal_id: str, packet: Dict[str, Any]) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        pr = packet["pull_request"]
        self._heading(f"Pull request {pr['id']}: {pr['branch']} -> main  (you act as reviewer, principal {principal_id})")
        self._write(f"Requirement {packet['business_requirement']['id']}; design {packet['technical_requirement_and_adr']['id']} "
                    f"({packet['technical_requirement_and_adr']['decision']})")
        self._write(f"Locked acceptance tests {packet['protected_uat']['manifest']}: "
                    + ", ".join(f"{s} -> {', '.join(t)}" for s, t in packet["protected_uat"]["scenarios"].items()))
        checks = packet["independent_checks"]
        self._show_list("Acceptance results (trusted runner)", [f"{k}: {v}" for k, v in checks["acceptance"].items()])
        self._show_list("Quality checks", [f"{k}: {v}" for k, v in checks.items()
                                           if k in ("COVERAGE_TESTS", "MUTATION", "SECRET_SCAN", "DEPENDENCY_SCAN")])
        self._show_list("Security findings", packet["security_findings"])
        self._show_list("Surviving mutants (logic no test checks)", packet["surviving_mutants"])
        resolutions: Dict[str, Dict[str, Any]] = {}
        findings = [f for f in packet["ai_review_findings"] if f["status"] == "OPEN"]
        if findings:
            self._write("\nOpen findings from the review agent. Agents can raise findings; only you can clear them.")
        for f in findings:
            self._write(f"\n{f['finding_id']} ({f['severity']}): {f['summary']}")
            action = self._choose("  ", {"l": "eave open", "f": "ixed (you checked)", "a": "ccept the risk"}, "l")
            if action == "f":
                resolutions[f["finding_id"]] = {"status": "RESOLVED"}
            elif action == "a":
                owner = self._require("  who owns this risk", "eng_lead")
                resolutions[f["finding_id"]] = {"status": "RISK_ACCEPTED", "risk_owner": owner}
        if any(f["severity"] == "BLOCKING" and f["finding_id"] not in resolutions for f in findings):
            self._write("\nA blocking finding is still open; the gate will refuse an approval.")
        action = self._choose("\nYour review", {"a": "pprove", "c": "hanges requested"})
        decision = "APPROVE" if action == "a" else "REQUEST_CHANGES"
        self.log.append({"step": "review", "pull_request": pr["id"], "principal": principal_id,
                         "decision": decision, "resolutions": dict(resolutions)})
        return decision, resolutions
