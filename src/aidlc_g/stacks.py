"""Technology stacks the team can choose, and how the trusted runner treats each.

The team, through its engineering lead, picks the stack and writes instruction
files (coding standards, framework conventions). Agents receive both as
constraints. C2 binds the choice by digest, and every attestation names the
stack the runner executed, so the gate can tell when anyone switched it.

Layout rule for every stack (the path ACLs depend on it):
    src/              product code, written by the implementation agent
    uat/              locked acceptance tests, written by the acceptance-test agent
    tests/coverage/   extra tests, written by the coverage agent
JVM and .NET builds must therefore list uat/ and tests/coverage/ as test
source roots; each profile's build_notes say how.

What the runner can do per stack:
    python   pytest, import-boundary check (AST), mutation testing, requirements scan
    java/kotlin/csharp
             the profile's build command with JUnit XML reports, import-boundary
             check (static, from import/using and package/namespace lines),
             pom.xml / Gradle / NuGet dependency scan. Mutation testing is NOT
             run: the C2 stack decision must waive it by name, or the gate blocks.
"""

import fnmatch
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .canonical import digest, file_digest
from .contracts import InstructionRef, StackProfile

DYNAMIC_IMPORT = "<dynamic>"

_JUNIT_RUN = ["Each test file is a class whose name equals the file name.",
              "Acceptance tests live under uat/, extra tests under tests/coverage/, product code under src/."]

PYTHON_PYTEST = StackProfile(
    stack_id="python-pytest", language="python", framework="Python 3", build_tool="pytest",
    test_file_glob="test_*.py", report_glob="report.xml", timeout_seconds=120,
    dependency_manifest="requirements",
    build_notes=["Product code is importable from src/ (PYTHONPATH=src).", "Pin dependencies in src/requirements*.txt."],
)

JAVA_SPRING_MAVEN = StackProfile(
    stack_id="java-spring-maven", language="java", framework="Spring Boot 3", build_tool="Maven",
    test_file_glob="*Test.java",
    test_command=["mvn", "-q", "-B", "test", "-Dtest={names}", "-Dsurefire.failIfNoSpecifiedTests=false"],
    report_glob="target/surefire-reports/*.xml", dependency_manifest="maven",
    build_notes=[*_JUNIT_RUN, "Product code stays in src/main/java; pom.xml adds uat/ and tests/coverage/ as test "
                 "source roots with build-helper-maven-plugin.", "Tests use JUnit 5."],
)

JAVA_MICRONAUT_GRADLE = StackProfile(
    stack_id="java-micronaut-gradle", language="java", framework="Micronaut 4", build_tool="Gradle",
    test_file_glob="*Test.java", test_command=["gradle", "-q", "test", "{each}"], each_args=["--tests", "{name}"],
    report_glob="build/test-results/test/*.xml", dependency_manifest="gradle",
    build_notes=[*_JUNIT_RUN, "Product code stays in src/main/java; build.gradle sets "
                 "sourceSets.test.java.srcDirs = ['uat', 'tests/coverage'].", "Tests use JUnit 5 with @MicronautTest."],
)

KOTLIN_SPRING_GRADLE = StackProfile(
    stack_id="kotlin-spring-gradle", language="kotlin", framework="Spring Boot 3 (Kotlin)", build_tool="Gradle",
    test_file_glob="*Test.kt", test_command=["gradle", "-q", "test", "{each}"], each_args=["--tests", "{name}"],
    report_glob="build/test-results/test/*.xml", dependency_manifest="gradle",
    build_notes=[*_JUNIT_RUN, "Product code stays in src/main/kotlin; build.gradle.kts sets the test source dirs to "
                 "uat and tests/coverage.", "Tests use JUnit 5."],
)

DOTNET_ASPNET_XUNIT = StackProfile(
    stack_id="dotnet-aspnet-xunit", language="csharp", framework="ASP.NET Core 8", build_tool="dotnet",
    test_file_glob="*Tests.cs",
    test_command=["dotnet", "test", "--nologo", "--filter", "{names}", "--logger", "junit;LogFilePath=TestResults/{{assembly}}.xml"],
    name_format="FullyQualifiedName~{name}", name_separator="|",
    report_glob="**/TestResults/*.xml", dependency_manifest="nuget",
    build_notes=["Each test file holds one test class named after the file.",
                 "src/ holds the product projects; uat/ and tests/coverage/ each hold a test project that references them.",
                 "Test projects reference JunitXml.TestLogger so results are written as JUnit XML.", "Tests use xUnit."],
)

BUILT_IN: Dict[str, StackProfile] = {p.stack_id: p for p in (
    PYTHON_PYTEST, JAVA_SPRING_MAVEN, JAVA_MICRONAUT_GRADLE, KOTLIN_SPRING_GRADLE, DOTNET_ASPNET_XUNIT)}
DEFAULT_STACK = PYTHON_PYTEST


def profile_digest(profile: StackProfile) -> str:
    return digest(profile.model_dump(mode="json"))


DEFAULT_STACK_DIGEST = profile_digest(DEFAULT_STACK)

# Per-stack capability of the trusted runner, shown to the person choosing the stack.
UNSUPPORTED_CHECKS = {"python": (), "java": ("MUTATION",), "kotlin": ("MUTATION",), "csharp": ("MUTATION",)}


def unsupported_checks(profile: StackProfile) -> Tuple[str, ...]:
    return UNSUPPORTED_CHECKS[profile.language]


# ---------------------------------------------------------- instructions


def read_instructions(directory: Optional[Path]) -> Dict[str, str]:
    """Team instruction files (*.md, *.txt) keyed by file name. Missing directory: none."""
    if directory is None or not Path(directory).is_dir():
        return {}
    return {p.name: p.read_text() for p in sorted(Path(directory).iterdir())
            if p.is_file() and p.suffix.lower() in (".md", ".txt")}


def instruction_refs(instructions: Mapping[str, str]) -> List[InstructionRef]:
    return [InstructionRef(path=f"instructions/{name}", digest=file_digest(text.encode()))
            for name, text in sorted(instructions.items())]


def instructions_digest(instructions: Mapping[str, str]) -> str:
    return digest([r.model_dump(mode="json") for r in instruction_refs(instructions)])


# ----------------------------------------------------------- test files


def is_test_file(profile: StackProfile, path: str) -> bool:
    return fnmatch.fnmatch(path.rsplit("/", 1)[-1], profile.test_file_glob)


def test_name(path: str) -> str:
    """The class (or module) a test file holds: its file name without the extension."""
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def expand_command(profile: StackProfile, names: List[str]) -> List[str]:
    formatted = [profile.name_format.format(name=n) for n in names]
    argv: List[str] = []
    for arg in profile.test_command:
        if arg == "{each}":
            for name in formatted:
                argv.extend(a.replace("{name}", name) for a in profile.each_args)
        else:
            argv.append(arg.replace("{names}", profile.name_separator.join(formatted)).replace("{{", "{").replace("}}", "}"))
    return argv


# ------------------------------------------------- static import analysis

_JAVA_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;?", re.M)
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?(\w+(?:\.\w+)*(?:\.\*)?)\s*;?", re.M)
_JAVA_REFLECTION = re.compile(r"Class\.forName|getDeclared(?:Field|Method|Constructor)|setAccessible\s*\(")
_CS_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
_CS_USING = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?!\w+\s*=)([\w.]+)\s*;", re.M)
_CS_REFLECTION = re.compile(r"BindingFlags\.NonPublic|Activator\.CreateInstance|Type\.GetType\s*\(|InternalsVisibleTo")


def _package_of(name: str) -> str:
    """com.acme.billing.Invoice.method -> com.acme.billing (drop the class and anything after it)."""
    parts = []
    for part in name.split("."):
        if part == "*" or part[:1].isupper():
            break
        parts.append(part)
    return ".".join(parts)


def _project_namespaces(tree: Mapping[str, bytes], pattern: "re.Pattern[str]", suffixes: Tuple[str, ...]) -> set:
    found = set()
    for path, content in tree.items():
        if path.startswith("src/") and path.endswith(suffixes):
            found.update(pattern.findall(content.decode("utf-8", "replace")))
    return found


def jvm_imports(tree: Mapping[str, bytes], path: str) -> List[str]:
    """Project packages a Java or Kotlin test reaches: its imports and its own package declaration."""
    project = _project_namespaces(tree, _JAVA_PACKAGE, (".java", ".kt"))
    text = tree[path].decode("utf-8", "replace")
    found = {p for p in (_package_of(i) for i in _JAVA_IMPORT.findall(text)) if p in project}
    found.update(p for p in _JAVA_PACKAGE.findall(text) if p in project)  # same-package access needs no import
    if _JAVA_REFLECTION.search(text):
        found.add(DYNAMIC_IMPORT)
    return sorted(found)


def csharp_imports(tree: Mapping[str, bytes], path: str) -> List[str]:
    """Project namespaces a C# test reaches: its using directives, and its namespace and every enclosing one."""
    project = _project_namespaces(tree, _CS_NAMESPACE, (".cs",))
    text = tree[path].decode("utf-8", "replace")
    found = {u for u in _CS_USING.findall(text) if u in project}
    for ns in _CS_NAMESPACE.findall(text):
        parts = ns.split(".")
        found.update(".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in project)
    if _CS_REFLECTION.search(text):
        found.add(DYNAMIC_IMPORT)
    return sorted(found)


# ------------------------------------------------------ dependency scans

_POM_DEP = re.compile(r"<dependency>(.*?)</dependency>", re.S)
_POM_FIELD = re.compile(r"<(artifactId|version)>\s*([^<\s]+)\s*</\1>")
_GRADLE_DEP = re.compile(r"""["']([\w.-]+):([\w.-]+):([\w.+-]+)["']""")
_NUGET_DEP = re.compile(r"""<PackageReference\s+Include=["']([\w.-]+)["']\s+Version=["']([^"']+)["']""")


def scan_jvm_or_nuget(tree: Mapping[str, bytes], manifest: str,
                      advisories: Mapping[Tuple[str, str], str]) -> Tuple[int, List[str]]:
    """Declared dependencies with explicit versions, matched against the advisory feed by (name, version).

    Versions managed by a parent or BOM (no explicit version) are counted but not judged here."""
    names = {"maven": ("pom.xml",), "gradle": ("build.gradle", "build.gradle.kts"), "nuget": (".csproj",)}[manifest]
    count, findings = 0, []
    for path in sorted(p for p in tree if p.endswith(names) or p.rsplit("/", 1)[-1] in names):
        text = tree[path].decode("utf-8", "replace")
        if manifest == "maven":
            deps = []
            for block in _POM_DEP.findall(text):
                fields = dict(_POM_FIELD.findall(block))
                if "artifactId" in fields:
                    deps.append((fields["artifactId"], fields.get("version")))
        elif manifest == "gradle":
            deps = [(a, v) for _, a, v in _GRADLE_DEP.findall(text)]
        else:
            deps = _NUGET_DEP.findall(text)
        for name, version in deps:
            count += 1
            key = (name.lower(), version or "")
            if key in advisories:
                findings.append(f"{path}: {name} {version} {advisories[key]}")
    return count, findings
