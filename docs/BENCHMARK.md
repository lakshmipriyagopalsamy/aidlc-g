# Performance

`scripts/benchmark.py` times the gate as its store grows. For each size, the store is first filled with
that many approved contracts from other work items, so the artifact, approval and audit tables all grow.
Then a fresh work item is carried through every stage to release-ready. Filling the store isn't timed.

Measured on 25 Nov 2026 (aidlc-g 0.8.0, Python 3.9.6, Apple Silicon laptop, in-memory SQLite, median of
5 runs per size):

| Approved contracts in store | Audit entries | Transition decision (ms) | Release gate (ms) | Whole lifecycle (ms) | Peak memory (MB) |
|---|---|---|---|---|---|
| 0 | 56 | 0.51 | 1.06 | 7.3 | 0.2 |
| 1,000 | 2,056 | 0.59 | 0.94 | 8.1 | 0.2 |
| 5,000 | 10,056 | 0.54 | 1.08 | 7.5 | 0.2 |
| 20,000 | 40,056 | 0.61 | 1.36 | 8.3 | 0.2 |

- **Release gate:** the last transition, which re-checks every upstream contract, approval, attestation and signature.
- **Whole lifecycle:** all eight transitions, plus the gate's own work of submitting, approving and ingesting evidence.
- **Memory:** peak memory of the lifecycle run, excluding the filled store.

Decision time stays roughly flat as the store grows, because the gate looks up the referenced artifacts, approvals and evidence by key rather than scanning history. The cost of governance is people's time, not machine time; see `results/decision_latency.md`.

Not included:
- **Running tests.** The trusted runner's subprocess test runs and mutation testing take about two seconds for the example change.
- **Real build tools.** Only Python projects run with their real toolchain; the Java, Kotlin and C# profiles run against a stand-in build tool.

Run it yourself:

```bash
python scripts/benchmark.py 0 1000 5000 20000
```

It writes `results/benchmark.md` and `results/benchmark.json`.
