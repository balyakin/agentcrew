# Implementation evidence

2026-10-05. Written specification read; all work is confined to `agentcrew/`. Environment: Linux, Python 3.12.3, Git 2.43.0, tmux 3.4, Node 24.19.0, Codex CLI 0.160.0. Claude CLI absent. Paid calls await explicit permission; macOS, pilot and comparison remain external pending acceptance.

The explicit request to implement the specification authorizes local implementation. A standalone repository/branch isolates this work. A numbered stage is an implementation checkpoint; it does not constitute a separately finished product release.

## Executed evidence

- Contracts/config: RED observed before modules existed; later boundary RED exposed unhashable config enum types, `.` paths and Unicode line separators. Fixed and rerun: **103 passed** with `.venv/bin/python -m pytest tests/unit/test_contracts.py tests/unit/test_config.py -q --tb=short`.
- Checks/reporting: RED observed for missing modules and later for order-sensitive finding fingerprints, suppressed JUnit cases, wrapped worker results and native Node absolute testcase paths. Fixed and rerun: **24 passed** with `.venv/bin/python -m pytest tests/integration/test_checks.py tests/unit/test_reporting.py -q --tb=short`. Includes actual pytest and Node JUnit, fresh worktree per check, timeout, stale/tampered evidence and candidate-bound manual audit.
- Consolidated local subset: **179 passed in 14.46 s** with `.venv/bin/python -m pytest tests/unit/test_contracts.py tests/unit/test_config.py tests/unit/test_doctor.py tests/unit/test_cli.py tests/unit/test_models.py tests/integration/test_storage.py tests/integration/test_git.py tests/integration/test_checks.py tests/integration/test_scaffold.py tests/unit/test_reporting.py -q --tb=short`.
- Historical aggregate run: **204 passed, 1 failed in 26.78 s**. `tests/e2e/test_workflows.py::test_develop_freezes_real_code_and_preserves_source` failed because its test-only Codex executable did not advertise the auth-status subcommand in the expected help format; the planner correctly stopped with `provider_unauthenticated`. Keep this failure recorded; the final rerun is recorded below by the integrator.
- Ruff passed for the contracts/config/checks/requirements/aggregate/report files and their tests. The final whole-repository result is separate.

## Specification §19 stages

| Stage | Implemented/checkable portion | Executed local evidence | Outstanding acceptance / next step |
|---|---|---|---|
| 1. Skeleton and contracts | Metadata, CLI/doctor, config, closed plan/implement/review validation | Contracts/config 103; complete final suite; clean installed-wheel CLI and both baselines below | Target metadata does not establish public 1.0 readiness. |
| 2. Git and storage | Candidate/worktree identities, transactional claims, events and source-based apply | Git and storage tests in the consolidated subset | Broader final workflow integration and macOS. |
| 3. Tmux and worker | Private socket, supervisor, workers, leases and preserved crash state | Actual tmux lifecycle, server loss during writing, repeated recovery, pause/cancel and fencing; final aggregate below | Reboot and remaining platform fault matrix. |
| 4. Providers | Codex/Claude argv, stream parsing, process bounds and version-specific read-only policy | Test-only executable protocol suite maintained in test_providers.py; final aggregate pending | Real authenticated Codex/Claude smoke/workflows and mixed role run. |
| 5. Review | Fresh checks copies, JUnit proof, validation, conservative aggregation and partial reports | 24 checks/report tests; real native runner output | Real planted defect detection frequency and reviewer failure in final CLI. |
| 6. Develop and new | Plan, single writer, freeze/evaluate/revisions, Python/TypeScript baseline | Real Git/SQLite/tmux with test-only provider: both complete CSV paths, hidden behavior after apply, fresh revision proof, revision limit, unknown criterion, failed/invalid reviewer and durable answer | Full real-provider CSV and hidden behavior; user acceptance. |
| 7. Fix and apply | Source-HEAD guards, isolated candidate, explicit dry-run/apply | Binary/spaced paths and stale/dirty/unaccepted refusal; full test-provider child fix verifies new SHA, two requirements and two reviews before original finding becomes fixed | Real-provider child fix and remaining fault acceptance. |
| 8. Persistent teams | Templates, durable inbox, ownership, SHA handoff and snapshot | Real Git/SQLite/tmux team tests implemented by team worker; final aggregate pending | Complete CLI/provider team session and inspected writer recovery. |
| 9. CLI/TUI/docs | Full command integration, EN/RU interface/documentation, architecture, CI and examples | 15 headless Textual tests: 80×24, 60-column cards, keyboard, 1000-event timing and audit; locale/JSON checks; clean installed-wheel help/status/doctor/init and both baselines; no-model cast | Full real-provider demonstration and five-person pilot. |
| 10. Public 1.0 acceptance | Release/evaluation ledgers and reproducible commands | Linux local implementation evidence above | Final suite/package, macOS, accounts, five-person pilot and five-repository comparison; human publication only. |

No stage is marked externally complete while its independent acceptance is pending. [release-checklist.md](release-checklist.md) carries every specification §2 feature and §17 scenario.

## Final integration result

Final verified code tree on Linux, 2026-10-05:

- `.venv/bin/ruff check .`: **passed**. `git diff --check`: **passed**.
- `.venv/bin/pytest -q`: **279 passed, 9 skipped in 128.64 s**. The nine skips are the opt-in real Codex/Claude/mixed cases; no model call was made. Includes actual Git, SQLite, tmux, pytest/Node JUnit, CSV hidden behaviors, child fix/apply, leases, cancellation, lost server, provider descendants, native-resume selection, billing across revisions and Textual keyboard/layout checks.
- `.venv/bin/python -m pip wheel --no-deps . --wheel-dir dist`: **passed**, producing `agentcrew-1.0.0-py3-none-any.whl`. Installed that wheel and its `[dev]` dependencies into a new `/tmp` virtual environment. Import resolved to its `site-packages`, not the working tree. Packaged SQLite migration loaded successfully.
- Clean installed-wheel smoke: **52 commands passed**, including all root/nested command help, version, no-model doctor, empty status, non-mutating recovery preview, init with Russian locale, both scaffold baselines and their actual CLI/tests. Python baseline also passed isolated ruff. The provider-dependent README steps remain pending; this result does not claim those were run.
- Tool versions observed: Python **3.12.3**, Git **2.43.0**, tmux **3.4**, Node **24.19.0**, Textual **8.2.8**, pytest **9.1.1**, ruff **0.16.10**, Codex CLI **0.160.0**. Claude CLI absent. GitHub Actions v7 tags were checked against their official repositories; configured Linux/macOS jobs have not been run remotely.
- [demo.cast](demo.cast) contains actual PTY output from the installed wheel, labelled **no model calls**. It records version, doctor, status and Russian help. A full real-model screencast remains pending.

Public acceptance still requires explicitly authorized real Codex/Claude/mixed trials, macOS execution, the remaining fault/interactive matrix, the five-person pilot, matched five-repository OpenRig comparison and a human publication decision. See [release-checklist.md](release-checklist.md) and the reproducible real-provider commands in [eval.md](eval.md). No publication was performed.

## Boundary corrections from integration and independent review

- Independent review demonstrated two P1 defects: cancellation could be resurrected by the writer's exception handler; a successful provider leader could leave children writing after exit. Both were reproduced before changes and are guarded by runtime/process tests. Cancellation remains terminal; normal, failed and canceled calls clean up their owned group. Recovery observes surviving groups and refuses duplicate attempts without signaling stored PID records.
- `recover --plan` was observed creating state/runtime metadata. Its CLI now opens existing SQLite read-only and returns empty state without creating a directory. Two regression tests passed.
- A check modifying tracked candidate code was incorrectly recorded as passed. It now fails isolation; generated result XML remains permitted. A physically present file absent from the frozen Git commit cannot support a finding.
- CLI provider probing now uses the explicit fix override and the actual custom-team roles. Repository locale and `--lang ru` select translated help/progress/reports while JSON keys remain stable. Secret redaction runs before JSON serialization and covers quoted values and bearer headers.
- A worker now resumes a recorded native session only when its adapter confirms support. Otherwise it supplies brief saved context and records `session_restarted`; it does not promise hidden context restoration. Seat session updates are fenced by worker identity.
- Workflow usage sums recorded calls across earlier revisions, rather than only the displayed final seats. A missing/invalid attempt leaves total usage/cost unknown. Individual seat metrics still describe their displayed task.

## Post-implementation multi-agent review

The requested [multi-agent review](code-review-2026-10-05.md) found eight confirmed P1 defects across process lifecycle, current-attempt evidence, audited finding identity and interrupted workflow transitions. All eight were reproduced and corrected with regressions. Two reviewers independently checked the other implementation area's final diff; six confirmed P2 remain documented outside the requested fix scope.

Final review tree verification: `.venv/bin/pytest -q` — **323 passed, 9 skipped in 143.02 s**; Ruff and `git diff --check` passed. A newly built wheel was installed in a fresh virtual environment; packaged imports, SQLite migration and five no-model CLI commands passed. Earlier evidence above describes the original implementation checkpoint. Real-provider, macOS, pilot and comparison acceptance remain pending.

## GitHub publication preparation

2026-10-05. User selected `https://github.com/balyakin/agentcrew`, a Russian main README with an English companion, and MIT licensing attributed to **Evgeny Balyakin**. The existing GitHub repository was observed empty; local `origin` points to it and the branch is `main`. No push was performed.

- README headers contain the actual Textual [screenshot](assets/agentcrew-tui.png), explicitly labelled fictional demonstration data, and five clickable badges. The [capture script](../scripts/capture_readme.py) starts no agents. Badge images and the linked provider/uv documentation returned HTTP 200; remote workflow status will become available after publication and CI execution.
- Documentation validation checked **114 local file/image/anchor links**, **22 shell blocks** with `bash -n`, image alternative text and balanced disclosure blocks. Ruff and the staged diff whitespace check pass.
- Relevant existing tests: `.venv/bin/pytest -q tests/unit/test_tui.py tests/unit/test_cli.py` — **24 passed in 15.78 s**. Runtime implementation did not change; the full-suite result above remains the recorded code-review checkpoint.
- The rebuilt wheel contains SPDX `License-Expression: MIT`, the exact [LICENSE.md](../LICENSE.md) text, `Copyright (c) 2026 Evgeny Balyakin`, author metadata, the Russian README and the selected GitHub URLs. SHA-256: `7eaa2470d67a00c92ff1752d98efed8cfd9c2c9e5fa51614db4c21de12ef5b5a`.
- A clean export of the publication tree passed README installation from `requirements.lock`, ten no-model CLI commands and committing the generated Russian configuration. Reinstalling the built wheel then passed a site-packages import, installed MIT/author metadata, version and doctor checks. Local log: `/tmp/agentcrew-publication-check-j67hrjww/installation.log`. Paid workflow and macOS acceptance remain separate.
