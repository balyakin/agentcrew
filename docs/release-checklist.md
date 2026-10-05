# Release acceptance checklist

Date: 2026-10-05. Environment **L1**: Linux, Python 3.12.3, Git 2.43.0, tmux 3.4, Node 24.19.0, Codex CLI 0.160.0; Claude absent. **M1**: macOS CI configured, not executed. **P1**: real paid-provider runs awaiting permission/accounts. **U1**: no user pilot or OpenRig comparison yet.

`passed` requires an executed check for the whole row; `pending` means a required part remains. A metadata version or a written test is not a passed acceptance test. Commands and historical failures are recorded in [progress.md](progress.md).

## Complete 1.0 scope — specification §2

| Capability | Status | Evidence / remaining work | Environment/date |
|---|---|---|---|
| Develop from a task | pending | Both complete local CSV fixtures pass hidden behavior after apply and preserve source; bounded revision, unknown criteria and reviewer faults checked. [Real provider harness](../tests/e2e/test_real_providers.py) remains unexecuted. | L1/P1, 2026-10-05 |
| New small project | pending | [Both runnable baselines](../tests/integration/test_scaffold.py) locally checked; full real-provider task result remains. | L1/P1, 2026-10-05 |
| Review changes | pending | [Contracts](../tests/unit/test_contracts.py), [partial reports](../tests/unit/test_reporting.py), workflow fixture; genuine planted-defect detection frequency remains. | L1/P1, 2026-10-05 |
| Fix and fresh verification | pending | Full [test-provider child fix](../tests/e2e/test_workflows.py) passes: new SHA, isolated fix branch, two criteria/reviewers, source intact before apply. Real child fix and independent defect reproduction remain. | L1/P1, 2026-10-05 |
| Persistent teams | pending | [Team lifecycle/handoff tests](../tests/integration/test_teams.py); final CLI and real-provider team task acceptance remains. | L1/P1, 2026-10-05 |
| Codex and Claude | pending | [Protocol adapter tests](../tests/unit/test_providers.py); Claude absent, real smoke/workflows and mixed run unperformed. | L1/P1, 2026-10-05 |
| Real tmux runtime | pending | [Private server](../tests/integration/test_tmux.py), [worker crash](../tests/integration/test_runtime.py); final reboot/server-kill fault matrix and macOS remain. | L1/M1, 2026-10-05 |
| Reliability | pending | [Storage races](../tests/integration/test_storage.py), Git source preservation, exact candidate proof; full pause/revision/recovery fault acceptance remains. | L1/M1/P1, 2026-10-05 |
| CLI/TUI, English/Russian | pending | 15 Textual tests pass at 80×24/narrow layouts; CLI locale and JSON tested. Installed-wheel commands and [no-model cast](demo.cast) checked. Full provider demonstration and pilot remain. | L1/U1, 2026-10-05 |
| Release preparation | pending | License/docs/CI, full local suite and clean Linux wheel installation checked; macOS, full provider demo, evaluation and human publication decision remain. | L1/M1/P1/U1, 2026-10-05 |

## User scenarios — specification §17

| # | Scenario | Status | Evidence / next required proof | Environment/date |
|---|---|---|---|---|
| 1 | First review: doctor, init, two seats, SHA report, source intact | pending | [Doctor](../tests/unit/test_doctor.py), Git/tmux and test-provider workflow checks; fresh README run with a real provider remains. | L1/P1, 2026-10-05 |
| 2 | Genuine defect detected without inserting a manual model finding | pending | Repeated authorized real-model trials with preregistered defects and miss rate. | P1, 2026-10-05 |
| 3 | Hallucinated path/line rejected and visible as provider failure | pending | [Strict candidate validator](../tests/unit/test_contracts.py), failed-role report checked; real/full CLI fault fixture remains. | L1/P1, 2026-10-05 |
| 4 | Worker death preserves task/candidate; recovery does not duplicate fix | pending | [Runtime crash test](../tests/integration/test_runtime.py), [leases](../tests/integration/test_storage.py); full fix crash/recovery scenario remains. | L1/M1, 2026-10-05 |
| 5 | Fix uses separate worktree and review checks new SHA | pending | Git isolation and no transplanted candidate proof checked; full independent child-fix test remains. | L1/P1, 2026-10-05 |
| 6 | Exit TUI, return through CLI, attach/report share one run | pending | Final 80×24 TUI/CLI integration and fresh README recording. | L1/M1, 2026-10-05 |
| 7 | Addressed message, owned task, handoff, snapshot and restore | pending | [Durable team/handoff checks](../tests/integration/test_teams.py); full CLI/provider session and macOS remain. | L1/M1/P1, 2026-10-05 |
| 8 | CSV Python/TypeScript task passes hidden behavior and preserves source | pending | [Both complete CSV protocol fixtures](../tests/e2e/test_csv.py) pass local hidden behavior and preserve source; actual model generation remains external acceptance. | L1/P1, 2026-10-05 |
| 9 | Failing first implementation creates bounded fresh revision | passed | [Native controller workflow with test-only provider](../tests/e2e/test_workflows.py): first failed check, fresh candidate/evidence, explicit one-extra-revision ceiling. This is controller evidence, not a real model trial. | L1, 2026-10-05 |
| 10 | Unknown criterion or failed reviewer prevents accepted | passed | [Full CSV workflows](../tests/e2e/test_csv.py) show green checks with missing exact test ID, failed/invalid reviewer, retained candidate and blocked ordinary apply. | L1, 2026-10-05 |
| 11 | Both new templates create baseline and applicable implemented result | pending | [Baseline/occupied-directory checks](../tests/integration/test_scaffold.py); full provider development and apply remain. | L1/P1, 2026-10-05 |
| 12 | Real independent Codex/Claude workflows and explicit mixed roles | pending | Adapter contracts pass locally; Claude/account and authorized real trials absent. | P1, 2026-10-05 |
| 13 | Close client, pause, answer persisted question, resume one writer | pending | [Local workflow tests](../tests/e2e/test_workflows.py) cover durable idempotent answer, one writer, pause while writing and canceled partial work. Real interactive client/platform scenario remains. | L1/M1/P1, 2026-10-05 |

## Additional public-release gates

| Gate | Status | Required artifact |
|---|---|---|
| Final ruff and full test suite | passed | [Post-review aggregate evidence](progress.md): ruff passes; 323 passed, 9 explicitly skipped real-provider cases. Linux L1, 2026-10-05. |
| Clean Linux wheel and basic README smoke | passed | Fresh site-packages installation; 52 executed no-model commands and both runnable baselines. Provider-dependent steps remain separate. Linux L1, 2026-10-05. |
| Fresh wheel install and README on Linux/macOS | pending | Executed clean-environment logs; CI configuration alone is insufficient. |
| Real demo GIF/screencast | pending | [Actual no-model infrastructure cast](demo.cast) is available. Full real-provider develop/review/fix recording and actual reports remain. |
| Five-person pilot, at least 4/5 complete both tasks unaided | pending | Anonymized protocol/results and repeated pilot after fixes. |
| Matched OpenRig comparison on five repositories | pending | Current version, all attempts, blind adjudication, usage when measured. |
| License owner attribution verified | passed | User explicitly chose MIT and Copyright (c) 2026 Evgeny Balyakin. See [LICENSE.md](../LICENSE.md). |
| Human publication authorization | pending | Separate decision; no automatic repository/package/media publication. |
