<div align="center">

<h1>AgentCrew</h1>
<p><strong>Local AI coding teams for development and review.<br>Plans, patches and checks tied to a specific Git commit.</strong></p>

<p>
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-MIT-22c55e?style=flat-square" alt="MIT license"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12 or newer"></a>
  <a href="#providers-and-configuration"><img src="https://img.shields.io/badge/CLI-Codex%20%2B%20Claude-8b5cf6?style=flat-square" alt="Codex CLI and Claude Code providers"></a>
  <a href="https://github.com/balyakin/agentcrew/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/balyakin/agentcrew/ci.yml?branch=main&amp;label=CI&amp;style=flat-square" alt="GitHub Actions status: Linux and macOS"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/docs-EN%20%2F%20RU-f59e0b?style=flat-square" alt="English and Russian documentation"></a>
</p>

<a href="docs/assets/agentcrew-tui.png"><img src="docs/assets/agentcrew-tui.png" width="1040" alt="AgentCrew TUI showing two independent reviewers, passing unit and lint checks, and a P1 finding tied to the candidate"></a>
<p><sub>The actual TUI with demonstration data. No model calls. <a href="docs/demo.md">Reproduce this screenshot</a>.</sub></p>

<p><a href="#install">Install</a> · <a href="#your-first-task">First task</a> · <a href="#review-and-fix">Review</a> · <a href="#documentation">Documentation</a> · <a href="README.md">Русский</a></p>

</div>

Write the task in Markdown. AgentCrew starts local agents in tmux, separates implementation from review, and collects the result: a plan, a diff, checks, findings and a report. Close the interface. Come back later. The work keeps running.

Every candidate has its own Git commit. One agent writes in an isolated worktree; two reviewers inspect the frozen result independently, while project checks run against the same SHA. An explicit `apply` brings the changes into your source checkout.

Look at the screenshot. Tests pass, but a reviewer has flagged a path that could destroy an existing file. The candidate is rejected. That detail belongs in the report.

## What you can do

| Task | Command | Result |
|---|---|---|
| Build a small feature from a specification | `develop` | A plan, candidate, requirement evidence and two reviews |
| Start a CLI project from a working baseline | `new` | A Python or TypeScript template followed by the development workflow |
| Review changes against a branch | `review` | Independent correctness and test reviews with file-and-line findings |
| Fix one specific finding | `fix` | A child run with a fresh candidate and verification |
| Keep a team around | `team up` | Roles, addressed messages, an owned task queue and handoff |

Start with something you can verify: a CSV export, a bug fix, a CLI change. A task such as “make the project good” leaves too much room for guesswork.

## Install

You need **Python 3.12+**, **Git 2.38+** and **tmux 3.3+**. Target platforms are Linux and macOS; the TypeScript template also needs Node.js 22.18+. Executed platform checks are listed in the [release checklist](docs/release-checklist.md).

Install and authenticate at least one provider: [Codex CLI](https://github.com/openai/codex) or [Claude Code](https://code.claude.com/docs/en/setup). AgentCrew uses their local CLIs. There is no separate AgentCrew account.

Clone the repository and install it in a dedicated virtual environment:

```sh
git clone https://github.com/balyakin/agentcrew.git
cd agentcrew
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
agentcrew --version
agentcrew doctor --json
```

This installs AgentCrew and the locked development tools. The application itself has one third-party runtime dependency: Textual.

`doctor` checks the environment without calling a model. Inspect the selected provider's status too: infrastructure `ok` does not establish provider authentication. Claude needs an additional check of read-only mode for its installed version:

```sh
agentcrew doctor --smoke --provider claude --yes
```

**The smoke check calls a model and consumes provider usage.** Use `--provider codex` for the equivalent Codex check. Repeat Claude's policy verification after an upgrade.

## Your first task

The repository includes a [CSV export specification](examples/task.md). The baseline already has an order source and a working `status` command. Agents must write the export, including ordering, quoting, empty input and preservation of existing files.

From the AgentCrew directory, with the virtual environment active:

```sh
AGENTCREW_DEMO_DIR="$(mktemp -d)"
cp examples/task.md "$AGENTCREW_DEMO_DIR/task.md"
agentcrew new --directory "$AGENTCREW_DEMO_DIR/orders" \
  --template python-cli \
  --spec "$AGENTCREW_DEMO_DIR/task.md" \
  --provider codex
```

This command starts model work. Before the first call, AgentCrew shows the provider, permissions, source-sharing scope, project checks and time limit. Read them. Add `--yes` when you choose to accept those policies explicitly.

The launch prints an ID. Replace `RUN_ID` below with that value and inspect the result:

```sh
agentcrew plan RUN_ID --format md
agentcrew diff RUN_ID
agentcrew report RUN_ID --format md
agentcrew apply RUN_ID --dry-run
```

Once the candidate is accepted and you are satisfied with the changes, apply it and try the export:

```sh
agentcrew apply RUN_ID
cd "$AGENTCREW_DEMO_DIR/orders"
python orders_cli.py export-orders --output orders.csv
```

For TypeScript, choose `--template typescript-cli`; after applying, run `node --experimental-strip-types src/cli.ts export-orders --output orders.csv`. The `new` destination must be empty and outside any existing Git repository.

<details>
<summary><strong>I already have a project</strong></summary>

Start from a clean Git checkout. Keep the specification outside it, or commit the file before launching.

```sh
cd /path/to/your/project
agentcrew init
# Inspect .agentcrew.toml: the checks must match your project.
git add .agentcrew.toml
git commit -m "Configure AgentCrew checks"
agentcrew develop --spec /path/to/task.md --provider codex
```

`init` writes configuration without running checks. Python defaults propose pytest with JUnit and ruff; Node defaults propose `npm test`. Install your project's dependencies in an environment accessible from isolated worktrees. Use the same `plan`, `diff`, `report` and `apply` commands afterwards.

</details>

## Review and fix

Use a clean feature branch with committed changes relative to `main`. Create and inspect `.agentcrew.toml` with `init` first if it is missing.

```sh
agentcrew review --base main --provider codex
agentcrew report RUN_ID --format md
agentcrew finding RUN_ID FINDING_ID
```

The launch and report provide `RUN_ID` and `FINDING_ID`. Findings carry distinct levels of confirmation:

| Status | Evidence |
|---|---|
| `reported` | One reviewer's claim; still a hypothesis |
| `corroborated` | Independent reviewers agree |
| `reproduced` | A check result or an audited manual confirmation |

Choose a finding to fix:

```sh
agentcrew fix RUN_ID --finding FINDING_ID
agentcrew report FIX_RUN_ID --format md
agentcrew diff FIX_RUN_ID
agentcrew apply FIX_RUN_ID --dry-run
agentcrew apply FIX_RUN_ID
```

`FIX_RUN_ID` is the new run's ID. It has its own commit, checks and reviews. Earlier passing tests cannot stand in for fresh verification.

Without check configuration, you can explicitly choose `review --no-checks`. Its report is partial.

## What counts as done

**Specification → plan → one writer → candidate commit → checks and two reviews → decision.** The workflow can revise the candidate; the default limit is two extra revisions.

Planned requirements are matched to this candidate's check results. Automated criteria need JUnit output and exact test IDs: pytest `tests/file.py::test_name` or native Node test names. A green suite without the named test leaves the criterion `unknown`. A failed reviewer also blocks acceptance.

`apply` requires an unchanged source HEAD, a clean checkout and an accepted candidate. Use `--dry-run` to inspect the transfer first. Applying an unaccepted result requires the separate `--allow-unaccepted` option; examine the report before choosing it.

<details>
<summary><strong>Background work, TUI and recovery</strong></summary>

```sh
agentcrew status --json
agentcrew tui
agentcrew attach RUN_ID correctness
agentcrew pause RUN_ID
agentcrew resume RUN_ID
agentcrew answer RUN_ID --body-file answer.md
agentcrew cancel RUN_ID
agentcrew recover --plan
```

In the TUI: `r` opens a report, `a` attaches to a role, `f` filters, `?` opens help and `q` exits. CLI and TUI read the same saved state.

Add `--detach` to return after scheduling. Ctrl-C detaches the waiting client; leaving the TUI also keeps tmux workers running. `cancel` explicitly stops the run. After a reboot, inspect the recovery plan first: tmux processes are gone, and interrupted writes are never silently replayed.

Record an observation when confirming a manual criterion:

```sh
agentcrew confirm RUN_ID REQUIREMENT_ID --status passed \
  --evidence "What I checked and the result I observed"
```

Exit codes: `0` command success, `1` unaccepted workflow, `2` invalid input or environment, `3` process or storage failure, `130` client detachment. Reading a report successfully does not change the workflow outcome.

</details>

<details>
<summary><strong>Persistent teams and the queue</strong></summary>

```sh
agentcrew team up --template pair --cwd . --provider codex
agentcrew team ps
agentcrew send dev-owner@TEAM_ID --body-file note.md --verify
agentcrew queue create --to dev-owner@TEAM_ID --body-file task.md
agentcrew queue list --team TEAM_ID
agentcrew queue handoff TASK_ID --to dev-check@TEAM_ID \
  --note "Review the frozen result"
agentcrew team stop TEAM_ID --snapshot
agentcrew team resume TEAM_ID
```

Messages persist. Tasks have an owner; handoff requires a controller-frozen commit or a hashed non-code artifact. Message `--verify` confirms inbox delivery. Reading and execution are separate.

The `pair` template provides one writer and one reviewer. Standard `develop` acceptance requires two independent reviewers. Other templates include `review`, `adversarial-review` and a [custom-team example](examples/sample-team.toml).

</details>

## Providers and configuration

Project settings live in `.agentcrew.toml`: language, roles, providers, models, checks and limits. Model names are explicit settings; there is no bundled model catalogue.

Assign Codex and Claude to different roles through `[roles.<role>].provider`. Omit `--provider` when launching to preserve those assignments. Defaults allow 15 minutes for review, 30 for develop and two simultaneous agent calls.

`--lang ru` selects Russian help, progress and text reports. Otherwise the project configuration selects the language, with English as the default. Command names and JSON keys stay stable.

## Boundaries and project status

AgentCrew runs on your machine. Worktrees separate changes from the source branch; they are not an operating-system sandbox. Checks execute repository code, and your selected provider may receive source files. Consider those settings when choosing a project and account.

State lives in `$XDG_STATE_HOME/agentcrew` or `~/.local/state/agentcrew`. Raw traces can contain code and private data. Read the [storage and export policy](docs/security.md) before sharing reports.

**Stage: prerelease.** Package metadata says `1.0.0`; public acceptance is unfinished. The last full local Linux run recorded **323 passed, 9 skipped**; the nine skips require paid real-provider checks. [Review findings and fixes](docs/code-review-2026-10-05.md) were recorded on October 5, 2026. Real Codex/Claude workflows, macOS and the user pilot remain in the [acceptance checklist](docs/release-checklist.md).

## Documentation

| Document | Open it to… |
|---|---|
| [Architecture](docs/architecture.md) | Understand roles, candidates and saved state |
| [Recovery and troubleshooting](docs/troubleshooting.md) | Investigate an interrupted run or a partial report |
| [Security](docs/security.md) | Check permissions, source sharing and log storage |
| [Demonstration](docs/demo.md) | Reproduce the screenshot or watch recorded CLI output |
| [Contributing](CONTRIBUTING.md) | Prepare a change and run local checks |
| [Changelog](CHANGELOG.md) | See what the current version contains |
| [Evaluation protocol](docs/eval.md) | Assess quality on real tasks |

## License

[MIT](LICENSE.md) · Copyright (c) 2026 **Evgeny Balyakin**.
