# Decision latency by risk tier

> Simulated clock: each human step waits 120 min; approvals of one artifact version are answered in parallel. Response times are assumed; the decision counts and the gate outcomes are exact.

| Case | Released | Effective tier | Evidence signals | Human decisions | Policy authorizations | Approval critical path (min) | Lead time (min) | Catch-up decisions |
|---|---|---|---|---|---|---|---|---|
| HIGH (default) | yes | HIGH | RISK_ACCEPTED | 11 | 0 | 840 | 1080 | — |
| MEDIUM | yes | MEDIUM | RISK_ACCEPTED | 9 | 3 | 720 | 960 | — |
| LOW, reviewer accepts a risk | yes | MEDIUM | RISK_ACCEPTED | 9 | 3 | 960 | 960 | TR-NOTIFY@v1 engineering_lead |
| LOW, clean review | yes | LOW | — | 6 | 6 | 360 | 600 | — |
| LOW, design lists a threat | yes | HIGH | SECURITY_RELEVANT_DESIGN | 12 | 0 | 840 | 1080 | TR-NOTIFY@v1 engineering_lead |
