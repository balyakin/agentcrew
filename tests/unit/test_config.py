import tomllib
from copy import deepcopy

import pytest

from agentcrew.models import UserError


def test_defaults_preserve_explicit_models_and_mix_providers():
    from agentcrew.config import validate_config

    result = validate_config({"default_provider": "claude", "roles": {
        "tests": {"provider": "codex", "model": "arbitrary-future-model"}}})
    assert result["language"] == "en"
    assert result["max_develop_seconds"] == 1800
    assert result["roles"]["planner"] == {"provider": "claude", "access": "read-only"}
    assert result["roles"]["implementer"]["access"] == "workspace-write"
    assert result["roles"]["tests"]["model"] == "arbitrary-future-model"
    assert result["roles"]["tests"]["provider"] == "codex"


@pytest.mark.parametrize("data", [
    {"schema_version": True}, {"schema_version": 2}, {"token": "secret"},
    {"language": "de"}, {"language": []}, {"default_provider": "unknown"},
    {"default_provider": {}},
    {"max_review_seconds": 0}, {"max_develop_seconds": -1},
    {"max_develop_seconds": 2.5}, {"max_parallel_agents": True},
    {"max_parallel_agents": 0}, {"max_revisions": -1},
    {"roles": {"invented": {}}}, {"roles": {"tests": {"access": "workspace-write"}}},
    {"roles": {"implementer": {"access": "read-only"}}},
    {"roles": {"planner": {"provider": "other"}}},
    {"roles": {"planner": {"provider": []}}},
    {"roles": {"planner": {"access": {}}}},
    {"roles": {"planner": {"permissions": "all"}}},
    {"roles": {"tests": {"model": ""}}},
    {"checks": [{"name": "unit", "argv": "pytest", "timeout_seconds": 10}]},
    {"checks": [{"name": "unit", "argv": [], "timeout_seconds": 10}]},
    {"checks": [{"name": "unit", "argv": ["pytest", 2], "timeout_seconds": 10}]},
    {"checks": [{"name": "unit", "argv": ["pytest"], "timeout_seconds": True}]},
    {"checks": [{"name": "unit", "argv": ["pytest"], "timeout_seconds": 0}]},
    {"checks": [{"name": "unit", "argv": ["pytest"], "timeout_seconds": 10,
                 "result_format": "junit", "result_file": "../outside.xml"}]},
    {"checks": [{"name": "unit", "argv": ["pytest"], "timeout_seconds": 10,
                 "result_format": "junit"}]},
    {"checks": [{"name": "unit", "argv": ["pytest"], "timeout_seconds": 10,
                 "result_format": "other"}]},
    {"checks": [{"name": "unit", "argv": ["pytest"], "timeout_seconds": 10,
                 "result_format": []}]},
    {"checks": [{"name": "unit", "argv": ["pytest"], "timeout_seconds": 10,
                 "result_file": "."}]},
])
def test_invalid_config_is_rejected(data):
    from agentcrew.config import validate_config

    with pytest.raises(UserError):
        validate_config(data)


def test_check_names_are_unique_and_validation_does_not_mutate_input():
    from agentcrew.config import validate_config

    data = {"checks": [{"name": "unit", "argv": ["python", "-m", "pytest"],
                        "timeout_seconds": 180}]}
    original = deepcopy(data)
    assert validate_config(data)["checks"][0]["argv"] == ["python", "-m", "pytest"]
    assert data == original
    data["checks"].append(deepcopy(data["checks"][0]))
    with pytest.raises(UserError):
        validate_config(data)


def test_loading_requires_init_except_explicit_no_checks(tmp_path):
    from agentcrew.config import load_config

    with pytest.raises(UserError, match="init"):
        load_config(tmp_path)
    assert load_config(tmp_path, no_checks=True)["checks"] == []
    assert not (tmp_path / ".agentcrew.toml").exists()


def test_init_writes_valid_config_without_running_checks(tmp_path):
    from agentcrew.config import init_config, load_config

    path = init_config(tmp_path, language="ru")
    assert path == tmp_path / ".agentcrew.toml"
    data = tomllib.loads(path.read_text())
    assert data["checks"][0]["argv"] == ["python", "-m", "pytest", "-q",
                                        "--junitxml=.agentcrew-results/unit.xml"]
    result = load_config(tmp_path, no_checks=True, provider="claude")
    assert result["language"] == "ru"
    assert result["checks"] == []
    assert {role["provider"] for role in result["roles"].values()} == {"claude"}
    original = path.read_bytes()
    with pytest.raises(UserError):
        init_config(tmp_path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_init_node_project_uses_argv_without_claiming_junit(tmp_path):
    from agentcrew.config import init_config, load_config

    (tmp_path / "package.json").write_text('{"scripts":{"test":"node --test"}}')
    init_config(tmp_path)
    check = load_config(tmp_path)["checks"][0]
    assert check["argv"] == ["npm", "test"]
    assert check.get("result_format", "plain") == "plain"


def test_bad_toml_and_symlinked_result_file_are_rejected(tmp_path):
    from agentcrew.config import init_config, load_config

    path = tmp_path / ".agentcrew.toml"
    path.write_text("language = [")
    with pytest.raises(UserError):
        load_config(tmp_path)
    path.unlink()
    init_config(tmp_path)
    outside = tmp_path.parent / "outside-results"
    outside.mkdir()
    (tmp_path / ".agentcrew-results").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UserError):
        load_config(tmp_path)


def template():
    return {"schema_version": 1, "name": "pair", "roles": [
        {"address": "dev-owner", "description": "Writes changes", "provider": "codex",
         "access": "workspace-write", "model": "explicit-model"},
        {"address": "dev-check", "description": "Reviews changes", "provider": "claude",
         "access": "read-only"}],
        "links": [{"from": "dev-owner", "to": "dev-check", "automatic": True}]}


def test_team_template_supports_explicit_models_and_bounded_auto_cycles():
    from agentcrew.config import validate_template

    data = template()
    assert validate_template(data)["roles"][0]["model"] == "explicit-model"
    data["links"].append({"from": "dev-check", "to": "dev-owner", "automatic": True})
    with pytest.raises(UserError):
        validate_template(data)
    data["max_steps"] = 3
    assert validate_template(data)["max_steps"] == 3


@pytest.mark.parametrize("mutation", [
    lambda d: d["roles"].append(deepcopy(d["roles"][0])),
    lambda d: d["roles"][0].update(access="danger-full-access"),
    lambda d: d["roles"][0].update(provider="unknown"),
    lambda d: d["roles"][0].update(address="../escape"),
    lambda d: d["links"][0].update(to="missing"),
    lambda d: d["links"][0].update(to=[]),
    lambda d: d.update(max_steps=0),
    lambda d: d["roles"].extend({"address": f"role-{i}", "description": "Reviewer",
                                "provider": "codex", "access": "read-only"} for i in range(7)),
])
def test_invalid_team_templates_are_rejected(mutation):
    from agentcrew.config import validate_template

    data = template()
    mutation(data)
    with pytest.raises(UserError):
        validate_template(data)
