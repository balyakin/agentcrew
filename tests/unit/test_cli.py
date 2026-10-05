import json
import subprocess
import sys


def test_unknown_command():
    result = subprocess.run(
        [sys.executable, "-m", "agentcrew", "unrecognized"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_doctor_json():
    result = subprocess.run(
        [sys.executable, "-m", "agentcrew", "doctor", "--json"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert all(set(c) >= {"name", "status", "detail", "remedy"} for c in json.loads(result.stdout)["checks"])


def test_recovery_preview_does_not_create_state(tmp_path, monkeypatch):
    from agentcrew.cli import main

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert main(["recover", "--plan", "--json"]) == 0
    assert not (tmp_path / "agentcrew").exists()


def test_recovery_preview_does_not_initialize_runtime_metadata(tmp_path, monkeypatch):
    from agentcrew.cli import main
    from agentcrew.db import Database

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    db = Database()
    db.create_scope("team", str(tmp_path))
    before = db.path.read_bytes()
    assert main(["recover", "--plan", "--json"]) == 0
    assert db.rows("SELECT * FROM meta") == []
    assert db.path.read_bytes() == before
    db.close()


def test_fix_provider_override_is_probed_before_launch(tmp_path, monkeypatch):
    from agentcrew import cli
    from agentcrew.config import validate_config
    from agentcrew.db import Database
    from agentcrew.models import canonical
    from agentcrew.review import fix

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    db = Database()
    parent = db.create_scope("run", str(tmp_path))
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) VALUES(?,?,?,?,?,?)",
        (
            parent,
            "review",
            "a" * 40,
            "b" * 40,
            "report",
            canonical(validate_config({"default_provider": "claude"})),
        ),
    )
    probed = []
    monkeypatch.setattr(cli, "check_providers", lambda config: probed.append(config))
    monkeypatch.setattr(fix, "start_fix", lambda *args, **kwargs: "child")
    monkeypatch.setattr(cli, "wait_run", lambda *args: 0)
    assert cli.main(["fix", parent, "--finding", "F-001", "--provider", "codex", "--yes"]) == 0
    assert {role["provider"] for role in probed[0]["roles"].values()} == {"codex"}
    assert db.run(parent)["config"]["default_provider"] == "claude"
    db.close()


def test_team_checks_and_discloses_actual_custom_providers(tmp_path, monkeypatch):
    from agentcrew import cli, git_snapshot, teams

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    template = tmp_path / "custom.toml"
    template.write_text(
        'schema_version = 1\nname = "custom"\nlinks = []\n[[roles]]\naddress = "reader"\ndescription = "Read"\nprovider = "claude"\naccess = "read-only"\n'
    )
    monkeypatch.setattr(git_snapshot, "preflight", lambda *args: {"changed_files": []})
    monkeypatch.setattr(teams, "up", lambda *args: "team")
    probed = []
    monkeypatch.setattr(cli, "check_providers", lambda config: probed.append(config))
    assert cli.main(["team", "up", "--template", str(template), "--cwd", str(tmp_path), "--yes"]) == 0
    assert {role["provider"] for role in probed[0]["roles"].values()} == {"claude"}
    assert set(probed[0]["roles"]) == {"reader"}


def test_russian_cli_honors_repository_language_and_preserves_json(tmp_path, monkeypatch, capsys):
    from agentcrew.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / ".agentcrew.toml").write_text('language = "ru"\n')
    assert main(["status"]) == 0
    assert "Статус" in capsys.readouterr().out
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"scopes": []}
    assert main(["status", "--lang", "en"]) == 0
    assert "Статус" not in capsys.readouterr().out


def test_russian_help_translates_text_but_keeps_command_names(tmp_path, monkeypatch, capsys):
    import pytest

    from agentcrew.cli import main

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as result:
        main(["--lang", "ru", "--help"])
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert "Использование" in text and "разработ" in text and "develop" in text


def test_json_output_redacts_nested_secrets_without_changing_keys(capsys):
    from argparse import Namespace

    from agentcrew.cli import emit

    emit(
        {
            "tasks": [
                {"id": "one", "body": 'api_key="privatevalue"', "status": "done", "password": "anothersecret"}
            ]
        },
        Namespace(json=True, lang="en", command="queue"),
    )
    text = capsys.readouterr().out
    assert "privatevalue" not in text and "anothersecret" not in text
    data = json.loads(text)
    assert data["tasks"][0]["id"] == "one" and data["tasks"][0]["status"] == "done"
