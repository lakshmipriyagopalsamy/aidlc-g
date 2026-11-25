# Scenario-binding study

- Semantic changes caught: 24/24
- Presentation-only changes also blocked: 16/16
- Structural and ownership cases stopped as expected: 5/5
- Not detected by the gate: test bindings swapped between two scenarios

| Family | Change | Field | Caught (of 4 scenarios) |
|---|---|---|---|
| semantic | negation toggled | then | 4/4 |
| semantic | outcome weakened | then | 4/4 |
| semantic | condition dropped | given | 4/4 |
| semantic | actor swapped | given | 4/4 |
| semantic | scope widened | when | 4/4 |
| semantic | outcome replaced | then | 4/4 |
| presentation | double space | when | 4/4 |
| presentation | trailing period | then | 4/4 |
| presentation | capitalized | given | 4/4 |
| presentation | typographic apostrophe or no-break space | given | 4/4 |

| Structural case | Gate decision | Reason codes |
|---|---|---|
| scenario dropped from C3 | BLOCK_WITH_REASONS | SCENARIO_NOT_COVERED |
| scenario invented in C3 | BLOCK_WITH_REASONS | SCENARIO_NOT_IN_C1 |
| missing behavior proposed | PAUSE_FOR_HUMAN_DECISION | DOMAIN_PROPOSAL_PENDING |
| test bindings swapped between two scenarios | ALLOW | — |
| agent recorded as scenario owner | BLOCK_WITH_REASONS | SCENARIO_OWNER_INVALID |
| no negative or boundary scenario | BLOCK_WITH_REASONS | NEGATIVE_SCENARIO_MISSING |
