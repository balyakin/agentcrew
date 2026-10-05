"""Provider contracts run local fake executables, never paid models."""

import importlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from agentcrew.models import UserError

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
          "properties": {"ok": {"type": "boolean"}}}
CODEX_HELP = """Commands:
  resume Resume session
Options:
  --json --output-schema --output-last-message -C --cd --sandbox --model
  --config --ignore-user-config --ignore-rules
  [possible values: read-only, workspace-write, danger-full-access]
"""
ROOT_HELP = "Options:\n --sandbox -C --cd --ask-for-approval --model"
RESUME_HELP = "Options:\n --json --output-schema --output-last-message --model"
CLAUDE_HELP = """Commands:
  auth Manage auth
Options:
  -p --print --output-format stream-json --json-schema --permission-mode
  plan acceptEdits bypassPermissions --tools --disallowedTools --verbose
  --resume --model --bare --permission-prompts
"""

FAKE_PROVIDER = r'''
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

name = Path(sys.argv[0]).name
args = sys.argv[1:]
settings = json.loads(os.environ["PROVIDER_SETTINGS"])
probe = settings["probes"].get(name + " " + " ".join(args))
with open(os.environ["PROVIDER_CALLS"], "a") as stream:
    stream.write(json.dumps({"provider": name, "args": args, "probe": probe is not None}) + "\n")
if probe is not None:
    print(probe[1])
    sys.exit(probe[0])
prompt = sys.stdin.read()
Path(os.environ["PROVIDER_PROMPT"]).write_text(prompt)
mode = settings.get("mode", "success")
if mode in {"hang", "child"}:
    if mode == "child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(os.environ["PROVIDER_CHILD"]).write_text(str(child.pid))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)
if mode == "line_overflow":
    print("x" * 4096, flush=True)
elif mode == "log_overflow":
    for _ in range(100):
        print(json.dumps({"type": "progress", "text": "x" * 100}), flush=True)
elif mode == "stderr_overflow":
    print("x" * 4096, file=sys.stderr, flush=True)
elif mode == "malformed":
    print("this is not JSON", flush=True)
elif mode == "permission":
    print(json.dumps({"type": "result", "is_error": True,
                      "permission_denials": [{"tool_name": "Bash"}], "result": "Permission denied"}))
else:
    for event in settings.get("events", []):
        print(json.dumps(event), flush=True)
if name == "codex" and "--output-last-message" in args:
    output = Path(args[args.index("--output-last-message") + 1])
    output.write_text(settings.get("raw", '{"ok":true}'))
if settings.get("stderr"):
    print(settings["stderr"], file=sys.stderr)
sys.exit(settings.get("exit_code", 0))
'''


@pytest.fixture
def provider_fixture(tmp_path, monkeypatch):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    calls.touch()
    prompt = tmp_path / "prompt.txt"
    child = tmp_path / "child.txt"
    monkeypatch.setenv("PATH", str(binary_dir))
    monkeypatch.setenv("PROVIDER_CALLS", str(calls))
    monkeypatch.setenv("PROVIDER_PROMPT", str(prompt))
    monkeypatch.setenv("PROVIDER_CHILD", str(child))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for provider in ("codex", "claude"):
        executable = binary_dir / provider
        executable.write_text(f"#!{sys.executable}\n" + FAKE_PROVIDER)
        executable.chmod(0o700)

    def configure(provider="codex", *, mode="success", events=None, raw='{"ok":true}',
                  exit_code=0, stderr="", overrides=None):
        probes = {
            "codex --version": [0, "codex-cli 0.160.0"],
            "codex --help": [0, ROOT_HELP],
            "codex exec --help": [0, CODEX_HELP],
            "codex exec resume --help": [0, RESUME_HELP],
            "codex login --help": [0, "Commands:\n status Show login status"],
            "codex login status": [0, "Logged in using ChatGPT"],
            "claude --version": [0, "2.1.100 (Claude Code)"],
            "claude --help": [0, CLAUDE_HELP],
            "claude auth --help": [0, "Commands:\n status Show authentication status"],
            "claude auth status": [0, '{"loggedIn":true}'],
        }
        probes.update(overrides or {})
        if events is None:
            events = ([{"type": "thread.started", "thread_id": "codex-thread"},
                       {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}}]
                      if provider == "codex" else
                      [{"type": "system", "subtype": "init", "session_id": "claude-session", "model": "test-model"},
                       {"type": "result", "subtype": "success", "is_error": False,
                        "session_id": "claude-session", "structured_output": {"ok": True},
                        "usage": {"input_tokens": 12, "output_tokens": 4}, "total_cost_usd": 0.001}])
        settings = {"probes": probes, "mode": mode, "events": events, "raw": raw,
                    "exit_code": exit_code, "stderr": stderr}
        monkeypatch.setenv("PROVIDER_SETTINGS", json.dumps(settings))
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps(SCHEMA))
        task = {"schema_path": str(schema_path), "schema": SCHEMA, "access": "read-only",
                "prompt": "Inspect this fictitious project.", "timeout_seconds": 2}
        return task, tmp_path / "output", calls, prompt, child

    return configure


def adapter(provider="codex", **kwargs):
    return importlib.import_module("agentcrew.providers.base").get_adapter(provider, **kwargs)


def attestation(version="2.1.100 (Claude Code)"):
    return {"provider": "claude", "version": version, "read_only_verified": True}


def test_codex_command_uses_explicit_sandbox_schema_and_safe_approval(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture()
    command = adapter().build_command(task, tmp_path, output / "result.json")
    assert command[command.index("exec") + 1] == "--json"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--output-schema") + 1] == task["schema_path"]
    assert command[command.index("--output-last-message") + 1] == str(output / "result.json")
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert command[command.index("--ask-for-approval") + 1] == "on-request"
    assert command[-1] == "-"
    assert "never" not in command
    assert "danger-full-access" not in command


def test_codex_resume_places_global_flags_before_exec(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture()
    task.update(session_id="previous-session", model="chosen-model")
    provider = adapter()
    assert provider.resume_capability() is True
    command = provider.build_command(task, tmp_path, output / "result.json")
    assert command[command.index("exec") + 1] == "resume"
    assert command.index("--sandbox") < command.index("exec")
    assert command.index("-C") < command.index("exec")
    assert command[-2:] == ["previous-session", "-"]
    assert command[command.index("--model") + 1] == "chosen-model"


def test_unverified_resume_does_not_guess_or_silently_restart(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture(overrides={"codex exec resume --help": [0, "--json"]})
    task["session_id"] = "previous-session"
    provider = adapter()
    assert provider.resume_capability() is False
    with pytest.raises(UserError) as error:
        provider.build_command(task, tmp_path, output / "result.json")
    assert error.value.code == "provider_resume_unavailable"


def test_claude_requires_matching_attestation_and_restricts_review_tools(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture("claude")
    with pytest.raises(UserError) as error:
        adapter("claude").build_command(task, tmp_path, output / "result.json")
    assert error.value.code == "provider_mode_unverified"
    with pytest.raises(UserError):
        adapter("claude", read_only_attestation=attestation("old-version")).build_command(
            task, tmp_path, output / "result.json")
    provider = adapter("claude", read_only_attestation=attestation())
    assert provider.probe().read_only is True
    command = provider.build_command(task, tmp_path, output / "result.json")
    assert "-p" in command
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert command[command.index("--disallowedTools") + 1] == "mcp__*"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command
    assert "--dangerously-skip-permissions" not in command


def test_claude_writer_uses_accept_edits_without_readonly_attestation(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture("claude")
    task["access"] = "workspace-write"
    command = adapter("claude").build_command(task, tmp_path, output / "result.json")
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "bypassPermissions" not in command


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_adapter_run_preserves_result_session_usage_and_private_artifacts(provider_fixture, tmp_path, provider):
    task, output, calls_path, prompt, _ = provider_fixture(provider)
    kwargs = {"read_only_attestation": attestation()} if provider == "claude" else {}
    result = adapter(provider, **kwargs).run(task, tmp_path, output)
    assert result.data == {"ok": True}
    assert result.session_id == ("codex-thread" if provider == "codex" else "claude-session")
    assert result.usage == {"input_tokens": 12, "output_tokens": 4}
    assert result.cost_usd == (None if provider == "codex" else 0.001)
    assert result.model == (None if provider == "codex" else "test-model")
    assert prompt.read_text() == task["prompt"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for name in ("events.jsonl", "provider.log", "result.json", "metadata.json"):
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600
    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["provider"] == provider
    assert metadata["session_id"] == result.session_id
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert len([call for call in calls if not call["probe"]]) == 1


@pytest.mark.parametrize(("mode", "code"), [
    ("malformed", "provider_output_invalid"),
    ("line_overflow", "provider_output_too_large"),
    ("log_overflow", "provider_output_too_large"),
    ("stderr_overflow", "provider_output_too_large"),
])
def test_stream_errors_are_bounded_and_never_reported_as_clean_results(
        provider_fixture, tmp_path, monkeypatch, mode, code):
    task, output, _, _, _ = provider_fixture(mode=mode)
    base = importlib.import_module("agentcrew.providers.base")
    monkeypatch.setattr(base, "MAX_JSON", 1024)
    monkeypatch.setattr(base, "MAX_LOG", 2048)
    with pytest.raises(UserError) as error:
        adapter().run(task, tmp_path, output)
    assert error.value.code == code
    assert json.loads((output / "metadata.json").read_text())["status"] == "failed"
    assert (output / "events.jsonl").stat().st_size <= 2048
    assert (output / "provider.log").stat().st_size <= 2048


def test_timeout_terminates_only_the_owned_provider_process_group(provider_fixture, tmp_path, monkeypatch):
    task, output, _, _, child_file = provider_fixture(mode="child")
    task["timeout_seconds"] = 1
    base = importlib.import_module("agentcrew.providers.base")
    monkeypatch.setattr(base, "TERMINATION_GRACE", 0.1)
    started = time.monotonic()
    with pytest.raises(UserError) as error:
        adapter().run(task, tmp_path, output)
    assert error.value.code == "provider_timeout"
    assert time.monotonic() - started < 4
    child_pid = int(child_file.read_text())
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        # Linux may retain a terminated orphan as a zombie until its parent reaps it.
        process_status = Path(f"/proc/{child_pid}/stat")
        assert process_status.exists() and process_status.read_text().split()[2] == "Z"


def test_cancellation_retains_partial_output_and_heartbeat(provider_fixture, tmp_path, monkeypatch):
    task, output, _, prompt, _ = provider_fixture(mode="hang")
    base = importlib.import_module("agentcrew.providers.base")
    monkeypatch.setattr(base, "TERMINATION_GRACE", 0.1)
    heartbeats = []
    with pytest.raises(UserError) as error:
        adapter().run(task, tmp_path, output, heartbeat=lambda: heartbeats.append(True),
                      canceled=lambda: prompt.exists())
    assert error.value.code == "provider_canceled"
    assert heartbeats
    assert (output / "events.jsonl").exists()


@pytest.mark.parametrize("raw", ['{}', '{"ok":"yes"}', '{"ok":true,"extra":1}', '{"ok":NaN}',
                                  '{"ok":true,"ok":false}'])
def test_raw_final_is_strictly_validated_against_task_schema(provider_fixture, tmp_path, raw):
    task, output, _, _, _ = provider_fixture(raw=raw)
    with pytest.raises(UserError) as error:
        adapter().run(task, tmp_path, output)
    assert error.value.code == "provider_output_invalid"


def test_nonzero_provider_exit_keeps_raw_result_but_fails(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture(exit_code=1, stderr="unexpected model failure")
    with pytest.raises(UserError) as error:
        adapter().run(task, tmp_path, output)
    assert error.value.code == "provider_failed"
    assert (output / "result.json").read_text() == '{"ok":true}'


def test_claude_permission_denial_requires_attention(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture("claude", mode="permission")
    with pytest.raises(UserError) as error:
        adapter("claude", read_only_attestation=attestation()).run(task, tmp_path, output)
    assert error.value.code == "permission_required"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_missing_terminal_event_cannot_use_an_old_or_partial_final(provider_fixture, tmp_path, provider):
    task, output, _, _, _ = provider_fixture(provider, events=[])
    kwargs = {"read_only_attestation": attestation()} if provider == "claude" else {}
    with pytest.raises(UserError) as error:
        adapter(provider, **kwargs).run(task, tmp_path, output)
    assert error.value.code == "provider_output_invalid"


def test_existing_artifacts_are_preserved_and_not_overwritten(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture()
    output.mkdir()
    (output / "result.json").write_text("partial candidate")
    with pytest.raises(UserError):
        adapter().run(task, tmp_path, output)
    assert (output / "result.json").read_text() == "partial candidate"


def test_unauthorized_provider_does_not_submit_prompt(provider_fixture, tmp_path):
    task, output, _, prompt, _ = provider_fixture(overrides={"codex login status": [1, "Not logged in"]})
    with pytest.raises(UserError) as error:
        adapter().run(task, tmp_path, output)
    assert error.value.code == "provider_unauthenticated"
    assert not prompt.exists()


def test_unknown_provider_is_rejected():
    with pytest.raises(UserError):
        adapter("unsupported")


def test_relative_artifact_paths_are_resolved_before_changing_provider_cwd(provider_fixture, tmp_path, monkeypatch):
    task, _, _, _, _ = provider_fixture()
    monkeypatch.chdir(tmp_path)
    task["schema_path"] = "schema.json"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    result = adapter().run(task, worktree, Path("output"))
    assert result.data == {"ok": True}
    assert (tmp_path / "output" / "result.json").exists()
    assert not (worktree / "output").exists()


def test_json_exponent_overflow_is_invalid_in_events():
    base = importlib.import_module("agentcrew.providers.base")
    with pytest.raises(UserError) as error:
        base.decode_json('{"type":"turn.completed","usage":{"input_tokens":1e999}}')
    assert error.value.code == "provider_output_invalid"


@pytest.mark.parametrize(("key", "value"), [("access", "danger-full-access"), ("model", "--unsafe"),
                                             ("session_id", "--last"), ("timeout_seconds", True)])
def test_invalid_task_options_never_submit_a_model_prompt(provider_fixture, tmp_path, key, value):
    task, output, _, prompt, _ = provider_fixture()
    task[key] = value
    with pytest.raises(UserError):
        adapter().run(task, tmp_path, output)
    assert not prompt.exists()


@pytest.mark.parametrize(("provider", "terminal", "code"), [
    ("codex", {"type": "turn.failed", "error": {"message": "model unavailable"}}, "provider_failed"),
    ("codex", {"type": "error", "message": "approval required"}, "permission_required"),
    ("claude", {"type": "result", "is_error": True, "result": "model unavailable"}, "provider_failed"),
    ("claude", {"type": "result", "subtype": "success", "is_error": False}, "provider_output_invalid"),
    ("claude", {"type": "result", "structured_output": {"ok": True}, "total_cost_usd": -1},
     "provider_output_invalid"),
])
def test_terminal_provider_errors_are_explicit(provider_fixture, tmp_path, provider, terminal, code):
    task, output, _, _, _ = provider_fixture(provider, events=[terminal])
    kwargs = {"read_only_attestation": attestation()} if provider == "claude" else {}
    with pytest.raises(UserError) as error:
        adapter(provider, **kwargs).run(task, tmp_path, output)
    assert error.value.code == code


def test_absent_usage_and_cost_remain_unknown(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture(events=[{"type": "turn.completed"}])
    result = adapter().run(task, tmp_path, output)
    assert result.usage == {}
    assert result.cost_usd is None
    assert result.model is None


def test_claude_reads_stored_attestation_without_modifying_it(provider_fixture, tmp_path):
    task, output, _, _, _ = provider_fixture("claude")
    proof = tmp_path / "state" / "agentcrew" / "providers" / "claude-read-only.json"
    proof.parent.mkdir(parents=True)
    content = json.dumps(attestation())
    proof.write_text(content)
    assert adapter("claude").run(task, tmp_path, output).data == {"ok": True}
    assert proof.read_text() == content
