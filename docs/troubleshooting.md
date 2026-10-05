# Troubleshooting

Start with `agentcrew doctor --json`, then `agentcrew status --json` and `agentcrew report RUN_ID --format json`. Read provider warnings separately from the infrastructure `ok` field.

| Symptom | Meaning and next step |
|---|---|
| `dirty_source` | Commit or otherwise save staged, modified and untracked files. `init` creates a new config; explicitly commit `.agentcrew.toml` or exclude it yourself in `.git/info/exclude`. Keep task files outside the source or commit them. |
| `config_missing` | Run `agentcrew init`, inspect its argv checks, then save the config. Only standalone `review --no-checks` may proceed without it. |
| Provider unavailable/unauthenticated | Install or authenticate the selected CLI yourself and repeat doctor. An unused missing Claude CLI does not prevent an explicitly selected working Codex. No fallback provider is chosen. |
| Claude read-only capability unavailable | Run the explicitly consented `doctor --smoke --provider claude --yes`. A successful record is bound to its CLI version; repeat after upgrades. |
| `provider_output_invalid` | The result violated a schema, bound or candidate path/line rule. One format retry is bounded; afterwards inspect the failed task and preserved output. Zero findings does not replace this failure. |
| JUnit result missing or unsafe | Configure `result_format = "junit"`, a relative `result_file`, and argv which creates it in the current worktree. Old committed reports are removed from the fresh check copy before launch. |
| Requirement `unknown` despite green tests | The exact planned test ID was absent, skipped, duplicated, suppressed or not supported by the report. pytest uses `tests/file.py::test_name`; native Node uses the actual name. A general check PASS is insufficient. |
| Manual criterion `unknown` | Inspect the observable result, then use `confirm RUN_ID REQUIREMENT_ID --status passed\|failed --evidence "What I checked"`. The audited decision applies only to that candidate. |
| `needs_attention` with a question | Read `plan RUN_ID --format md`, write the answer to a file, then `answer RUN_ID --body-file answer.md`. Do not replace a failed requirement with a weaker one. |
| Apply refuses changed HEAD | The source moved after launch. Preserve the candidate and inspect its diff; start a run from the new source instead of forcing a stale patch into it. |
| Apply refuses a dirty source | Save your local changes before retry. Dry-run performs the same source and conflict checks. |
| Partial reviewer report | See the failed seat's reason and task. After stopping the old worker, use `queue retry TASK_ID` for that role; this reopens evaluation of the current candidate. Retrying a reviewer does not replay a writer. |
| Writer crashed | Stop/verify the old process, inspect its preserved worktree and `recover --plan`. `queue retry TASK_ID --inspect-writer` authorizes one attempt to continue the partial patch at the original HEAD; changed HEAD, branch, ownership or escaping paths are refused. |
| `queue complete` refuses a workflow task | This command records a manual team artifact or `no_artifact` reason. A run task needs a validated provider/check result; inspect the failure and use `queue retry` instead. |
| Team handoff refused | The result must have a controller-frozen commit or hashed artifact. Uncommitted writer changes cannot be handed to a checker as a verified result. |

## Disconnect, pause, cancel, reboot

Ctrl-C detaches a waiting launch command and returns 130. Work remains in the private tmux server; use `status`, `attach RUN_ID ROLE` or `tui` to return. Exiting TUI does not stop workers.

`pause RUN_ID` stops issuing tasks while the current writer can finish. `resume RUN_ID` continues an ordinary pause. `cancel RUN_ID` stops that run and preserves partial work. After a process crash or reboot, use `recover --plan` first; tmux itself does not survive a reboot. Recovery does not promise exactly-once effects of a writing model call.

`team stop TEAM_ID --snapshot` saves seats, tasks and artifact references. `team resume TEAM_ID` reconstructs from saved state; an interrupted writer may still require inspection. Messages may be redelivered after a crash between `delivered` and `acknowledged`. A repeated idempotency key does not create a second inbox message.

## Reporting a bug

Include OS and tool versions, the exact command, a fictional reproduction, doctor JSON and a redacted report with candidate SHA. Omit raw logs by default. The issue template asks for this evidence. Keep failed checks visible; changing the result text does not fix the underlying behavior.
