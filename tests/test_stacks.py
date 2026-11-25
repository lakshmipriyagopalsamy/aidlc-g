"""The team picks the stack: profiles, instruction files, C2 binding, runner execution and gate checks."""

import json
import sys

import pytest

from aidlc_g.claude_model import render_inputs
from aidlc_g.contracts import StackProfile
from aidlc_g.examples import notifications as ex
from aidlc_g.examples import notifications_jvm as jvm
from aidlc_g.lifecycle import Stage
from aidlc_g.orchestrator import Engineering, SliceEnvironment, SliceOrchestrator
from aidlc_g.product import load_pack, scaffold
from aidlc_g.runner import TrustedRunner
from aidlc_g.stacks import (BUILT_IN, DEFAULT_STACK, DEFAULT_STACK_DIGEST, DOTNET_ASPNET_XUNIT, JAVA_MICRONAUT_GRADLE,
                            JAVA_SPRING_MAVEN, csharp_imports, expand_command, instructions_digest, jvm_imports,
                            profile_digest, scan_jvm_or_nuget)
from aidlc_g.testkit import World

from test_product import keyboard


def run_jvm(model=None, desk=None, engineering=None):
    env = SliceEnvironment.create(jvm.SEED, ex.ADVISORIES, stack=jvm.JAVA_SPRING_SIMULATED)
    report = SliceOrchestrator(env, model or jvm.model(), desk or jvm.desk(),
                               engineering=engineering or jvm.engineering()).release(ex.REQUEST)
    return env, report


def codes(report):
    return [r["code"] for r in (report.halted or {}).get("reasons", [])]


# ------------------------------------------------------------ end to end


def test_team_chosen_java_stack_releases_with_a_named_waiver():
    env, report = run_jvm()
    assert report.completed, report.halted
    c2 = env.gate.get_artifact(env.admin, "TR-NOTIFY")["body"]
    assert c2["stack"]["stack_id"] == "java-spring-simulated"
    assert c2["stack"]["decided_by"] == "eng_lead"
    assert c2["stack"]["profile_digest"] == profile_digest(jvm.JAVA_SPRING_SIMULATED)
    assert c2["stack"]["instructions"][0]["path"] == "instructions/conventions.md"
    assert c2["stack"]["instructions_digest"] == instructions_digest(jvm.INSTRUCTIONS)
    assert c2["stack"]["quality_waivers"] == ["MUTATION"]
    assert c2["domain_interface"] == ["com.acme.notifications"]
    runs = {r.kind: r for r in report.runs}
    assert {r["outcome"] for r in runs["BASELINE_STUB"].payload["results"]} == {"FAILED"}
    assert {r["outcome"] for r in runs["CANDIDATE"].payload["results"]} == {"PASSED"}
    assert all(r["stack_digest"] == profile_digest(jvm.JAVA_SPRING_SIMULATED)
               for r in (runs["CANDIDATE"].payload, runs["QUALITY"].payload))
    merged = next(t for t in report.transitions if t["to_stage"] == "DOCUMENTATION")
    assert merged["decision"] == "ALLOW"
    assert "## Technology stack (decided by the team)" in report.to_markdown()


def test_without_the_waiver_the_gate_blocks_on_the_missing_mutation_check():
    _, report = run_jvm(desk=jvm.desk(waivers=[]))
    assert not report.completed
    assert "QUALITY_CHECK_NOT_RUN" in codes(report)


def test_runner_on_a_different_stack_than_the_team_decided_is_caught():
    env = SliceEnvironment.create(jvm.SEED, ex.ADVISORIES, stack=jvm.JAVA_SPRING_SIMULATED)
    env.runner.use_stack = lambda profile: None  # a platform that ignores the team's decision
    env.runner.stack, env.runner.stack_digest = JAVA_SPRING_MAVEN, profile_digest(JAVA_SPRING_MAVEN)
    report = SliceOrchestrator(env, jvm.model(), jvm.desk(), engineering=jvm.engineering()).release(ex.REQUEST)
    assert not report.completed
    assert "STACK_MISMATCH" in codes(report)


def test_agents_receive_the_stack_and_team_instructions_but_cannot_change_the_binding():
    seen = {}

    def design(inputs):
        seen.update(inputs["engineering_context"])
        out = jvm._design(inputs)
        return out  # the model has no stack field to set; the binding is copied from the decision

    env, report = run_jvm(model=jvm.model().with_override("architecture_agent", "design", design))
    assert report.completed, report.halted
    assert seen["stack"]["stack_id"] == "java-spring-simulated"
    assert seen["stack"]["cannot_run"] == ["MUTATION"]
    assert "Java 21, Spring Boot 3" in seen["team_instructions"]["conventions.md"]


def test_reflection_into_private_state_bypasses_the_java_domain_interface():
    _, report = run_jvm(model=jvm.model().with_override("acceptance_test_agent", "author_tests", jvm.internal_tests))
    assert not report.completed
    assert "TEST_BYPASSES_DOMAIN_INTERFACE" in codes(report)


def test_lead_can_pick_another_stack_at_the_desk_and_the_runner_follows():
    eng = Engineering(jvm.JAVA_SPRING_SIMULATED, dict(jvm.INSTRUCTIONS),
                      {"python-pytest": DEFAULT_STACK, jvm.JAVA_SPRING_SIMULATED.stack_id: jvm.JAVA_SPRING_SIMULATED})
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES, stack=jvm.JAVA_SPRING_SIMULATED)
    desk = ex.desk()
    desk.stack_choice = {"stack_id": "python-pytest", "quality_waivers": []}
    report = SliceOrchestrator(env, ex.model(), desk, engineering=eng).release(ex.REQUEST)
    assert report.completed, report.halted
    assert env.runner.stack.stack_id == "python-pytest"
    assert report.stack_decision["stack_id"] == "python-pytest"


def test_example_without_a_team_decision_keeps_the_default_python_stack():
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES)
    report = SliceOrchestrator(env, ex.model(), ex.desk()).release(ex.REQUEST)
    assert report.completed
    assert env.gate.get_artifact(env.admin, "TR-NOTIFY")["body"].get("stack") is None
    assert all(r.payload.get("stack_digest", DEFAULT_STACK_DIGEST) == DEFAULT_STACK_DIGEST for r in report.runs)


# ---------------------------------------------------------- gate rules


def stack_binding(**overrides):
    body = {"stack_id": "java-spring-maven", "profile_digest": profile_digest(JAVA_SPRING_MAVEN),
            "framework": "Spring Boot 3", "instructions": [], "instructions_digest": instructions_digest({}),
            "quality_waivers": [], "decided_by": "eng_lead"}
    body.update(overrides)
    return body


def test_an_agent_cannot_be_recorded_as_the_stack_decider():
    w = World()
    w.advance_to(Stage.SPIKE)
    w.make_c2(stack=stack_binding(decided_by="architecture_agent"))
    assert "STACK_OWNER_INVALID" in [r["code"] for r in w.transition(Stage.STORIES)["reasons"]]


def test_a_named_engineering_lead_is_a_valid_stack_decider():
    w = World()
    w.advance_to(Stage.SPIKE)
    w.make_c2(stack=stack_binding())
    assert w.transition(Stage.STORIES)["decision"] == "ALLOW"


def test_default_stack_attestation_against_a_java_c2_is_a_mismatch():
    w = World()
    w.advance_to(Stage.SPIKE)
    w.make_c2(stack=stack_binding())
    assert w.transition(Stage.STORIES)["decision"] == "ALLOW"
    for make in dict(World.STEPS)[Stage.ACCEPTANCE_TESTS]:
        getattr(w, make)()
    assert w.transition(Stage.ACCEPTANCE_TESTS)["decision"] == "ALLOW"
    for make in dict(World.STEPS)[Stage.IMPLEMENTATION]:
        getattr(w, make)()  # the testkit runner attests on the default python stack
    reasons = [(r["code"], r.get("slot")) for r in w.transition(Stage.IMPLEMENTATION)["reasons"]]
    assert ("STACK_MISMATCH", "BASELINE") in reasons


# --------------------------------------------------------- the runner


def test_build_commands_expand_per_tool():
    assert expand_command(JAVA_SPRING_MAVEN, ["ATest", "BTest"])[4] == "-Dtest=ATest,BTest"
    assert expand_command(JAVA_MICRONAUT_GRADLE, ["ATest", "BTest"]) == [
        "gradle", "-q", "test", "--tests", "ATest", "--tests", "BTest"]
    argv = expand_command(DOTNET_ASPNET_XUNIT, ["ATests", "BTests"])
    assert "FullyQualifiedName~ATests|FullyQualifiedName~BTests" in argv
    assert "junit;LogFilePath=TestResults/{assembly}.xml" in argv


def test_missing_build_tool_is_an_error_never_a_pass(tmp_path):
    runner = TrustedRunner.__new__(TrustedRunner)
    runner.stack = StackProfile(**{**JAVA_SPRING_MAVEN.model_dump(), "test_command": ["no-such-build-tool-xyz", "{names}"]})
    outcomes, out = runner._run_command({"uat/ATest.java": b"class ATest {}"}, ["uat/ATest.java"])
    assert outcomes == {"uat/ATest.java": "ERROR"} and "no-such-build-tool-xyz" in out


def test_junit_cases_map_to_files_by_class_name_and_ambiguity_fails_closed(tmp_path):
    report = tmp_path / "TEST-x.xml"
    report.write_text('<testsuite><testcase classname="com.acme.ATest" name="a"/>'
                      '<testcase classname="com.acme.BTest" name="b"><failure/></testcase>'
                      '<testcase classname="com.acme.CTest" name="c"/></testsuite>')
    paths = ["uat/ATest.java", "uat/BTest.java", "uat/x/CTest.java", "tests/coverage/CTest.java", "uat/DTest.java"]
    assert TrustedRunner._parse_junit([report], paths) == {
        "uat/ATest.java": "PASSED", "uat/BTest.java": "FAILED",
        "uat/x/CTest.java": "ERROR", "tests/coverage/CTest.java": "ERROR",  # two files claim CTest
        "uat/DTest.java": "ERROR"}  # nothing ran


def test_java_imports_count_package_declarations_and_flag_reflection():
    tree = {"src/main/java/com/acme/billing/Invoice.java": b"package com.acme.billing;\nclass Invoice {}",
            "src/main/java/com/acme/billing/internal/Store.java": b"package com.acme.billing.internal;\nclass Store {}",
            "uat/A.java": b"package com.acme.billing.acceptance;\nimport com.acme.billing.Invoice;\n"
                          b"import static com.acme.billing.internal.Store.load;\nimport org.junit.jupiter.api.Test;",
            "uat/B.java": b"package com.acme.billing.internal;\nclass B {}",
            "uat/C.java": b"import com.acme.billing.*;\nClass.forName(\"x\");"}
    assert jvm_imports(tree, "uat/A.java") == ["com.acme.billing", "com.acme.billing.internal"]
    assert jvm_imports(tree, "uat/B.java") == ["com.acme.billing.internal"]  # same package: no import needed
    assert jvm_imports(tree, "uat/C.java") == ["<dynamic>", "com.acme.billing"]


def test_csharp_imports_include_enclosing_namespaces_and_reflection():
    tree = {"src/Acme.Billing/Invoice.cs": b"namespace Acme.Billing;\npublic class Invoice {}",
            "src/Acme.Billing/Internal/Store.cs": b"namespace Acme.Billing.Internal;\nclass Store {}",
            "uat/InvoiceTests.cs": b"using Acme.Billing;\nusing Xunit;\nnamespace Acme.Billing.Acceptance;",
            "uat/PeekTests.cs": b"using Acme.Billing.Internal;\nvar f = typeof(Invoice).GetField(\"x\", BindingFlags.NonPublic);"}
    assert csharp_imports(tree, "uat/InvoiceTests.cs") == ["Acme.Billing"]
    assert csharp_imports(tree, "uat/PeekTests.cs") == ["<dynamic>", "Acme.Billing.Internal"]


def test_dependency_scans_for_maven_gradle_and_nuget():
    adv = {("jackson-databind", "2.9.0"): "ADV-SYN-2", ("newtonsoft.json", "9.0.1"): "ADV-SYN-3"}
    pom = {"pom.xml": b"<dependency><artifactId>jackson-databind</artifactId><version>2.9.0</version></dependency>"
                      b"<dependency><artifactId>spring-boot-starter</artifactId></dependency>"}
    assert scan_jvm_or_nuget(pom, "maven", adv) == (2, ["pom.xml: jackson-databind 2.9.0 ADV-SYN-2"])
    gradle = {"build.gradle": b"implementation 'com.fasterxml.jackson.core:jackson-databind:2.9.0'"}
    assert scan_jvm_or_nuget(gradle, "gradle", adv)[1] == ["build.gradle: jackson-databind 2.9.0 ADV-SYN-2"]
    nuget = {"src/A/A.csproj": b'<PackageReference Include="Newtonsoft.Json" Version="9.0.1" />'}
    assert scan_jvm_or_nuget(nuget, "nuget", adv)[1] == ["src/A/A.csproj: Newtonsoft.Json 9.0.1 ADV-SYN-3"]


def test_profile_digest_changes_with_the_build_command():
    changed = StackProfile(**{**JAVA_SPRING_MAVEN.model_dump(), "test_command": ["mvn", "verify"]})
    assert profile_digest(changed) != profile_digest(JAVA_SPRING_MAVEN)
    assert set(BUILT_IN) == {"python-pytest", "java-spring-maven", "java-micronaut-gradle", "kotlin-spring-gradle",
                             "dotnet-aspnet-xunit"}


# ------------------------------------------------- packs, desk, prompts


def test_pack_with_a_java_stack_and_instructions(tmp_path):
    scaffold(tmp_path / "inv", "INVOICE", stack="java-micronaut-gradle")
    spec = json.loads((tmp_path / "inv" / "product.json").read_text())
    assert spec["stack"] == "java-micronaut-gradle"
    assert (tmp_path / "inv" / "repo" / "src" / "README.md").read_text().startswith("Put the existing Micronaut 4")
    spec.update(name="Invoices", change_request="Remind customers before an invoice is overdue")
    spec["sponsor"].update(problem="late payment", metric="on-time share", measurement_source="ledger")
    (tmp_path / "inv" / "product.json").write_text(json.dumps(spec))
    (tmp_path / "inv" / "instructions" / "security.md").write_text("Never log card numbers.")
    pack = load_pack(tmp_path / "inv")
    assert pack.engineering.stack == JAVA_MICRONAUT_GRADLE
    assert sorted(pack.engineering.instructions) == ["conventions.md", "security.md"]


def test_pack_rejects_an_unknown_stack_and_accepts_a_custom_profile(tmp_path):
    scaffold(tmp_path / "p", "INVOICE")
    spec = json.loads((tmp_path / "p" / "product.json").read_text())
    spec.update(name="n", change_request="c", stack="cobol-cics")
    spec["sponsor"].update(problem="p", metric="m", measurement_source="s")
    (tmp_path / "p" / "product.json").write_text(json.dumps(spec))
    with pytest.raises(Exception, match="unknown stack"):
        load_pack(tmp_path / "p")
    spec["stack"] = {**JAVA_SPRING_MAVEN.model_dump(), "stack_id": "java-quarkus-maven", "framework": "Quarkus 3"}
    (tmp_path / "p" / "product.json").write_text(json.dumps(spec))
    pack = load_pack(tmp_path / "p")
    assert pack.engineering.stack.framework == "Quarkus 3"
    assert "java-quarkus-maven" in pack.engineering.options


def test_terminal_desk_records_the_stack_choice_and_waiver():
    kb = keyboard(["a", "y"])
    proposal = {"stack": jvm.JAVA_SPRING_SIMULATED.model_dump(mode="json"), "cannot_run": ["MUTATION"],
                "instructions": ["conventions.md"], "options": {}}
    assert kb.desk().choose_stack("eng_lead", proposal) == {"stack_id": "java-spring-simulated",
                                                             "quality_waivers": ["MUTATION"]}
    assert "you act as engineering_lead" in kb.text


def test_terminal_desk_can_switch_stack_and_refuse_the_waiver():
    kb = keyboard(["o", "9", "2", "n"])
    options = {sid: {"framework": p.framework, "build_tool": p.build_tool, "language": p.language,
                     "cannot_run": ["MUTATION"] if p.language != "python" else []} for sid, p in BUILT_IN.items()}
    proposal = {"stack": DEFAULT_STACK.model_dump(mode="json"), "cannot_run": [], "instructions": [],
                "options": options}
    choice = kb.desk().choose_stack("eng_lead", proposal)
    assert choice == {"stack_id": list(BUILT_IN)[1], "quality_waivers": []}
    assert "QUALITY_CHECK_NOT_RUN" in kb.text


def test_claude_prompt_fences_team_instructions_separately():
    text = render_inputs("design", {"x": 1, "engineering_context": {
        "stack": {"stack_id": "java-spring-maven"}, "team_instructions": {"conventions.md": "Use records."}}})
    assert '<team_instructions file="conventions.md">\nUse records.\n</team_instructions>' in text
    assert "<team_stack>" in text and '"engineering_context"' not in text
