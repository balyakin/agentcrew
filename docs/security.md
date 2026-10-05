# Security and privacy

AgentCrew operates on one local machine. An isolated Git worktree protects the source branch from accidental writes; it does **not** prevent repository code or a permitted provider process from accessing other local files or the network.

## Before launch

The first agent launch shows the selected provider, repository, changed files, access policy, test commands and time limit. Trust is saved for the repository and configuration hash. A policy change needs new consent. `--yes` is an explicit acceptance of those policies and local execution of the project's checks, not an account login or a guarantee that the project is harmless.

The selected provider may receive source code. Authenticate the provider CLI yourself; AgentCrew neither asks for API keys nor stores credentials in `.agentcrew.toml`. It does not edit your `AGENTS.md`, `CLAUDE.md`, provider settings, tmux config, global Git settings or hooks.

## Process and data boundaries

- Commands use argv arrays. Model `reproduction` text is displayed, never executed.
- Provider policies are read-only for planner/reviewers and workspace-write for implementers. Claude read-only support requires a smoke-test record for the installed version. Help text alone is insufficient.
- Candidate and report paths are resolved before access. Absolute prefixes, traversal, NUL and escaping symlinks are rejected. Native Node JUnit's absolute testcase paths are accepted only when they resolve inside the checks-worktree.
- Provider JSON is limited to 1 MiB; logs, result XML and inspected source files are limited to 20 MiB. XML DTDs/entities are rejected. Timeouts terminate the owned process group.
- JSON contracts reject extra fields and invalid types. A reviewer must cite an existing line in the frozen candidate. A textual connection to the diff is a guard against unrelated claims; it cannot establish semantic truth.
- A late/stale result cannot become current lease or candidate evidence. Crashed writers are preserved for inspection, not silently reissued.

Normal provider completion also terminates remaining descendants in its owned process group. Recovery observes a surviving group even when its leader has exited and blocks retry; stored PID records never authorize automatic signals. `recover --plan` opens the existing database read-only and neither initializes state nor starts processes.

Checks run repository code on your machine. Native provider policies do not isolate a test command. Run only projects whose code you trust.

## Stored data

State lives under `$XDG_STATE_HOME/agentcrew` or `~/.local/state/agentcrew`. The state directory is `0700`; the SQLite database and raw logs are `0600`. The database stores durable messages, task bodies, candidate identities, results and audit decisions. Raw provider traces can include source code and prompts.

Normal reports redact common secret patterns and strip terminal control sequences. They omit raw traces and full source. `--include-logs` is a deliberate request for more sensitive material; inspect it before sharing. Redaction is pattern based and does not guarantee removal of every secret. `export --redacted` is the intended shareable export path.

Cleanup first shows recorded resources and ownership. Worktrees containing changes are preserved. There is no automatic deletion of active runs, no automatic push, package publication or PR creation. Publication is a separate human action.

For a security issue, prepare a small private reproduction without actual credentials. Do not publish private source, raw state databases or provider traces in a public issue.
