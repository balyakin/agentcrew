"""Doctor probes disposable infrastructure and provider CLIs without using models."""

import importlib
import json
import sys
from pathlib import Path

import pytest

CODEX_HELP = """Commands:
  resume  Resume a previous session
Options:
  --json
  --output-schema <FILE>
  --output-last-message <FILE>
  -C, --cd <DIR>
  --sandbox <MODE> [possible values: read-only, workspace-write, danger-full-access]
"""
CLAUDE_HELP = """Commands:
  auth  Manage authentication
Options:
  -p, --print
  --output-format <FORMAT> [possible values: text, json, stream-json]
  --json-schema <SCHEMA>
  --permission-mode <MODE> [possible values: default, plan, acceptEdits, bypassPermissions]
  --resume <ID>
"""

FAKE_BINARY = r'''
import json
import os
from pathlib import Path
import sys
import time

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["DOCTOR_CALLS"], "a") as stream:
    stream.write(json.dumps({"binary": name, "args": args}) + "\n")
if name == "git":
    command = "--version" if args == ["--version"] else (
        "rev-parse" if "rev-parse" in args else "init" if "init" in args else "unknown")
elif name == "tmux":
    command = "-V" if args == ["-V"] else next(
        (item for item in args if item in {"new-session", "has-session", "kill-server"}), "unknown")
else:
    command = " ".join(args)
key = name + " " + command
responses = json.loads(os.environ["DOCTOR_RESPONSES"])
if key in responses:
    response = responses[key]
    if len(response) > 2:
        time.sleep(response[2])
    print(response[1])
    sys.exit(response[0])
if name == "git" and command == "init":
    if os.environ.get("GIT_DIR") or os.environ.get("GIT_WORK_TREE"):
        sys.exit(93)
    root = Path(args[-1])
    (root / ".git").mkdir(parents=True)
elif name == "git" and command == "rev-parse":
    root = Path(args[args.index("-C") + 1])
    if not (root / ".git").exists():
        sys.exit(94)
    print("true")
elif name == "tmux" and command != "unknown":
    if "-S" not in args:
        sys.exit(95)
    socket = Path(args[args.index("-S") + 1])
    if command == "new-session":
        if "-f" not in args or args[args.index("-f") + 1] != os.devnull:
            sys.exit(96)
        socket.touch()
    elif command == "has-session":
        if not socket.exists():
            sys.exit(97)
    elif command == "kill-server":
        socket.unlink(missing_ok=True)
else:
    print("Unexpected command: " + key)
    sys.exit(98)
'''


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls_path = tmp_path / "calls.jsonl"
    calls_path.touch()
    monkeypatch.setenv("PATH", str(binary_dir))
    monkeypatch.setenv("DOCTOR_CALLS", str(calls_path))

    def configure(*, installed=("git", "tmux", "codex", "claude"), responses=None):
        defaults = {
            "git --version": [0, "git version 2.43.0"],
            "tmux -V": [0, "tmux 3.4"],
            "codex --version": [0, "codex-cli 0.160.0"],
            "codex exec --help": [0, CODEX_HELP],
            "codex login --help": [0, "Commands:\n  status  Show login status"],
            "codex login status": [0, "Logged in using ChatGPT"],
            "claude --version": [0, "2.1.100 (Claude Code)"],
            "claude --help": [0, CLAUDE_HELP],
            "claude auth --help": [0, "Commands:\n  status  Show authentication status"],
            "claude auth status": [0, '{"loggedIn": true, "authMethod": "claude.ai"}'],
        }
        defaults.update(responses or {})
        monkeypatch.setenv("DOCTOR_RESPONSES", json.dumps(defaults))
        for name in installed:
            binary = binary_dir / name
            binary.write_text(f"#!{sys.executable}\n" + FAKE_BINARY)
            binary.chmod(0o700)
        return calls_path

    return configure


def run_doctor(**kwargs):
    return importlib.import_module("agentcrew.doctor").doctor(**kwargs)


def checks(report):
    return {check["name"]: check for check in report["checks"]}


def calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_doctor_checks_functional_infrastructure_and_provider_flags(fake_tools):
    fake_tools()
    report = run_doctor()
    assert report["ok"] is True
    assert all(set(check) == {"name", "status", "detail", "remedy"} for check in report["checks"])
    assert checks(report)["git"]["status"] == "ok"
    assert checks(report)["tmux"]["status"] == "ok"
    assert report["providers"]["codex"]["authenticated"] is True
    assert report["providers"]["codex"]["read_only"] is True
    assert report["providers"]["codex"]["workspace_write"] is True
    assert "--output-schema" in report["providers"]["codex"]["supported_flags"]
    assert report["providers"]["claude"]["authenticated"] is True
    assert report["providers"]["claude"]["read_only"] is False
    assert checks(report)["claude.capabilities"]["status"] == "warning"
    assert "smoke" in checks(report)["claude.capabilities"]["remedy"].lower()


def test_doctor_uses_only_disposable_tmux_server_and_git_repository(fake_tools, monkeypatch):
    calls_path = fake_tools(installed=("git", "tmux", "codex"))
    monkeypatch.setenv("TMUX", "/tmp/user-tmux,1,0")
    monkeypatch.setenv("GIT_DIR", "/tmp/user-repo/.git")
    run_doctor(providers=("codex",))
    operations = [call["args"] for call in calls(calls_path) if call["binary"] == "tmux"
                  and call["args"] != ["-V"]]
    sockets = [args[args.index("-S") + 1] for args in operations]
    assert len(set(sockets)) == 1
    assert sockets[0] != "/tmp/user-tmux"
    assert "kill-server" in operations[-1]
    assert not Path(sockets[0]).parent.exists()
    git_init = next(call["args"] for call in calls(calls_path) if "init" in call["args"])
    assert not Path(git_init[-1]).exists()


@pytest.mark.parametrize(("tool", "output"), [("git", "git version 2.37.9"), ("tmux", "tmux 3.2a")])
def test_old_infrastructure_is_a_hard_failure(fake_tools, tool, output):
    flag = "--version" if tool == "git" else "-V"
    fake_tools(responses={f"{tool} {flag}": [0, output]})
    report = run_doctor(providers=())
    assert report["ok"] is False
    assert checks(report)[tool]["status"] == "error"
    assert checks(report)[tool]["remedy"]


@pytest.mark.parametrize("tool", ["git", "tmux"])
def test_missing_infrastructure_is_a_hard_failure(fake_tools, tool):
    fake_tools(installed=tuple(name for name in ("git", "tmux") if name != tool))
    report = run_doctor(providers=())
    assert report["ok"] is False
    assert checks(report)[tool]["status"] == "error"


@pytest.mark.parametrize(("response", "check"), [
    ({"git init": [1, "disk full"]}, "git"),
    ({"tmux new-session": [1, "cannot create server"]}, "tmux"),
    ({"tmux has-session": [1, "session disappeared"]}, "tmux"),
])
def test_installed_but_broken_infrastructure_fails_functional_check(fake_tools, response, check):
    calls_path = fake_tools(responses=response)
    report = run_doctor(providers=())
    assert report["ok"] is False
    assert checks(report)[check]["status"] == "error"
    if check == "tmux":
        assert "kill-server" in calls(calls_path)[-1]["args"]


def test_missing_provider_is_reported_without_failing_infrastructure(fake_tools):
    fake_tools(installed=("git", "tmux", "codex"))
    report = run_doctor()
    assert report["ok"] is True
    assert report["providers"]["codex"]["authenticated"] is True
    assert report["providers"]["claude"]["available"] is False
    assert checks(report)["claude.version"]["status"] == "warning"
    assert checks(report)["claude.version"]["remedy"]


def test_missing_flag_is_not_confused_with_longer_flag_name(fake_tools):
    fake_tools(responses={"codex exec --help": [0, CODEX_HELP.replace("--json\n", "--json-extra\n")]})
    report = run_doctor(providers=("codex",))
    assert report["ok"] is True
    assert report["providers"]["codex"]["missing_flags"] == ["--json"]
    assert checks(report)["codex.capabilities"]["status"] == "warning"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_unauthorized_provider_never_gets_a_model_or_fallback_call(fake_tools, provider):
    auth_command = "login status" if provider == "codex" else "auth status"
    calls_path = fake_tools(responses={f"{provider} {auth_command}": [1, "Not logged in"]})
    report = run_doctor(providers=(provider,))
    assert report["ok"] is True
    assert report["providers"][provider]["authenticated"] is False
    assert checks(report)[f"{provider}.auth"]["status"] == "warning"
    provider_calls = [call["args"] for call in calls(calls_path) if call["binary"] == provider]
    assert all(args == ["--version"] or args[-1] == "--help" or args == auth_command.split()
               for args in provider_calls)
    assert not any(call["binary"] == ("claude" if provider == "codex" else "codex")
                   for call in calls(calls_path))


def test_claude_without_auth_subcommand_does_not_interpret_auth_as_a_prompt(fake_tools):
    calls_path = fake_tools(responses={"claude --help": [0, CLAUDE_HELP.replace("  auth  Manage authentication", "")]})
    report = run_doctor(providers=("claude",))
    assert report["providers"]["claude"]["authenticated"] is False
    assert checks(report)["claude.auth"]["status"] == "warning"
    assert not any("auth" in call["args"] for call in calls(calls_path))


def test_auth_error_details_redact_tokens_and_control_sequences(fake_tools):
    fake_tools(responses={"codex login status": [1, "\x1b[31mtoken=private-value\x1b[0m"]})
    report = run_doctor(providers=("codex",))
    output = json.dumps(report)
    assert "private-value" not in output
    assert "[REDACTED]" in output
    assert "\\u001b" not in output


def test_probe_timeout_is_reported_without_retry_or_model_call(fake_tools, monkeypatch):
    fake_tools(responses={"codex login status": [0, "Logged in using ChatGPT", 1]})
    module = importlib.import_module("agentcrew.doctor")
    monkeypatch.setattr(module, "PROBE_TIMEOUT", 0.1)
    report = module.doctor(providers=("codex",))
    assert report["ok"] is True
    assert report["providers"]["codex"]["authenticated"] is False
    assert "timed out" in checks(report)["codex.auth"]["detail"]


def test_old_python_is_a_hard_failure(fake_tools, monkeypatch):
    fake_tools()
    module = importlib.import_module("agentcrew.doctor")
    monkeypatch.setattr(module.sys, "version_info", (3, 11, 10))
    report = module.doctor(providers=())
    assert report["ok"] is False
    assert checks(report)["python"]["status"] == "error"


def test_paid_smoke_is_unavailable_before_adapter_integration(fake_tools):
    from agentcrew.models import UserError

    calls_path = fake_tools()
    with pytest.raises(UserError) as error:
        run_doctor(smoke=True)
    assert error.value.code == "smoke_unavailable"
    assert calls(calls_path) == []


def test_unknown_provider_is_rejected_before_any_subprocess(fake_tools):
    from agentcrew.models import UserError

    calls_path = fake_tools()
    with pytest.raises(UserError):
        run_doctor(providers=("unknown",))
    assert calls(calls_path) == []
