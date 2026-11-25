# Bounded model checking

Bounds: {'versions_per_artifact': 2, 'attestation_ids': 2, 'racing_requests': 2}

| Model | Properties hold | Reachable states | Transitions |
|---|---|---|---|
| approvals (P1-P3) | yes | 80,601 | 158,400 |
| attestations (P4) | yes | 22 | 22 |
| race (P5) | yes | 4 | 4 |
| rollback (P6) | yes | 3 | 2 |

| Mutant (one mechanism removed) | Counterexample found | Violation | Trace length |
|---|---|---|---|
| no invalidation on change | yes | P2 approval of C2 by eng still counts after an upstream change | 2 |
| no role or human check on approval | yes | P3 approval of C1 by bot (author or agent) counts | 1 |
| author may approve own artifact | yes | P3 approval of C3 by author (author or agent) counts | 1 |
| replayed attestation accepted | yes | P4 an attestation id was accepted twice | 3 |
| attestation not bound to current manifest | yes | P4 an attestation was accepted for a build bound to an old manifest | 3 |
| transition commits without compare-and-set | yes | P5 the same transition was admitted twice | 4 |
| agent may request rollback | yes | P6 rollback without a human decision | 1 |
