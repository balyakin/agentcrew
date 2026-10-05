# Evaluation protocol and current results

Date: 2026-10-05. Real-provider quality and the user comparison are **pending**. Local tests establish protocol, isolation and evidence behavior; they do not establish a model's defect-detection rate or useful code-generation quality. No cost, success frequency or user result is invented.

## Comparison fixed before measurement

Compare AgentCrew with the current OpenRig on the same five small fictional Git repositories, planted defects, task specifications, baseline commits, provider/model and budgets. Refresh OpenRig's actual installed version and command help before a run. Its [README](https://github.com/mvschwarz/openrig), [architecture](https://openrig.dev/docs/architecture), [coordination](https://openrig.dev/docs/coordination) and [proof](https://openrig.dev/docs/work-tree) already describe teams, queues, TUI, recovery and work evidence; these are not claimed as AgentCrew inventions.

Each repository has a preregistered defect truth set and hidden behavioral checks. Publish anonymized input fixtures, versions, all attempts, artifacts and adjudication. Run at least three attempts per tool/scenario; keep misses, invalid outputs, crashes and manual interventions in the denominator. Alternate tool order and use fresh equivalent checkouts. Reviewers judging findings should not see which tool produced them.

| Measure | Definition |
|---|---|
| Time to first usable review | Setup start and repeat launch start measured separately, ending at a report with concrete candidate-linked claims. |
| Detection | Number of planted defects correctly identified; an existing line alone is insufficient. |
| False findings | Claimed defects not supported by the truth set or independent reproduction. |
| Development completion | Hidden behavior and regression checks on the applied candidate, including all CSV conditions. |
| Time to applicable patch | Start at the saved task, end at a candidate which can be applied to the unchanged source. |
| Intervention and recovery | Number and duration of human steps; retained artifacts and duplicate effects after fault injection. |
| Usage and cost | Provider-reported measurements only. Missing values remain `unknown`; duration is not a price estimator. |

Predeclared decision thresholds: detection must be at least the baseline and false findings no greater; develop hidden-test pass rate must be at least the baseline. Claim a speed/usability improvement only if median time falls without worse completion and interventions. With this small sample, report descriptive results and uncertainty, not a universal superiority claim. Role count and diff size are not success measures.

If OpenRig has no directly comparable automatic path, record its supported manual workflow and every human step. Do not invent a missing command and count its absence as poor code quality.

## Real-provider acceptance

After explicit permission to use the paid accounts:

```sh
agentcrew doctor --json
agentcrew doctor --smoke --provider codex --yes
agentcrew doctor --smoke --provider claude --yes
```

The separately marked [real-provider harness](../tests/e2e/test_real_providers.py) makes actual calls only after explicit environment opt-in. It covers Python/TypeScript CSV plus a preregistered zero-division review for each provider and a mixed configuration. It saves the actual report under each retained test directory, preserves unsuccessful attempts, and runs the same hidden CSV behavior as the local protocol fixtures.

```sh
# Run only after consenting to paid calls; no fabricated answers are used here.
AGENTCREW_REAL_PROVIDERS=codex .venv/bin/python -m pytest \
  tests/e2e/test_real_providers.py -q --basetemp=/tmp/agentcrew-real-codex
# Both authenticated providers, including mixed roles:
AGENTCREW_REAL_PROVIDERS=codex,claude .venv/bin/python -m pytest \
  tests/e2e/test_real_providers.py -q --basetemp=/tmp/agentcrew-real-both
```

Choose unused disposable `--basetemp` paths: pytest replaces that test directory. Without opt-in, all nine cases skip. Those skips are not successful model trials. Per-workflow time/revision limits apply; there is no unmeasured hard monetary guarantee.

For each provider, run both review with planted defects and develop/new with the CSV task from [examples/task.md](../examples/task.md), separately on Python and TypeScript. Also run an explicitly mixed role configuration, without overriding it with `--provider`. Record actual provider versions/model, SHA, duration, all role/check outcomes and measured usage. Repeat the same genuine defect several times and report misses rather than manually inserting a finding.

| Trial | Status | Evidence still needed |
|---|---|---|
| Real Codex review and CSV development | pending | Authorized calls, reports, exact models, hidden checks and repeat frequencies. |
| Real Claude review and CSV development | pending | CLI/account unavailable in this environment; version-bound read-only smoke and both workflows. |
| Mixed real providers | pending | Explicit role configuration plus both authenticated accounts. |
| Linux automated implementation | passed subset | [progress.md](progress.md): real Git/SQLite/tmux and test-only provider protocols; final aggregate run recorded separately. |
| macOS | pending | CI is configured, but no macOS execution has been observed. |
| OpenRig five-repository comparison | pending | Current version, all matched trials and blinded adjudication. |

## Five-person pilot

Recruit five developers who know basic Python and have not used AgentCrew. Give each the README and a clean environment, then one first review and one small CSV development task. Do not coach them during the measured run; record confusing steps, assistance, time, outcome, source preservation and ability to explain evidence versus model claims.

The acceptance threshold is at least **4 of 5** completing **both** tasks unaided. If fixes are made, repeat the pilot and retain the failed first round. Obtain consent for anonymized observation, and publish no private code or account details. Status: pending; no participants have been measured.

## Reproducible environment snapshot

Locally observed: Linux, Python 3.12.3, Git 2.43.0, tmux 3.4, Node 24.19.0, Codex CLI 0.160.0. Claude CLI is absent. Auth/help probes consume no model calls. Package version `1.0.0` is target metadata, not evidence that this acceptance is complete.
