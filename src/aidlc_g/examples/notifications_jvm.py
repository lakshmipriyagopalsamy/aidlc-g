"""The notifications example on a team-chosen JVM stack: Java, Spring Boot, Maven layout.

Same request, scenarios and people as notifications.py. Only the engineering
side changes: the team picks a Java/Spring stack and gives the agents an
instruction file; the scripted agents then design, test and implement in Java.

The build tool is fake_build.py, a stand-in that reads @check directives
instead of compiling Java (no JDK or Maven is needed to run the tests). The
profile, report parsing, attestation, stack binding and gate checks are the
real ones. With Maven installed, use stacks.JAVA_SPRING_MAVEN instead.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

from ..agents import ScriptedModel
from ..contracts import StackProfile
from ..orchestrator import Engineering, ScriptedDesk
from ..stacks import BUILT_IN, JAVA_SPRING_MAVEN
from . import notifications as ex

PKG = "src/main/java/com/acme/notifications"
SERVICE = f"{PKG}/PreferenceService.java"

JAVA_SPRING_SIMULATED = StackProfile(**{
    **JAVA_SPRING_MAVEN.model_dump(),
    "stack_id": "java-spring-simulated",
    "framework": "Spring Boot 3 (simulated build)",
    "build_tool": "fake_build.py standing in for Maven",
    "test_command": [sys.executable, str(Path(__file__).with_name("fake_build.py")), "{names}",
                     "target/surefire-reports"],
})

INSTRUCTIONS = {"conventions.md": (
    "# Notifications team conventions\n\n"
    "- Java 21, Spring Boot 3. Services are plain classes in com.acme.notifications; no field injection.\n"
    "- The public domain interface is the com.acme.notifications package. Tests use only that package.\n"
    "- Unknown input fails closed with a domain exception, never a null.\n"
    "- Tests: JUnit 5 and AssertJ, one test class per file, named after the behavior.\n"
)}

SEED: Dict[str, bytes] = {
    **{k: v for k, v in ex.SEED.items() if k.startswith("docs/")},
    "pom.xml": b"""<project><modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId><artifactId>notifications</artifactId><version>1.3.0</version>
  <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>3.3.4</version></parent>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter</artifactId></dependency>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><scope>test</scope></dependency>
  </dependencies>
  <!-- build-helper-maven-plugin adds uat/ and tests/coverage/ as test source roots -->
</project>
""",
    f"{PKG}/LegacyEmailPrefs.java": b"""package com.acme.notifications;

import java.util.HashSet;
import java.util.Set;

/** Today: only marketing email can be silenced. */
public class LegacyEmailPrefs {
    private final Set<String> optOut = new HashSet<>();

    public void optOutMarketingEmail(String userId) { optOut.add(userId); }
}
""",
}

INTERFACE = """package com.acme.notifications;

import java.util.Set;

/** Notification preferences - interface per ADR-NOTIFY (implementation pending). */
public class PreferenceService {
    public static final Set<String> CRITICAL_CATEGORIES = Set.of("security", "account_recovery");
    public static final Set<String> OPTIONAL_CATEGORIES = Set.of("marketing", "product_updates", "social");

    public void setOptionalEnabled(String actor, String userId, boolean enabled) {
        throw new UnsupportedOperationException();
    }

    public boolean shouldDeliver(String userId, String category) {
        throw new UnsupportedOperationException();
    }
}
"""

IMPLEMENTATION = """package com.acme.notifications;

import java.util.HashMap;
import java.util.Map;
import java.util.Set;

/** Notification preferences (ADR-NOTIFY: extend existing preference storage). */
public class PreferenceService {
    public static final Set<String> CRITICAL_CATEGORIES = Set.of("security", "account_recovery");
    public static final Set<String> OPTIONAL_CATEGORIES = Set.of("marketing", "product_updates", "social");

    private final Map<String, Boolean> optional = new HashMap<>();

    public void setOptionalEnabled(String actor, String userId, boolean enabled) {
        if (!actor.equals(userId)) throw new PermissionDenied(actor + " cannot change " + userId);
        optional.put(userId, enabled);
    }

    public boolean shouldDeliver(String userId, String category) {
        if (CRITICAL_CATEGORIES.contains(category)) return true;
        if (!OPTIONAL_CATEGORIES.contains(category)) throw new UnknownCategory(category);
        return optional.getOrDefault(userId, true);
    }
}
"""

EXCEPTIONS = {
    f"{PKG}/PermissionDenied.java": "package com.acme.notifications;\n\npublic class PermissionDenied extends "
                                    "RuntimeException {\n    public PermissionDenied(String m) { super(m); }\n}\n",
    f"{PKG}/UnknownCategory.java": "package com.acme.notifications;\n\npublic class UnknownCategory extends "
                                   "RuntimeException {\n    public UnknownCategory(String c) { super(c); }\n}\n",
}


def _test(name: str, given: str, checks: List[str]) -> str:
    directives = "\n".join(f'    // @check {SERVICE} contains "{c}"' for c in checks)
    return (f"package com.acme.notifications.acceptance;\n\n"
            f"import com.acme.notifications.PreferenceService;\n"
            f"import org.junit.jupiter.api.Test;\n\n"
            f"class {name} {{\n    // {given}\n{directives}\n"
            f"    @Test\n    void behaves_as_the_owner_decided() {{ /* executed by the build */ }}\n}}\n")


UAT_TESTS = {
    "UAT-001": ("uat/com/acme/notifications/acceptance/OptionalSuppressedTest.java", "UAT",
                _test("OptionalSuppressedTest", "SC-1", ["return optional.getOrDefault(userId, true);"])),
    "UAT-002": ("uat/com/acme/notifications/acceptance/CriticalAlwaysDeliveredTest.java", "UAT",
                _test("CriticalAlwaysDeliveredTest", "SC-2", ["if (CRITICAL_CATEGORIES.contains(category)) return true;"])),
    "UAT-003": ("uat/com/acme/notifications/acceptance/OnlyOwnerChangesPreferencesTest.java", "NEGATIVE",
                _test("OnlyOwnerChangesPreferencesTest", "SC-3", ["if (!actor.equals(userId)) throw new PermissionDenied"])),
    "UAT-004": ("uat/com/acme/notifications/acceptance/UnknownCategoryRejectedTest.java", "BOUNDARY",
                _test("UnknownCategoryRejectedTest", "SC-4", ["throw new UnknownCategory(category);"])),
}

COVERAGE_TEST = ("tests/coverage/com/acme/notifications/coverage/PreferenceDefaultsTest.java",
                 _test("PreferenceDefaultsTest", "new users receive optional notifications by default",
                       ["getOrDefault(userId, true)", "optional.put(userId, enabled);"]))


def _clarify(inputs: Dict[str, Any]) -> Dict[str, Any]:
    out = ex._clarify(inputs)
    for o in out["observations"]:
        o["source_uri"] = o["source_uri"].replace("repo://src/legacy_email_prefs.py",
                                                  f"repo://{PKG}/LegacyEmailPrefs.java")
    return out


def _design(inputs: Dict[str, Any]) -> Dict[str, Any]:
    out = ex._design(inputs)
    return {**out, "domain_interface": ["com.acme.notifications"],
            "api_or_data_contracts": [
                "com.acme.notifications.PreferenceService.setOptionalEnabled(actor, userId, enabled) throws "
                "PermissionDenied unless actor equals userId",
                "com.acme.notifications.PreferenceService.shouldDeliver(userId, category) -> boolean; throws UnknownCategory"],
            "interface_files": [{"path": SERVICE, "content": INTERFACE},
                                *({"path": p, "content": c} for p, c in EXCEPTIONS.items())]}


def _tests(inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {"scenarios": ex._bindings(inputs),
            "tests": [{"test_id": tid, "path": path, "kind": kind, "content": content}
                      for tid, (path, kind, content) in UAT_TESTS.items()]}


def _implement(inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {"files": [{"path": SERVICE, "content": IMPLEMENTATION}],
            "notes": "Critical categories bypass the preference; unknown categories fail closed."}


def _coverage(inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {"tests": [{"path": COVERAGE_TEST[0], "content": COVERAGE_TEST[1]}],
            "rationale": "Defaults and per-user storage"}


def _review(inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {"findings": [{"finding_id": "F-1", "severity": "ADVISORY", "file": SERVICE,
                          "summary": "Preferences are held in memory; persistence needs a follow-up"}]}


def internal_tests(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A test agent that reads private state by reflection instead of going through the domain interface."""
    out = _tests(inputs)
    path, kind, content = UAT_TESTS["UAT-001"]
    peek = ('    // reads the private map directly\n'
            '    Object state = PreferenceService.class.getDeclaredField("optional");\n')
    out["tests"][0] = {"test_id": "UAT-001", "path": path, "kind": kind,
                       "content": content.replace("    @Test", peek + "    @Test", 1)}
    return out


def model() -> ScriptedModel:
    return ScriptedModel({**ex.SCRIPTS,
                          ("requirements_agent", "clarify"): _clarify,
                          ("architecture_agent", "design"): _design,
                          ("acceptance_test_agent", "author_tests"): _tests,
                          ("implementation_agent", "implement"): _implement,
                          ("test_coverage_agent", "add_tests"): _coverage,
                          ("code_review_agent", "review"): _review})


def engineering(stack: StackProfile = JAVA_SPRING_SIMULATED) -> Engineering:
    return Engineering(stack, dict(INSTRUCTIONS), {**BUILT_IN, stack.stack_id: stack})


def desk(waivers: List[str] = ("MUTATION",)) -> ScriptedDesk:
    return ScriptedDesk(answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS, scenarios=ex.DOMAIN_SCENARIOS,
                        stack_choice={"stack_id": JAVA_SPRING_SIMULATED.stack_id, "quality_waivers": list(waivers)})
