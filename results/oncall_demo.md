# On-call buddy demo

> Synthetic data. Chat, identity and the runtime platform are simulated; the gate, the agent's evidence rules and the deployment service are the real implementation.

## Release regression

**bot:**

```
:rotating_light: SEV2 notifications (production): critical_alert_delivery_pct alert A-7. Ask me in this thread: "what is happening?"
```

**sre:** `what is happening?`

**bot:**

```
On it. Read-only investigation of notifications in production for alert A-7; history limited to channels you can see.
```

**bot:**

```
*Situation*
critical_alert_delivery_pct for notifications in production (SEV2) has been below baseline since minute 36; leading hypothesis: Degradation began 2 min after REL-NOTIFY-1 was deployed (HIGH confidence) (newest 0 min old)
*Likely cause (hypotheses, not a root cause)*
H1 [HIGH] Degradation began 2 min after REL-NOTIFY-1 was deployed. Confirm: Compare error signatures before and after the deployment against the release manifest. Evidence: deploy:D-2, metric:critical_alert_delivery_pct, log:L-0005, log:L-0006 (newest 0 min old)
*Release check*
MATCH: running artifact is the gated, tested release REL-NOTIFY-1 (tag notifications-v1.4.0, commit 35bc53d437b0)
At risk: SC-2 "critical alerts are never silenced" decided by po; locked tests UAT-002; watched by critical_alert_delivery_pct GTE 100 percent
*Recent changes*
deployment REL-LEGACY at minute 5  NOT through a gated release
configuration CFG-1 at minute 5  NOT through a gated release
configuration CFG-2 at minute 12  NOT through a gated release
deployment REL-NOTIFY-1 at minute 34
*Next checks (read-only)*
S1: compare_release_manifest production - Runbook step for H1 (Delivery failures after a release)
*Gaps*
no traces were retrieved
*You decide*
decide noisy receipt 43696458  |  decide understood H1 receipt 43696458  |  decide deeper H1 rollback receipt 43696458
Nothing changes in production without your decision. Escalation owner: sre.
```

**sre:** `decide deeper H1 rollback receipt 43696458 errors match the new release`

**bot:**

```
Recorded: NEEDS_DEEPER_ACTION by sre on investigation 43696458, action ROLLBACK. Rollback ROLLED_BACK; production now runs d16da03527a9.
```

## Configuration change

**bot:**

```
:rotating_light: SEV2 notifications (production): critical_alert_delivery_pct alert A-4. Ask me in this thread: "what is happening?"
```

**sre:** `what is happening?`

**bot:**

```
On it. Read-only investigation of notifications in production for alert A-4; history limited to channels you can see.
```

**bot:**

```
*Situation*
critical_alert_delivery_pct for notifications in production (SEV2) has been below baseline since minute 39; leading hypothesis: Degradation began 1 min after smtp.send_timeout_ms was changed (HIGH confidence) (newest 0 min old)
*Likely cause (hypotheses, not a root cause)*
H1 [HIGH] Degradation began 1 min after smtp.send_timeout_ms was changed. Confirm: Diff the configuration before and after the change and check it against the runbook's safe values. Evidence: config:CFG-3, metric:critical_alert_delivery_pct, log:L-0006 (newest 0 min old)
*Release check*
MATCH: running artifact is the gated, tested release REL-NOTIFY-1 (tag notifications-v1.4.0, commit 35bc53d437b0)
At risk: SC-2 "critical alerts are never silenced" decided by po; locked tests UAT-002; watched by critical_alert_delivery_pct GTE 100 percent
*Recent changes*
configuration CFG-1 at minute 4  NOT through a gated release
deployment REL-LEGACY at minute 6  NOT through a gated release
deployment REL-NOTIFY-1 at minute 12
configuration CFG-2 at minute 14  NOT through a gated release
configuration CFG-3 at minute 38  NOT through a gated release
*Historical context*
thread T-103 in #notifications-oncall: earlier thread on a similar config change issue
*Next checks (read-only)*
S1: diff_configuration production - Runbook step for H1 (Configuration changes)
*Gaps*
no traces were retrieved
*You decide*
decide noisy receipt 31f74bb9  |  decide understood H1 receipt 31f74bb9  |  decide deeper H1 receipt 31f74bb9
Nothing changes in production without your decision. Escalation owner: sre.
```

**sre:** `anything new?`

**bot:**

```
On it. Read-only investigation of notifications in production for alert A-4; history limited to channels you can see.
```

**bot:**

```
*Situation*
critical_alert_delivery_pct for notifications in production (SEV2) has been below baseline since minute 39; leading hypothesis: Degradation began 1 min after smtp.send_timeout_ms was changed (HIGH confidence) (newest 0 min old)
*Likely cause (hypotheses, not a root cause)*
H1 [HIGH] Degradation began 1 min after smtp.send_timeout_ms was changed. Confirm: Diff the configuration before and after the change and check it against the runbook's safe values. Evidence: config:CFG-3, metric:critical_alert_delivery_pct, log:L-0006, log:L-0007 (newest 0 min old)
*Release check*
MATCH: running artifact is the gated, tested release REL-NOTIFY-1 (tag notifications-v1.4.0, commit 35bc53d437b0)
At risk: SC-2 "critical alerts are never silenced" decided by po; locked tests UAT-002; watched by critical_alert_delivery_pct GTE 100 percent
*Recent changes*
configuration CFG-1 at minute 4  NOT through a gated release
deployment REL-LEGACY at minute 6  NOT through a gated release
deployment REL-NOTIFY-1 at minute 12
configuration CFG-2 at minute 14  NOT through a gated release
configuration CFG-3 at minute 38  NOT through a gated release
*Historical context*
thread T-103 in #notifications-oncall: earlier thread on a similar config change issue
*Next checks (read-only)*
S1: diff_configuration production - Runbook step for H1 (Configuration changes)
*Gaps*
no traces were retrieved
*You decide*
decide noisy receipt c855e6d5  |  decide understood H1 receipt c855e6d5  |  decide deeper H1 receipt c855e6d5
Nothing changes in production without your decision. Escalation owner: sre.
```

**sre:** `decide understood H1 receipt 31f74bb9`

**bot:**

```
That receipt is not the latest investigation in this thread (latest: c855e6d5). Re-read it, then decide.
```

**sre:** `decide understood H1 receipt c855e6d5 revert smtp.send_timeout_ms via the change process`

**bot:**

```
Recorded: ISSUE_UNDERSTOOD by sre on investigation c855e6d5.
```
