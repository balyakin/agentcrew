"""Shared values and strict, bounded data at process boundaries."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

MAX_JSON = 1024 * 1024
MAX_LOG = 20 * 1024 * 1024
STATES = {
    "run": {"preparing", "running", "paused", "needs_attention", "completed", "failed", "canceled"},
    "seat": {"starting", "ready", "busy", "stopped", "crashed"},
    "task": {"pending", "claimed", "done", "failed", "blocked", "canceled"},
    "message": {"queued", "delivered", "acknowledged", "failed"},
    "finding": {"reported", "corroborated", "reproduced", "dismissed", "fixed"},
}


class UserError(Exception):
    def __init__(self, message: str, code: str = "invalid_input", exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def uid() -> str:
    return str(uuid4())


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def load_json(path: Path, limit: int = MAX_JSON):
    if path.stat().st_size > limit:
        raise UserError("JSON exceeds size limit", "provider_output_invalid")
    try:

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")),
        )
    except (ValueError, UnicodeError) as exc:
        raise UserError(f"Invalid JSON: {exc}", "provider_output_invalid") from exc


def validate_schema(value, schema: dict, label: str = "result"):
    """Validate the small JSON Schema subset used by the three closed contracts."""
    kind = schema.get("type")
    good = {
        "object": lambda: type(value) is dict,
        "array": lambda: type(value) is list,
        "string": lambda: type(value) is str,
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float),
        "boolean": lambda: type(value) is bool,
    }
    if kind in good and not good[kind]():
        raise UserError(f"{label}: expected {kind}", "provider_output_invalid")
    if "enum" in schema and value not in schema["enum"]:
        raise UserError(f"{label}: unsupported value", "provider_output_invalid")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise UserError(f"{label}: missing or extra fields", "provider_output_invalid")
        for key, child in schema["properties"].items():
            validate_schema(value[key], child, f"{label}.{key}")
    elif kind == "array":
        if len(value) > 200:
            raise UserError(f"{label}: too many items", "provider_output_invalid")
        for item in value:
            validate_schema(item, schema["items"], label)
    elif kind == "string" and len(value) > 4096:
        raise UserError(f"{label}: string too long", "provider_output_invalid")
    elif kind == "number":
        import math

        if not math.isfinite(value):
            raise UserError(f"{label}: non-finite number", "provider_output_invalid")
    return value


def sanitize(text: str) -> str:
    text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


def redact(text: str) -> str:
    text = sanitize(text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{8,}", "Bearer [REDACTED]", text)
    text = re.sub(
        r"""(?i)((?:api[_-]?key|token|password|secret|authorization)\b(?:\\?["'])?\s*[=:]\s*(?:\\?["'])?)[^\s,;"'\\}]+""",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})\b", "[REDACTED]", text)


def redact_data(value):
    """Redact strings before serialization so JSON keys and types stay intact."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if isinstance(child, str)
            and re.fullmatch(r"(?i)(api[_-]?key|token|password|secret|authorization)", str(key))
            else redact_data(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_data(child) for child in value]
    return value


@dataclass
class CapabilityReport:
    provider: str
    available: bool
    version: str = "unknown"
    authenticated: bool = False
    read_only: bool = False
    workspace_write: bool = False
    resume: bool = False
    detail: str = ""
    remedy: str = ""


@dataclass
class ProviderResult:
    data: dict
    session_id: str | None = None
    model: str | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
