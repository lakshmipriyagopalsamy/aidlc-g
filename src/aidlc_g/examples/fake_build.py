"""Stand-in for Maven/Gradle/dotnet in tests and the JVM demo. It does NOT compile or run Java.

    python fake_build.py <Name1,Name2,...> <report_dir>

For each named test class it finds <Name>.java (or .kt/.cs) under uat/ or
tests/coverage/ in the current directory and reads directive comments:

    // @check src/main/java/com/acme/X.java contains "some text"

Each directive is one test case: it passes when that file exists and contains
the text. Results are written as JUnit XML (TEST-<package>.<Name>.xml), the
same format Surefire, Gradle and the dotnet JUnit logger produce, so the
trusted runner's parser and the gate are exercised exactly as with a real
build. Real stacks need the real build tool installed where the runner runs.
"""

import re
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

CHECK = re.compile(r'@check\s+(\S+)\s+contains\s+"([^"]*)"')
PACKAGE = re.compile(r"^\s*(?:package|namespace)\s+([\w.]+)", re.M)


def main(names: str, report_dir: str) -> int:
    out = Path(report_dir)
    out.mkdir(parents=True, exist_ok=True)
    root = Path.cwd()
    for name in filter(None, names.split(",")):
        files = [p for base in ("uat", "tests/coverage") for p in (root / base).rglob(f"{name}.*") if p.is_file()]
        if len(files) != 1:
            continue  # nothing written: the runner reports ERROR, never a pass
        text = files[0].read_text()
        package = (PACKAGE.findall(text) or ["default"])[0]
        cases = []
        for n, (path, needle) in enumerate(CHECK.findall(text), 1):
            target = root / path
            ok = target.is_file() and needle in target.read_text()
            body = "" if ok else f"<failure message={quoteattr(f'{path} lacks {needle!r}')}/>"
            cases.append(f'<testcase classname="{package}.{name}" name="check_{n}">{body}</testcase>')
        failures = sum("<failure" in c for c in cases)
        (out / f"TEST-{package}.{name}.xml").write_text(
            f'<?xml version="1.0"?>\n<testsuite name="{package}.{name}" tests="{len(cases)}" failures="{failures}">'
            + "".join(cases) + "</testsuite>\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
