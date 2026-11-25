# AIDLC-G playbook

How to use AIDLC-G, depending on what you want from it. Every play is runnable today. Where something
isn't built yet, the play says so.

## Pick your play

| You are… | You want to… | Play | Time |
|---|---|---|---|
| Anyone curious | See what the gate does | [1. See it work](#play-1-see-it-work) | 10 min |
| A skeptic or security reviewer | Find out what it stops, and try to get past it | [2. Try to break it](#play-2-try-to-break-it) | 30 min |
| A team lead or developer | Run a real change of your own through it | [3. Run your own change](#play-3-run-your-own-change) | 1–2 h |
| A platform engineer | Call the gate from your own tools | [4. Use the gate as a service](#play-4-use-the-gate-as-a-service) | 1 h |
| A platform engineer | Add evidence-carrying checks to existing CI | [5. Adopt it piece by piece](#play-5-adopt-it-piece-by-piece) | days |
| A governance or risk owner | Decide who must approve what | [6. Tune the policy](#play-6-tune-the-policy) | 30 min |
| An SRE | See read-only incident support with human-only authority | [7. Try incident support](#play-7-try-incident-support) | 20 min |
| A researcher | Reproduce, compare, or extend the results | [8. Research with it](#play-8-research-with-it) | hours |

## Play 0: Install

You need Python 3.9 or later.

```bash
git clone https://github.com/lakshmipriyagopalsamy/aidlc-g && cd aidlc-g
python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

All tests should pass in about two minutes. For exact dependency versions or a container, see
[REPRODUCING.md](../REPRODUCING.md).

## Play 1: See it work

**Goal:** watch one change go from a plain request to production, with you making every human decision.

```bash
.venv/bin/python scripts/product.py run --example
```

The example change is "users can turn off optional notifications but still get critical alerts". The
scripted agents draft everything and stop at each point where a person decides. You'll be asked to:
- declare a risk tier, or skip to keep every approval;
- answer the agent's clarifying questions;
- keep, rewrite or drop each suggested scenario, and add your own;
- approve each artifact, in the role it belongs to;
- review the pull request.

The run ends released, or halted with the gate's reasons, and writes a full report to `results/`.

**Try next:**
- `run --example java`: the same change on a Java/Spring stack. It uses a stand-in build tool, so no JDK is needed.
- Answer differently: drop the only negative scenario, or reject an approval. See where the gate stops and why.
- Read `results/vertical_slice.md`: a complete run with every gate decision, approval, test run and release record.

**What to notice:** agents never approve anything. Every decision is bound to the exact version you saw, and the gate re-checks everything at each stage.

## Play 2: Try to break it

**Goal:** see which layer stops each misbehavior, then hunt for one that gets through.

1. **Run the ready-made attacks:**

   ```bash
   .venv/bin/python scripts/run_vertical_slice.py     # 23 adversarial scenarios
   .venv/bin/python scripts/run_trials.py --repeats 1 # 17 violation trials + 3 valid controls
   ```

2. **Play a misbehaving agent.** Replace any agent's output and watch where the work stops:

   ```python
   from aidlc_g.examples import notifications as ex
   from aidlc_g.orchestrator import SliceEnvironment, SliceOrchestrator

   model = ex.model().with_override("implementation_agent", "implement", ex.buggy_implementation)
   report = SliceOrchestrator(SliceEnvironment.create(ex.SEED, ex.ADVISORIES), model, ex.desk()).release(ex.REQUEST)
   print(report.halted)   # stopped at TEST_COVERAGE: a locked test caught critical alerts being silenced
   ```

   `src/aidlc_g/examples/notifications.py` has more adversaries:
   - an implementer that edits locked tests;
   - an agent that invents a metric or cites a source it never read;
   - a release that names a different artifact.

   Write your own by returning any output you like from an agent task.

3. **Attack the gate directly,** without agents:

   ```python
   from aidlc_g.lifecycle import Stage
   from aidlc_g.testkit import World

   w = World()
   w.advance_to(Stage.ACCEPTANCE_TESTS)
   w.make_c3(drop_scenarios=("SC-4",))    # leave one decided scenario untested
   w.make_baseline()
   print(w.transition(Stage.IMPLEMENTATION)["reasons"])   # SCENARIO_NOT_COVERED
   ```

**Found a way through?** Open a *Governance bypass* issue with the smallest steps to reproduce it. That's
the most valuable report you can make.

## Play 3: Run your own change

**Goal:** take a real change in your own product through the lifecycle, with live agents doing the work and you making the decisions.

**Before you start:**
- **Python product:** your code should be Python to run with its real toolchain. Java, Kotlin and C# profiles exist, but so far have run only against a stand-in build tool.
- **API access:** live agents use the Claude API. Install the client and set your key:

  ```bash
  .venv/bin/pip install anthropic
  export ANTHROPIC_API_KEY=...
  ```

  Runs spend API credits.
- **Isolation:** the test runner executes code the agents write, in a temporary folder with a timeout. That isn't a sandbox, so run it in a container or VM.

**Steps:**

1. List the stacks a team can choose, then create a product pack:

   ```bash
   .venv/bin/python scripts/product.py stacks
   .venv/bin/python scripts/product.py init products/myproduct --key MYPRODUCT --stack python-pytest
   ```

2. Fill in the pack:
   - `products/myproduct/product.json`: the problem, the metric that shows it's solved and where that metric comes from, and the change in plain words. The run refuses to start while any `TODO` is left.
   - `products/myproduct/repo/`: put your existing code here, or set `"repo"` to an existing checkout.
   - `products/myproduct/instructions/*.md`: your team's conventions, such as coding standards, framework rules, libraries and testing style. Every agent reads them; they set conventions, never permissions.

3. Run it:

   ```bash
   .venv/bin/python scripts/product.py run products/myproduct --claude
   ```

   You act as every human role in turn. The desk says which role you're acting as at each step.

4. Read the outputs:
   - `results/myproduct_delivery.md`: every decision, test run, approval and the release record;
   - `results/myproduct_decisions.json`: what you decided, in order.

**Tips:**
- Start with a small, well-understood change.
- Write the negative scenario: "what must *not* happen" catches the misreadings agents make most often.
- Declare a LOW risk tier only for genuinely low-risk changes. The gate raises the tier by itself if the evidence calls for it.

## Play 4: Use the gate as a service

**Goal:** drive the gate from your own tools over HTTP.

```bash
.venv/bin/pip install uvicorn
AIDLC_G_DB=gate.sqlite .venv/bin/uvicorn --factory aidlc_g.api:build_default_app
```

The server prints a one-time admin token. A typical flow:
1. **Set up identities:** register principals (people, agents, services) with roles (`POST /principals`) and the CI runner's public key (`POST /runners`).
2. **Open a work item:** `POST /work-items`.
3. **Submit contracts:** agents submit them (`POST /contracts/{type}`), and people approve the exact version and digest (`POST /contracts/{id}/approve`).
4. **Register builds:** `POST /builds/register`.
5. **Ingest evidence:** CI submits signed test evidence (`POST /evidence/ingest`).
6. **Ask for transitions:** `POST /transitions/evaluate`. The answer is ALLOW, PAUSE (a person must decide) or BLOCK, with reasons.
7. **Audit:** `GET /audit/verify` checks the hash-chained log; `GET /work-items/{id}/release-manifest` gives the full evidence record.

`tests/test_api.py` walks through the whole sequence. Calls without a valid bearer token are refused.

The store is a single SQLite file. Identity is bearer tokens, not single sign-on; for anything beyond a
trial, put the service behind your identity provider.

## Play 5: Adopt it piece by piece

**Goal:** add evidence-carrying checks to GitHub Actions or GitLab CI without adopting the whole framework.

Start with what's easiest and stop wherever the value is enough. **Not built yet:** these platform
adapters. The gate API above is what they would call.

| Level | What you add | How | Effort |
|---|---|---|---|
| 1 | Acceptance tests must carry the owner's scenarios word for word, and must fail before implementation | Two ordinary CI steps: compare scenario text in the test manifest with the approved requirement file; run the locked tests against a stub and require failures | Low |
| 2 | Approvals bound to exact versions | A small service records an approval only from a reviewer holding the right role (from code owners or your identity provider) and stores the file's content digest; a change to the file or anything it depends on voids it | Medium: needs an approval store outside any single pull request |
| 3 | One evaluator decides every promotion | Run the gate as a required status check on pull requests, merges and deployments; branch and environment protection require its decision | Medium |
| 4 | Signed evidence instead of pass/fail statuses | CI publishes signed attestations of what it ran, including test digests (for example in-toto signed through Sigstore); the evaluator verifies them | Medium |

The hard part is level 2's cross-pull-request invalidation: platform settings that dismiss stale reviews
work within one pull request, not across the requirement, design and tests.

## Play 6: Tune the policy

**Goal:** decide who must approve what, and how strict the checks are.

The policy is code: `src/aidlc_g/policy.py`. Useful fields:

| Field | What it controls | Default |
|---|---|---|
| `approvers` | Which roles must approve each contract type | sponsor (C0), product owner (C1), engineering lead (C2), and so on |
| `tier_approvers` | Which approvals LOW and MEDIUM risk tiers replace with a recorded policy authorization | C0, C1 and C3 always stay human |
| `default_risk_tier` | Tier for work nobody declared | `HIGH` (every approval) |
| `low_risk_max_changed_files` | Changes larger than this raise the tier to MEDIUM | 10 |
| `required_quality_checks` | Checks that must run before review | coverage tests, mutation, secrets, dependencies |
| `min_mutation_score_pct` | Share of mutants the tests must kill | 80 |

Two rules to know:
- **Changing the policy changes its version,** and approvals given under the old version stop counting.
- **Removing a rule should break a trial.** Run `.venv/bin/python -m pytest tests/test_trials.py` to see which trials depend on it.

## Play 7: Try incident support

**Goal:** see an operations agent help an on-call engineer in a chat thread, where only the engineer can act.

```bash
.venv/bin/python scripts/run_oncall_demo.py
```

`results/oncall_demo.md` shows two incidents:
- **A release regression:** the engineer decides to roll back, and the gate authorizes it.
- **A configuration change made outside any release:** a decision based on an outdated investigation is refused.

The agent only reads, cites what it read, and proposes steps from a fixed list of read-only diagnostics.

**Connecting your own systems** means writing adapters for the ports in `src/aidlc_g/oncall.py` and `src/aidlc_g/ops.py`:
- `ChatChannel` for your chat tool;
- `IdentityDirectory` for linking chat users to on-call identities;
- a read-only store for your runtime platform's metrics, logs, deployments and configuration.

**Not built yet:** these adapters. They must keep the agent read-only and verify your chat platform's request signatures.

## Play 8: Research with it

**Goal:** reproduce the results, compare against your own setup, or extend the studies.

- **Reproduce:** [REPRODUCING.md](../REPRODUCING.md) maps every result to its script. All are deterministic, except machine-dependent latencies.
- **Compare:** `scripts/run_baselines.py` runs the same trials under emulated conventional configurations. Add a configuration in `src/aidlc_g/baseline_study.py` that matches your organization's controls and see what it lets through.
- **Extend:** add a trial in `src/aidlc_g/trials.py` with a predeclared expected outcome, and pair it with a test showing the trial fails when its control is switched off. The bounded model checker in `src/aidlc_g/model_check.py` takes new component models the same way.
- **Live models:** most components accept a Claude-backed model in place of the scripted one, for example `scripts/run_vertical_slice.py --claude` and `scripts/run_incident_study.py --claude`. Report what you find, including negative results.
- **Cite:** use [CITATION.cff](../CITATION.cff).

## What it is not (yet)

- **A research prototype, not a production platform.** It has one example service, simulated deployment and telemetry, bearer-token identity, and a single-file store whose tampering it detects but doesn't prevent.
- **Evidence of enforcement, not of better outcomes.** The results show that the controls are enforceable and what they add over emulated baselines. They don't show improved delivery in real teams. Studies with live models and real teams are the next step, and your results would help.

## Feedback

Open an issue using a template: *Results report*, *Bug report* or *Governance bypass*. See
[CONTRIBUTING.md](../CONTRIBUTING.md).
