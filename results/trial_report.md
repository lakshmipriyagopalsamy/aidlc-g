# AIDLC-G gate trial report

Generated 2026-11-10T04:07:52+00:00 · aidlc-g 0.8.0 · Python 3.9.6 on arm64 · 20 runs per trial

> Data provenance: synthetic, author-created smoke scenarios (not empirical observations). These numbers show the gate's predeclared oracle holds on seeded scenarios; they are not evidence of lifecycle effectiveness.

| Metric | Value |
|---|---|
| Violation detection recall | 340/340 = 1.000 |
| False rejection rate | 0/60 = 0.000 |
| Transition latency, median / p95 (IQR) | 0.52 ms / 0.91 ms (0.29–0.66; n=800) |
| Full release gate latency, median / p95 (IQR) | 0.90 ms / 0.96 ms (0.88–0.93; n=100) |
| Environment | Apple M5, Python 3.9.6, SQLite 3.51.0 (in memory); 5 warm-up runs discarded |

| Trial | Scenario | Expected | Observed | Pass |
|---|---|---|---|---|
| G01 | Unauthorized product approval | DENY | DENY×20 | 20/20 |
| G02 | C1 changes after approval | INVALIDATE | INVALIDATE×20 | 20/20 |
| G02R | Targeted re-approval after C1 change (positive control) | ALLOW | ALLOW×20 | 20/20 |
| G03 | Implementation actor replaces UAT | DENY | DENY×20 | 20/20 |
| G04 | Stale test signature or wrong manifest version | DENY | DENY×20 | 20/20 |
| G05 | Required UAT absent | DENY | DENY×20 | 20/20 |
| G06 | Tampered CI signature / unknown key | DENY | DENY×20 | 20/20 |
| G07 | Implementer performs own reviewer approval | DENY | DENY×20 | 20/20 |
| G08 | All authenticated/current evidence valid | ALLOW | ALLOW×20 | 20/20 |
| G09 | Replay old valid attestation | DENY | DENY×20 | 20/20 |
| G10 | Concurrent duplicate transition | IDEMPOTENT_SINGLE_COMMIT | IDEMPOTENT_SINGLE_COMMIT×20 | 20/20 |
| G11 | Required domain scenario has no executable test | DENY | DENY×20 | 20/20 |
| G12 | Release doc/runbook evidence missing | DENY | DENY×20 | 20/20 |
| G13 | Agent records itself as owner of domain behavior | DENY | DENY×20 | 20/20 |
| G14 | Test author rewords an approved domain scenario | DENY | DENY×20 | 20/20 |
| G15 | Acceptance test bypasses the domain interface | DENY | DENY×20 | 20/20 |
| G16 | Agent records itself as the decider of the technology stack | DENY | DENY×20 | 20/20 |
| G17 | CI runs a different stack than the team approved | DENY | DENY×20 | 20/20 |
| G18 | Stack cannot run a required quality check and nobody waived it | DENY | DENY×20 | 20/20 |
| G18R | Named waiver of a check the stack cannot run (positive control) | ALLOW | ALLOW×20 | 20/20 |

## Evidence per trial (last run)

**G01** — ok  approvals refused: ['ROLE_NOT_PERMITTED', 'HUMAN_APPROVAL_REQUIRED', 'HUMAN_APPROVAL_REQUIRED']; ok  transition PAUSE_FOR_HUMAN_DECISION ['APPROVAL_MISSING']

**G02** — ok  C1 v1 SUPERSEDED; ok  downstream C2 v1 INVALIDATED; ok  impact list ['TR-NOTIFY@v1']; ok  stale refs BLOCK_WITH_REASONS ['STALE_VERSION', 'EVIDENCE_INVALIDATED']; ok  unapproved v2 BLOCK_WITH_REASONS ['EVIDENCE_INVALIDATED', 'APPROVAL_MISSING']

**G02R** — ALLOW []

**G03** — ok  manifest rewrite refused: ROLE_NOT_PERMITTED; ok  BLOCK_WITH_REASONS ['TEST_INTEGRITY_VIOLATION']

**G04** — ok  wrong-manifest attestation: MANIFEST_BINDING_MISMATCH; ok  stale build BLOCK_WITH_REASONS ['EVIDENCE_INVALIDATED']

**G05** — BLOCK_WITH_REASONS ['REQUIRED_TEST_RESULT_MISSING']

**G06** — ok  modified payload: INVALID_SIGNATURE; ok  unregistered key: INVALID_SIGNATURE; ok  unknown runner: UNKNOWN_RUNNER; ok  malformed signature: INVALID_SIGNATURE; ok  no accepted evidence -> BLOCK_WITH_REASONS ['MISSING_EVIDENCE']

**G07** — ok  self review: SEPARATION_OF_DUTIES; ok  PAUSE_FOR_HUMAN_DECISION ['REVIEW_MISSING']

**G08** — release REL-NOTIFY-1 -> sha256:b0caab47dd9c…

**G09** — ok  replay: REPLAY_DETECTED; ok  borrowed evidence BLOCK_WITH_REASONS ['MISSING_EVIDENCE']

**G10** — ok  same key -> 1 decision(s); ok  exactly one evaluation committed; ok  distinct keys -> 1 ALLOW; ok  final stage SPIKE

**G11** — BLOCK_WITH_REASONS ['SCENARIO_NOT_COVERED']

**G12** — ok  docs without runbook: SCHEMA_VIOLATION; ok  BLOCK_WITH_REASONS ['MISSING_EVIDENCE', 'MISSING_EVIDENCE']

**G13** — BLOCK_WITH_REASONS ['SCENARIO_OWNER_INVALID']

**G14** — BLOCK_WITH_REASONS ['SCENARIO_ALTERED']

**G15** — BLOCK_WITH_REASONS ['TEST_BYPASSES_DOMAIN_INTERFACE']

**G16** — BLOCK_WITH_REASONS ['STACK_OWNER_INVALID']

**G17** — BLOCK_WITH_REASONS ['STACK_MISMATCH']

**G18** — BLOCK_WITH_REASONS ['QUALITY_CHECK_NOT_RUN']

**G18R** — ALLOW []; MUTATION recorded as WAIVED
