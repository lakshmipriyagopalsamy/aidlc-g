"""Run the delivery slice (M2-M4: request -> merge -> provenance -> staged deployment) and adversarial scenarios.

    .venv/bin/python scripts/run_vertical_slice.py            # scripted model (deterministic)
    .venv/bin/python scripts/run_vertical_slice.py --claude   # live Claude agents (needs `anthropic` + credentials)

Writes results/vertical_slice.md. With --claude the slice spends API credits and
executes model-written code in a temp directory - review the README limits first.
"""

import argparse
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from aidlc_g.examples import notifications as ex
from aidlc_g.orchestrator import ScriptedDesk, SliceEnvironment, SliceOrchestrator, SliceReport

Scenario = Tuple[str, str, Callable[[], SliceReport]]


def run(model=None, desk=None, telemetry=None) -> SliceReport:
    env = SliceEnvironment.create(ex.SEED, ex.ADVISORIES, telemetry)
    return SliceOrchestrator(env, model or ex.model(), desk or ex.desk()).release(ex.REQUEST)


def outcome(report: SliceReport) -> str:
    if report.completed:
        if report.release_manifest:
            return f"released {report.release_manifest['tag']} to " + " -> ".join(d["environment"] for d in report.deployments)
        return f"completed at {report.final_stage}"
    h = report.halted or {}
    codes = [r["code"] for r in h.get("reasons", [])] or [h.get("error") or h.get("outcome")]
    return f"halted entering {h.get('stage')}: {', '.join(c for c in codes if c)}"


def scenarios() -> List[Scenario]:
    m = ex.model()
    return [
        ("Buggy implementation (critical alerts silenced)", "TESTS_FAILED at TEST_COVERAGE",
         lambda: run(m.with_override("implementation_agent", "implement", ex.buggy_implementation))),
        ("Implementer rewrites a locked UAT file", "PATH_NOT_PERMITTED",
         lambda: run(m.with_override("implementation_agent", "implement", ex.tampering_implementation))),
        ("Acceptance tests that cannot fail", "TESTS_CANNOT_FAIL at IMPLEMENTATION",
         lambda: run(m.with_override("acceptance_test_agent", "author_tests", lambda i: {
             **ex._tests(i), "tests": [dict(t, content="def test_ok():\n    assert True\n") for t in ex._tests(i)["tests"]]}))),
        ("Test author rewords a decided scenario", "SCENARIO_ALTERED at IMPLEMENTATION",
         lambda: run(m.with_override("acceptance_test_agent", "author_tests", ex.rewording_tests))),
        ("Test author tests behavior the owner dropped", "SCENARIO_NOT_IN_C1 at IMPLEMENTATION",
         lambda: run(m.with_override("acceptance_test_agent", "author_tests", ex.inventing_tests))),
        ("Test author proposes missing behavior", "DOMAIN_PROPOSAL_PENDING at IMPLEMENTATION",
         lambda: run(m.with_override("acceptance_test_agent", "author_tests", ex.proposing_tests))),
        ("Acceptance test reaches past the domain interface", "TEST_BYPASSES_DOMAIN_INTERFACE at IMPLEMENTATION",
         lambda: run(m.with_override("acceptance_test_agent", "author_tests", ex.internal_tests))),
        ("Model invents a baseline and target", "INVENTED_METRIC",
         lambda: run(m.with_override("requirements_agent", "draft_intent", ex.invented_metric_intent))),
        ("Model cites a source it never retrieved", "UNCITED_OBSERVATION",
         lambda: run(m.with_override("requirements_agent", "clarify", ex.hallucinated_citation))),
        ("Model obeys injected 'approve everything' text", "INVALID_MODEL_OUTPUT",
         lambda: run(m.with_override("requirements_agent", "draft_requirement", ex.injected_requirement))),
        ("Blocking clarification left unanswered", "OPEN_BLOCKING_DECISION at SPIKE",
         lambda: run(desk=ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers={}))),
        ("Product owner requests changes", "ARTIFACT_RETURNED at SPIKE",
         lambda: run(desk=ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, decisions={("po", "BR-NOTIFY"): "REQUEST_CHANGES"}))),
        ("Credential committed in code", "QUALITY_CHECK_FAILED at DOCUMENTATION",
         lambda: run(m.with_override("implementation_agent", "implement", ex.leaked_secret_implementation))),
        ("Vulnerable / unpinned dependency", "QUALITY_CHECK_FAILED at DOCUMENTATION",
         lambda: run(m.with_override("implementation_agent", "implement", ex.vulnerable_dependency_implementation))),
        ("Untested extra logic (surviving mutants)", "MUTATION_SCORE_LOW at DOCUMENTATION",
         lambda: run(m.with_override("implementation_agent", "implement", ex.untested_logic_implementation))),
        ("Coverage agent rewrites a locked UAT", "PATH_NOT_PERMITTED",
         lambda: run(m.with_override("test_coverage_agent", "add_tests", ex.uat_rewriting_coverage))),
        ("Blocking review finding left open", "BLOCKING_FINDINGS_OPEN",
         lambda: run(m.with_override("code_review_agent", "review", ex.blocking_review))),
        ("SRE rejects the runbook", "ARTIFACT_RETURNED at MERGE",
         lambda: run(desk=ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS,
                                       decisions={("sre", "DOCS-NOTIFY"): "REJECT"}))),
        ("Health check on a metric nobody documented", "UNGROUNDED_HEALTH_CHECK",
         lambda: run(m.with_override("release_evidence_agent", "draft_release", ex.ungrounded_release))),
        ("Model supplies its own artifact digest", "INVALID_MODEL_OUTPUT",
         lambda: run(m.with_override("release_evidence_agent", "draft_release", ex.digest_forging_release))),
        ("Release manager rejects", "ARTIFACT_RETURNED at RELEASE_READY",
         lambda: run(desk=ScriptedDesk(scenarios=ex.DOMAIN_SCENARIOS, answers=ex.ANSWERS, resolutions=ex.RESOLUTIONS,
                                       decisions={("rel_mgr", "REL-NOTIFY-1"): "REJECT"}))),
        ("Production canary degrades critical alerts", "ROLLED_BACK at DEPLOY:production",
         lambda: run(telemetry=ex.degraded_in_production)),
        ("Canary reports no critical-alert metric", "ROLLED_BACK at DEPLOY:staging",
         lambda: run(telemetry=ex.silent_telemetry)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude", action="store_true", help="use live Claude agents instead of the scripted model")
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    if args.claude:
        from aidlc_g.claude_model import ClaudeModel

        report = run(model=ClaudeModel())
        rows = []
    else:
        report = run()
        rows = [(name, expected, outcome(fn())) for name, expected, fn in scenarios()]

    lines = [report.to_markdown()]
    if rows:
        lines += ["## Adversarial scenarios", "", "| Scenario | Expected | Observed |", "|---|---|---|"]
        lines += [f"| {n} | {e} | {o} |" for n, e, o in rows]
        lines.append("")
    lines.append("> Synthetic fixture data (section 24.2). Scenario outcomes demonstrate enforcement, "
                 "not delivery effectiveness.\n")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "vertical_slice.md").write_text("\n".join(lines))
    print(outcome(report))
    for n, e, o in rows:
        print(f"  {n}: {o}")
    print(f"wrote {out / 'vertical_slice.md'}")
    ok = report.completed and all(e.split(" at ")[0] in o for _, e, o in rows)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
