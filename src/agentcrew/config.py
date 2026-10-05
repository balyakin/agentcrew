"""Explicit repository configuration; validation never executes project code."""

import json
import re
import tomllib
from copy import deepcopy
from pathlib import Path, PurePosixPath

from agentcrew.models import MAX_JSON, UserError

PROVIDERS = {"codex", "claude"}
ROLE_ACCESS = {"planner": "read-only", "implementer": "workspace-write",
               "correctness": "read-only", "tests": "read-only"}
DEFAULTS = {"schema_version": 1, "language": "en", "default_provider": "codex",
            "max_review_seconds": 900, "max_develop_seconds": 1800,
            "max_revisions": 2, "max_parallel_agents": 2}


def _fail(message):
    raise UserError(f"Invalid configuration: {message}", "config_invalid")


def _object(value, allowed, required=()):
    if type(value) is not dict or set(value) - set(allowed) or set(required) - set(value):
        _fail("missing or unknown fields")


def _text(value, label):
    if type(value) is not str or not value.strip() or len(value) > 4096 or "\x00" in value:
        _fail(f"{label} must be a nonempty string")


def _integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")


def _choice(value, allowed, label):
    if type(value) is not str or value not in allowed:
        _fail(f"unknown {label}")


def _relative(value):
    _text(value, "path")
    rel = PurePosixPath(value)
    if (not rel.parts or rel.is_absolute() or ".." in rel.parts or "\\" in value
            or re.match(r"^[A-Za-z]:", value)):
        _fail("path must remain inside its worktree")
    return Path(*rel.parts)


def _role(value, *, provider, access=None):
    _object(value, {"provider", "access", "model", "description", "timeout_seconds"})
    result = {"provider": provider, "access": access or "read-only", **deepcopy(value)}
    _choice(result["provider"], PROVIDERS, "provider")
    _choice(result["access"], {"read-only", "workspace-write"}, "access")
    if access and result["access"] != access:
        _fail("access does not match role")
    for key in ("model", "description"):
        if key in result:
            _text(result[key], key)
    if "timeout_seconds" in result:
        _integer(result["timeout_seconds"], "timeout_seconds")
    return result


def validate_config(data: dict) -> dict:
    _object(data, {*DEFAULTS, "roles", "checks"})
    result = {**DEFAULTS, **deepcopy(data)}
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        _fail("unsupported schema_version")
    _choice(result["language"], {"en", "ru"}, "language")
    _choice(result["default_provider"], PROVIDERS, "default_provider")
    for key in ("max_review_seconds", "max_develop_seconds", "max_parallel_agents", "max_revisions"):
        _integer(result[key], key, 0 if key == "max_revisions" else 1)
    roles = result.get("roles", {})
    _object(roles, ROLE_ACCESS)
    result["roles"] = {name: _role(roles.get(name, {}), provider=result["default_provider"], access=access)
                       for name, access in ROLE_ACCESS.items()}
    checks = result.setdefault("checks", [])
    if type(checks) is not list or len(checks) > 200:
        _fail("checks must be an array with at most 200 items")
    names = set()
    for check in checks:
        _object(check, {"name", "argv", "timeout_seconds", "result_format", "result_file"},
                {"name", "argv", "timeout_seconds"})
        _text(check["name"], "check name")
        if check["name"] in names:
            _fail("duplicate check name")
        names.add(check["name"])
        if type(check["argv"]) is not list or not check["argv"] or len(check["argv"]) > 200:
            _fail("check argv must be a nonempty array")
        for argument in check["argv"]:
            if type(argument) is not str or "\x00" in argument or len(argument) > 4096:
                _fail("argv arguments must be strings")
        _text(check["argv"][0], "check executable")
        _integer(check["timeout_seconds"], "check timeout_seconds")
        _choice(check.get("result_format", "plain"), {"plain", "junit"}, "result_format")
        if check.get("result_format") == "junit" and "result_file" not in check:
            _fail("junit needs result_file")
        if "result_file" in check:
            _relative(check["result_file"])
    return result


def load_config(repo: Path, *, no_checks=False, provider=None) -> dict:
    path = repo / ".agentcrew.toml"
    try:
        base = repo.resolve(strict=True)
        if path.exists():
            if not path.resolve(strict=True).is_relative_to(base) or path.stat().st_size > MAX_JSON:
                _fail("config path or size is invalid")
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        elif no_checks:
            data = {}
        else:
            raise UserError("Missing .agentcrew.toml; run agentcrew init first", "config_missing")
        result = validate_config(data)
        if provider is not None:
            _choice(provider, PROVIDERS, "provider override")
            result["default_provider"] = provider
            for role in result["roles"].values():
                role["provider"] = provider
        for check in result["checks"]:
            if "result_file" in check:
                target = (base / _relative(check["result_file"])).resolve()
                if not target.is_relative_to(base):
                    _fail("check result_file escapes its worktree")
        if no_checks:
            result["checks"] = []
        return result
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise UserError(f"Cannot load configuration: {exc}", "config_invalid") from exc


def init_config(repo: Path, language="en") -> Path:
    validate_config({"language": language})
    if not repo.is_dir():
        raise UserError("Repository directory does not exist", "config_invalid")
    lines = [f"{key} = {json.dumps(language if key == 'language' else value)}"
             for key, value in DEFAULTS.items()]
    for role, access in ROLE_ACCESS.items():
        lines.extend(["", f"[roles.{role}]", 'provider = "codex"', f'access = "{access}"'])
    if (repo / "package.json").is_file():
        lines.extend(["", "[[checks]]", 'name = "unit"', 'argv = ["npm", "test"]',
                      "timeout_seconds = 180"])
    else:
        lines.extend(["", "[[checks]]", 'name = "unit"',
                      'argv = ["python", "-m", "pytest", "-q", "--junitxml=.agentcrew-results/unit.xml"]',
                      "timeout_seconds = 180", 'result_format = "junit"',
                      'result_file = ".agentcrew-results/unit.xml"', "", "[[checks]]", 'name = "lint"',
                      'argv = ["ruff", "check", "."]', "timeout_seconds = 120"])
    path = repo / ".agentcrew.toml"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise UserError(f"Cannot create configuration: {exc}", "config_invalid") from exc
    return path


def validate_template(data: dict) -> dict:
    _object(data, {"schema_version", "name", "roles", "links", "max_steps"},
            {"schema_version", "name", "roles", "links"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _fail("unsupported template schema_version")
    _text(data["name"], "template name")
    if type(data["roles"]) is not list or not 1 <= len(data["roles"]) <= 8:
        _fail("template must contain 1 to 8 seats")
    if "max_steps" in data:
        _integer(data["max_steps"], "max_steps")
    addresses = set()
    for role in data["roles"]:
        _object(role, {"address", "provider", "access", "description", "model", "timeout_seconds"},
                {"address", "provider", "access"})
        address = role["address"]
        if type(address) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", address):
            _fail("invalid seat address")
        if address in addresses:
            _fail("duplicate seat address")
        addresses.add(address)
        _role({key: value for key, value in role.items() if key != "address"}, provider="codex")
    if type(data["links"]) is not list or len(data["links"]) > 64:
        _fail("links must be an array with at most 64 items")
    edges = {address: [] for address in addresses}
    links = set()
    for link in data["links"]:
        _object(link, {"from", "to", "automatic"}, {"from", "to", "automatic"})
        _text(link["from"], "link source")
        _text(link["to"], "link destination")
        if link["from"] not in addresses or link["to"] not in addresses or type(link["automatic"]) is not bool:
            _fail("invalid link")
        pair = (link["from"], link["to"])
        if pair in links:
            _fail("duplicate link")
        links.add(pair)
        if link["automatic"]:
            edges[link["from"]].append(link["to"])
    active, done = set(), set()

    def visit(address):
        if address in active:
            _fail("automatic handoff cycle requires max_steps")
        if address not in done:
            active.add(address)
            for target in edges[address]:
                visit(target)
            active.remove(address)
            done.add(address)

    if "max_steps" not in data:
        for address in addresses:
            visit(address)
    return deepcopy(data)
