# Decision-latency sensitivity

> Schedules from real gate runs; response times sampled (2000 draws per cell, log-normal, common random numbers). Assumed, not measured.

| Case | Effective tier | Human decisions | Policy authorizations | Waits | People asked |
|---|---|---|---|---|---|
| HIGH (default) | HIGH | 11 | 0 | 9 | 12 |
| MEDIUM | MEDIUM | 9 | 3 | 8 | 9 |
| LOW, reviewer accepts a risk | MEDIUM | 9 | 3 | 8 | 9 |
| LOW, clean review | LOW | 6 | 6 | 5 | 6 |
| LOW, design lists a threat | HIGH | 12 | 0 | 9 | 12 |

| Median response | Sigma | Case | Lead time p50 (h) | p90 (h) | p50 reduction vs HIGH |
|---|---|---|---|---|---|
| 30 min | 0.5 | HIGH (default) | 5.5 | 6.8 | 0% |
| 30 min | 0.5 | MEDIUM | 4.6 | 5.9 | 15% |
| 30 min | 0.5 | LOW, reviewer accepts a risk | 4.6 | 5.9 | 16% |
| 30 min | 0.5 | LOW, clean review | 2.9 | 3.9 | 46% |
| 30 min | 0.5 | LOW, design lists a threat | 5.5 | 6.8 | 0% |
| 30 min | 1.0 | HIGH (default) | 8.2 | 12.8 | 0% |
| 30 min | 1.0 | MEDIUM | 6.4 | 10.9 | 22% |
| 30 min | 1.0 | LOW, reviewer accepts a risk | 6.4 | 10.8 | 22% |
| 30 min | 1.0 | LOW, clean review | 4.0 | 7.6 | 51% |
| 30 min | 1.0 | LOW, design lists a threat | 8.2 | 12.8 | 0% |
| 120 min | 0.5 | HIGH (default) | 21.8 | 27.1 | 0% |
| 120 min | 0.5 | MEDIUM | 18.4 | 23.3 | 16% |
| 120 min | 0.5 | LOW, reviewer accepts a risk | 18.4 | 23.3 | 15% |
| 120 min | 0.5 | LOW, clean review | 11.6 | 15.6 | 47% |
| 120 min | 0.5 | LOW, design lists a threat | 21.8 | 27.1 | 0% |
| 120 min | 1.0 | HIGH (default) | 32.1 | 53.2 | 0% |
| 120 min | 1.0 | MEDIUM | 25.8 | 44.9 | 20% |
| 120 min | 1.0 | LOW, reviewer accepts a risk | 25.8 | 44.9 | 20% |
| 120 min | 1.0 | LOW, clean review | 16.1 | 31.1 | 50% |
| 120 min | 1.0 | LOW, design lists a threat | 32.1 | 53.2 | 0% |
| 480 min | 0.5 | HIGH (default) | 87.4 | 107.8 | 0% |
| 480 min | 0.5 | MEDIUM | 72.9 | 92.4 | 17% |
| 480 min | 0.5 | LOW, reviewer accepts a risk | 73.2 | 92.8 | 16% |
| 480 min | 0.5 | LOW, clean review | 46.3 | 61.0 | 47% |
| 480 min | 0.5 | LOW, design lists a threat | 87.4 | 107.8 | 0% |
| 480 min | 1.0 | HIGH (default) | 126.8 | 206.9 | 0% |
| 480 min | 1.0 | MEDIUM | 101.5 | 173.6 | 20% |
| 480 min | 1.0 | LOW, reviewer accepts a risk | 101.7 | 175.2 | 20% |
| 480 min | 1.0 | LOW, clean review | 62.4 | 121.2 | 51% |
| 480 min | 1.0 | LOW, design lists a threat | 126.8 | 206.9 | 0% |
