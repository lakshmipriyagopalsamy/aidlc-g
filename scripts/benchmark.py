"""Scaling benchmark: gate decision time as the governance store grows.

    .venv/bin/python scripts/benchmark.py                 # default sizes
    .venv/bin/python scripts/benchmark.py 0 1000 10000    # store sizes (approved contracts already present)

For each size, the store is first filled with that many approved contracts from other work items
(each a submitted C0 with a sponsor approval, so the artifacts, approvals and audit tables all grow).
A fresh work item is then carried through every stage to RELEASE_READY, and the script reports the
median time of a single transition decision, the full release-gate decision (which re-checks all
upstream evidence), the whole lifecycle, and peak memory. Filling the store is not timed.
Writes results/benchmark.{md,json}.
"""

import json
import platform
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aidlc_g import __version__  # noqa: E402
from aidlc_g.lifecycle import Stage  # noqa: E402
from aidlc_g.testkit import World  # noqa: E402

DEFAULT_SIZES = (0, 1_000, 5_000, 20_000)
RUNS = 5


def fill(w: World, n: int) -> None:
    for i in range(n):
        body = w.c0_body(initiative_id=f"INIT-FILL-{i}", key_result_id=f"KR-FILL-{i}")
        view = w.svc.submit_contract(w.tok("requirements_agent"), "C0", f"INIT-FILL-{i}", body)
        w.svc.approve(w.tok("sponsor"), view["artifact_id"], view["version"], view["digest"])


def measure(size: int) -> dict:
    transitions, release, lifecycle = [], [], []
    for _ in range(RUNS):  # timed without memory tracing, which would slow every call
        w = World()
        fill(w, size)
        started = time.perf_counter()
        w.advance_to(Stage.RELEASE_READY)
        lifecycle.append(time.perf_counter() - started)
        transitions.extend(w.latencies)
        release.append(w.latencies[-1])
    m = World()  # one extra run, untimed, for peak memory of the lifecycle itself
    fill(m, size)
    tracemalloc.start()
    m.advance_to(Stage.RELEASE_READY)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    rows = w.svc._db.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
    return {"stored_contracts": size, "audit_entries": rows,
            "transition_ms": round(statistics.median(transitions) * 1000, 2),
            "release_gate_ms": round(statistics.median(release) * 1000, 2),
            "lifecycle_ms": round(statistics.median(lifecycle) * 1000, 1),
            "peak_mb": round(peak / 1e6, 1)}


def main() -> int:
    sizes = [int(a) for a in sys.argv[1:]] or list(DEFAULT_SIZES)
    rows = []
    for size in sizes:
        print(f"store of {size:,} approved contracts ...", flush=True)
        rows.append(measure(size))
    env = {"version": __version__, "python": platform.python_version(), "machine": platform.machine(),
           "date": time.strftime("%d %b %Y"), "runs_per_size": RUNS}
    out = Path(__file__).resolve().parents[1] / "results"
    (out / "benchmark.json").write_text(json.dumps({"environment": env, "rows": rows}, indent=2) + "\n")
    lines = ["| Approved contracts in store | Audit entries | Transition decision (ms) | Release gate (ms) | "
             "Whole lifecycle (ms) | Peak memory (MB) |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['stored_contracts']:,} | {r['audit_entries']:,} | {r['transition_ms']} | "
                     f"{r['release_gate_ms']} | {r['lifecycle_ms']} | {r['peak_mb']} |")
    (out / "benchmark.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n{env}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
