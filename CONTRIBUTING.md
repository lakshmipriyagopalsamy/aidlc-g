# Contributing and feedback

AIDLC-G is a research prototype. The most useful contributions are evidence: results from running it in a setting other than the author's.

## Reporting results

If you ran the experiments, the benchmark, or the gate on your own changes, please open a **Results report** issue. Include:
- what you ran;
- the version or commit;
- your platform;
- what you observed, especially anything that disagrees with `results/`.

Negative results are as welcome as positive ones.

## Reporting problems

Open a **Bug report** with the command, the full output, and your Python version. For a governance bypass (a way to get a transition admitted that the rules should refuse), use the **Governance bypass** template and include the smallest reproducing steps. Treating bypasses as bugs is how the trial matrix grew.

## Changing the code

1. Set up as in [REPRODUCING.md](REPRODUCING.md) and run `python -m pytest -q`; all tests must pass.
2. A new governance rule needs:
   - a trial or test that fails without it;
   - a control mutant, or a test showing the trial fails when the rule is switched off.

   A trial that cannot fail proves nothing.
3. Keep the agents unable to approve: no change may give an agent role an approval, merge, deploy or rollback capability.
4. Describe in the pull request which results change, and re-run the affected scripts in `scripts/`.

## Citing

If you use AIDLC-G in research, please cite it using [CITATION.cff](CITATION.cff).

By contributing you agree that your contribution is licensed under the MIT license ([LICENSE](LICENSE)).
