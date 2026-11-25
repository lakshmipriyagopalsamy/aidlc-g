# Dependency-aware invalidation study

- Exact invalidation (no missed, no extra): 6/6 changes
- Stale approvals still counting after the change: 0 (AIDLC-G) vs 33 (no invalidation)
- Human re-approvals implied: 42 (targeted) vs 60 (restart all)
- Races between a design change and a transition: 40 runs, transition first 20, change first 20; admitted on invalidated evidence 0; stage re-admitted afterwards 0

| Changed | Invalidated (gate) | Missed | Extra | Stale approvals: AIDLC-G / none | Re-approvals: targeted / restart | Rebuild |
|---|---|---|---|---|---|---|
| C0 | AC-NOTIFY, BR-NOTIFY, DOCS-NOTIFY, REL-NOTIFY, STORIES-NOTIFY, TR-NOTIFY, build:BLD-1, build:BLD-STUB-1 | — | — | 0 / 9 | 10 / 10 | yes |
| C1 | AC-NOTIFY, DOCS-NOTIFY, REL-NOTIFY, STORIES-NOTIFY, TR-NOTIFY, build:BLD-1, build:BLD-STUB-1 | — | — | 0 / 8 | 9 / 10 | yes |
| C2 | AC-NOTIFY, DOCS-NOTIFY, REL-NOTIFY, STORIES-NOTIFY, build:BLD-1, build:BLD-STUB-1 | — | — | 0 / 7 | 8 / 10 | yes |
| STORIES | AC-NOTIFY, DOCS-NOTIFY, REL-NOTIFY, build:BLD-1, build:BLD-STUB-1 | — | — | 0 / 5 | 7 / 10 | yes |
| C3 | DOCS-NOTIFY, REL-NOTIFY, build:BLD-1, build:BLD-STUB-1 | — | — | 0 / 3 | 5 / 10 | yes |
| DOCS | REL-NOTIFY | — | — | 0 / 1 | 3 / 10 | no |
