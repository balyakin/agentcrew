# Contributing

Work on a small branch with the documented [workflow boundaries](docs/architecture.md) in mind. Do not claim completion from a provider's message, an exit code alone, or a mocked response. Real-provider tests are separate and require explicit permission to spend model usage.

## Local checks

Install Python 3.12+, Git 2.38+, tmux 3.3+ and Node 22.18+, then from this checkout:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m ruff check .
python -m pytest -q
python -m pip wheel --no-deps . --wheel-dir dist
agentcrew doctor --json
```

The default suite uses disposable Git repositories, real tmux/SQLite, native pytest/Node checks and test-only provider executables. It requires no paid account. A fake adapter exercises protocol and recovery; it does not measure a real model's ability to find or fix a defect.

Write a failing behavioral test first for changes to validation, state transitions, candidate evidence or recovery. Keep configuration/check boundaries strict and use argv arrays. Preserve source worktrees, prior candidates and logs after failures. Never replay a crashed writer automatically. Do not put test-only provider responses in production code.

## CI

[ci.yml](.github/workflows/ci.yml) configures Linux and macOS, Python 3.12/3.13, Node 24 and real tmux. Local Linux evidence is recorded in [progress.md](docs/progress.md); a configured macOS job is not a completed macOS test. The workflow uses the maintainers' documented [checkout](https://github.com/actions/checkout), [Python setup](https://github.com/actions/setup-python) and [Node setup](https://github.com/actions/setup-node) actions.

## Review and release evidence

Describe the concrete trigger, changed behavior and checks you ran. Include candidate/scope boundaries for changes involving evidence. Update [release-checklist.md](docs/release-checklist.md) only with actual results, environment/version, date and an artifact or command. Keep failures visible when a change fixes them.

Before a public 1.0 release, finish real Codex and Claude runs, both CSV fixtures, recovery fault injection, fresh installation/README runs on both platforms, the five-person pilot and the fair comparison described in [eval.md](docs/eval.md). Record unknown costs as unknown. Publishing the repository, package or demo is a separate human action.

The main [README](README.md) is Russian; keep [README.en.md](README.en.md) in sync. Public code and documentation use the [MIT license](LICENSE.md), Copyright (c) 2026 Evgeny Balyakin. Regenerate the header screenshot using the [documented capture commands](docs/demo.md#readme-screenshot); label demonstration data explicitly.

For a bug, provide `agentcrew doctor --json`, reproduction steps, outcome and a redacted report. Do not attach source code or raw provider logs by default. See [security.md](docs/security.md).
