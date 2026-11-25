# AIDLC-G M5 incident-support study

30 scenarios per cause class × 5 classes × 6 conditions; seed 2026; reasoner: per-condition default.

> Synthetic, seeded simulations with predeclared ground truth. The default reasoner is a deterministic rule-based stand-in: these results show how evidence availability changes triage under a fixed reasoner and validate the harness. They are not evidence about LLM capability or real MTTR (§12.2). The simulator and the reference reasoner share an author, so high full-condition accuracy is by construction; the informative signal is the difference between conditions. Simulated time to restore = base by class {'RELEASE_REGRESSION': 20, 'CONFIG_CHANGE': 20, 'PROVIDER_OUTAGE': 35, 'CAPACITY': 30} + 25 per false lead + 10 if the first step is not the runbook's step.

## All classes

| Condition | Top-1 correct (95% CI) | False leads | First step useful | Change assessed | Review items / words | Sim. restore (median min) | Rejected |
|---|---|---|---|---|---|---|---|
| full | 149/150 = 0.99 (0.96–1.00) | 0.01 | 1.00 | 1.00 | 12.2 / 95 | 30 | 0 |
| no_release_evidence | 119/150 = 0.79 (0.72–0.85) | 0.21 | 1.00 | 0.80 | 10.4 / 92 | 35 | 0 |
| no_runbooks | 149/150 = 0.99 (0.96–1.00) | 0.01 | 0.00 | 1.00 | 11.8 / 89 | 40 | 0 |
| no_config_history | 119/150 = 0.79 (0.72–0.85) | 0.21 | 0.80 | 1.00 | 10.6 / 96 | 35 | 0 |
| no_conversation_history | 149/150 = 0.99 (0.96–1.00) | 0.01 | 1.00 | 1.00 | 12.4 / 95 | 30 | 0 |
| naive_baseline | 30/150 = 0.20 (0.14–0.27) | 0.80 | 0.00 | 0.00 | 3.0 / 24 | 65 | 0 |

## Top-1 accuracy by cause class

| Condition | RELEASE_REGRESSION | CONFIG_CHANGE | PROVIDER_OUTAGE | CAPACITY | RED_HERRING |
|---|---|---|---|---|---|
| full | 30/30 | 30/30 | 30/30 | 30/30 | 29/30 |
| no_release_evidence | 0/30 | 30/30 | 30/30 | 30/30 | 29/30 |
| no_runbooks | 30/30 | 30/30 | 30/30 | 30/30 | 29/30 |
| no_config_history | 30/30 | 0/30 | 30/30 | 30/30 | 29/30 |
| no_conversation_history | 30/30 | 30/30 | 30/30 | 30/30 | 29/30 |
| naive_baseline | 30/30 | 0/30 | 0/30 | 0/30 | 0/30 |

## Incident drill on the released slice

- Incident `INC-DRILL-7`: PACKET → DECISION → RESOLVED → POSTMORTEM
- Top hypothesis: Degradation began 2 min after REL-NOTIFY-1 was deployed (HIGH)
- First step: compare_release_manifest production
- Rollback: ROLLED_BACK; production now runs `d16da03527a9`
- Packet recorded by `ops_agent`; decision, resolution and postmortem by humans (sre, sre, eng_lead)
