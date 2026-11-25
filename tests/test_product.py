"""Terminal desk and product packs: a person decides at the keyboard, the gate still checks everything."""

import json
from typing import Iterable, List

import pytest

from aidlc_g.desk import DeskClosed, TerminalDesk
from aidlc_g.examples import notifications as ex
from aidlc_g.orchestrator import ProductRequest, SliceEnvironment, SliceOrchestrator, product_ids
from aidlc_g.product import environment_for, load_pack, nominal_telemetry, read_seed, scaffold

SC4 = ["only known notification categories exist", "any user",
       "a notification arrives in a category the product does not define", "it is rejected as unknown", "BOUNDARY"]


def keyboard(lines: Iterable[str]) -> "Keyboard":
    return Keyboard(list(lines))


class Keyboard:
    def __init__(self, lines: List[str]) -> None:
        self.lines, self.prompts, self.output = lines, [], []

    def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError()
        return self.lines.pop(0)

    def write(self, text: str) -> None:
        self.output.append(text)

    def desk(self) -> TerminalDesk:
        return TerminalDesk(self.read, self.write)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


def happy_path(sc2: List[str] = ("k",)) -> List[str]:
    return [
        "a",                                                      # C0 sponsor
        ex.ANSWERS["Q-CRITICAL"], ex.ANSWERS["Q-PERSIST"], "",    # clarifications; Q-CHANNELS left open
        "k", *sc2, "k", "d",                                      # SC-1, SC-2, SC-3 kept; SC-Q dropped
        "y", *SC4, "n",                                           # owner adds SC-4
        "a", "a", "a", "a", "a", "a",                             # C1, C2, STORIES x2, C3 x2
        "a", "", "a",                                             # F-1 risk accepted (owner eng_lead), review approve
        "a", "a", "a",                                            # DOCS x2, RELEASE
    ]


def run_example(lines: List[str]):
    kb = keyboard(["s", *lines])  # risk tier: skip, so the policy default (every approval) applies
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
    desk = kb.desk()
    report = SliceOrchestrator(env, ex.model(), desk).release(ex.REQUEST)
    return report, desk, kb


def test_person_at_the_keyboard_releases_the_example():
    report, desk, kb = run_example(happy_path())
    assert report.completed, report.halted
    assert report.final_stage == "RELEASED"
    decisions = {d["scenario_id"]: d["decision"] for d in report.domain_decisions}
    assert decisions == {"SC-1": "KEPT", "SC-2": "KEPT", "SC-3": "KEPT", "SC-Q": "DROPPED (agent proposal)",
                         "SC-4": "ADDED BY OWNER"}
    approvals = [e for e in desk.log if e["step"] == "approval"]
    assert len(approvals) == 10 and all(e["decision"] == "APPROVE" for e in approvals)
    assert {e["role"] for e in approvals} == {"sponsor", "product_owner", "engineering_lead", "qa", "sre",
                                              "release_manager"}
    review = next(e for e in desk.log if e["step"] == "review")
    assert review["resolutions"] == {"F-1": {"status": "RISK_ACCEPTED", "risk_owner": "eng_lead"}}
    assert "you act as product_owner" in kb.text and "It does not authorize" in kb.text
    assert not kb.lines  # every scripted keystroke was asked for


def test_rewritten_scenario_reaches_the_locked_tests_word_for_word():
    rewrite = ["r", "", "", "a security or account-recovery event occurs for their account", "", ""]
    report, _, _ = run_example(happy_path(rewrite))
    assert report.completed, report.halted
    row = next(d for d in report.domain_decisions if d["scenario_id"] == "SC-2")
    assert row["decision"] == "REWRITTEN" and "account-recovery" in row["text"]


def test_requesting_changes_stops_the_work_at_that_artifact():
    lines = happy_path()
    lines[0] = "c"
    lines.insert(1, "the metric owner is wrong")
    report, desk, _ = run_example(lines)
    assert not report.completed
    assert report.halted["stage"] == "EPIC" and report.halted["decision"] != "ALLOW"
    assert desk.log[0] == {"step": "risk", "principal": "eng_lead", "tier": None}
    assert desk.log[1] == {"step": "approval", "artifact": "INIT-NOTIFY@v1", "role": "sponsor", "principal": "sponsor",
                           "decision": "REQUEST_CHANGES", "rationale": "the metric owner is wrong"}


def test_owner_adding_a_scenario_the_agents_cannot_design_pauses_the_work():
    lines = happy_path()
    at = lines.index("n")
    lines[at:at + 1] = ["y", "users can mute one sender", "a user", "they mute a sender", "that sender is silent",
                        "NORMAL", "n"]
    report, _, _ = run_example(lines)
    assert not report.completed
    assert "SCENARIO_NOT_DESIGNED" in [r["code"] for r in report.halted["reasons"]]


def test_without_a_negative_scenario_the_desk_warns_and_the_gate_blocks():
    lines = happy_path()
    at = lines.index("y")
    lines[4:at + len(SC4) + 2] = ["k", "k", "d", "d", "n"]  # drop SC-3 (NEGATIVE), add no SC-4 (BOUNDARY)
    report, _, kb = run_example(lines)
    assert "No NEGATIVE or BOUNDARY scenario yet" in kb.text
    assert not report.completed
    assert "NEGATIVE_SCENARIO_MISSING" in [r["code"] for r in report.halted["reasons"]]


def test_approving_a_review_with_a_blocking_finding_open_is_refused():
    kb = keyboard(["l", "a"])
    packet = {"pull_request": {"id": "PR-1", "branch": "work/x"},
              "business_requirement": {"id": "BR@v1"}, "technical_requirement_and_adr": {"id": "TR@v1", "decision": "d"},
              "protected_uat": {"manifest": "AC@v1", "scenarios": {"SC-1": ["UAT-001"]}},
              "independent_checks": {"acceptance": {"UAT-001": "PASSED"}}, "security_findings": [],
              "surviving_mutants": [],
              "ai_review_findings": [{"finding_id": "F-9", "severity": "BLOCKING", "summary": "s", "status": "OPEN"}]}
    decision, resolutions = kb.desk().review("reviewer", packet)
    assert (decision, resolutions) == ("APPROVE", {})
    assert "blocking finding is still open" in kb.text  # the desk warns; the gate refuses (test_delivery covers that)


def test_invalid_choices_are_asked_again():
    kb = keyboard(["yes", "x", "a"])
    packet = {"decision": "Approve C0 X@v1", "artifact_id": "X", "version": 1, "summary": [],
              "open_blocking_decisions": "NONE", "existing_system_findings": [], "conflicts": [],
              "changes_since_last_version": "first version", "what_approval_authorizes": "a",
              "what_approval_does_not_authorize": "b", "evidence_receipt": {"digest": "sha256:" + "0" * 64}}
    assert kb.desk().decide("sponsor", "sponsor", packet) == ("APPROVE", None)
    assert kb.text.count("choose one of") == 2


def test_end_of_input_stops_without_deciding():
    with pytest.raises(DeskClosed):
        run_example(["a"])


# ---------------------------------------------------------------- packs


def test_scaffold_then_load_refuses_placeholders(tmp_path):
    scaffold(tmp_path / "inv", "INVOICE")
    with pytest.raises(ValueError, match="TODO"):
        load_pack(tmp_path / "inv")
    with pytest.raises(ValueError, match="already exists"):
        scaffold(tmp_path / "inv", "INVOICE")
    with pytest.raises(ValueError, match="key"):
        scaffold(tmp_path / "other", "bad key")


def filled_pack(tmp_path):
    scaffold(tmp_path / "inv", "INVOICE")
    spec = json.loads((tmp_path / "inv" / "product.json").read_text())
    spec.update(name="Invoice reminders", change_request="Remind customers before an invoice is overdue")
    spec["sponsor"].update(problem="Invoices are paid late", metric="Share of invoices paid on time",
                           measurement_source="billing ledger")
    (tmp_path / "inv" / "product.json").write_text(json.dumps(spec))
    return load_pack(tmp_path / "inv")


def test_loaded_pack_has_its_own_ids_and_seed(tmp_path):
    pack = filled_pack(tmp_path)
    assert pack.request.ids == product_ids("INVOICE")
    assert pack.request.ids["C1"] == "BR-INVOICE" and pack.work_item_id == "WI-INVOICE-001"
    assert pack.request.artifact_name == "invoice.tar"
    assert pack.request.sponsor_request["initiative_id"] == "INIT-INVOICE"
    assert set(pack.seed) == {"README.md", "src/invoice/__init__.py"}
    env = environment_for(pack)
    assert env.workspace is not None


def test_seed_skips_tool_directories_and_large_files(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "src" / "__pycache__").mkdir(parents=True)
    (tmp_path / "src" / "__pycache__" / "a.pyc").write_bytes(b"x")
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "big.bin").write_bytes(b"0" * (600 * 1024))
    assert read_seed(tmp_path) == {"src/a.py": b"x = 1\n"}


def test_nominal_telemetry_reports_each_health_check_at_its_threshold():
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
    telemetry = nominal_telemetry(env, "REL-NOTIFY-1")
    assert telemetry("staging", "sha256:" + "0" * 64, "canary") == {}  # no release yet
    report = SliceOrchestrator(env, ex.model(), ex.desk()).release(ex.REQUEST)
    assert report.completed
    checks = env.gate.get_artifact(env.admin, "REL-NOTIFY-1")["body"]["health_checks"]
    assert telemetry("production", "x", "canary") == {c["metric"]: c["threshold"] for c in checks}


def test_example_released_through_a_pack_style_environment():
    """The pack path (seed + nominal telemetry + per-product ids) releases end to end."""
    request = ProductRequest(ex.REQUEST.sponsor_request, ex.REQUEST.change_request, product_ids("NOTIFY"),
                             "notify.tar")
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
    env.deployer._telemetry = nominal_telemetry(env, request.ids["RELEASE"])
    report = SliceOrchestrator(env, ex.model(), ex.desk()).release(request)
    assert report.completed, report.halted
