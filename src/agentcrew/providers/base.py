"""Provider contract and bounded execution of an owned process group."""

import json
import math
import os
import re
import selectors
import shutil
import subprocess
import time
from dataclasses import fields
from pathlib import Path

from ..doctor import _has_flag, _provider, _run
from ..models import (
    MAX_JSON,
    MAX_LOG,
    CapabilityReport,
    ProviderResult,
    UserError,
    canonical,
    load_json,
    now,
    redact,
    validate_schema,
)

TERMINATION_GRACE = 2


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("Non-finite JSON number")


def _number(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def decode_json(raw: bytes | str):
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant, parse_float=_number)
    except (ValueError, UnicodeError) as exc:
        raise UserError(f"Invalid provider JSON: {exc}", "provider_output_invalid") from exc


def events(path: Path):
    try:
        if path.stat().st_size > MAX_LOG:
            raise UserError("Provider events exceed the file limit", "provider_output_too_large")
        with path.open("rb") as stream:
            while raw := stream.readline(MAX_JSON + 1):
                if len(raw) > MAX_JSON:
                    raise UserError("Provider event exceeds the line limit", "provider_output_too_large")
                if not raw.strip():
                    continue
                event = decode_json(raw)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise UserError("Provider event must be an object with a type", "provider_output_invalid")
                yield event
    except OSError as exc:
        raise UserError(f"Cannot read provider events: {exc}", "provider_output_invalid") from exc


def final_json(path: Path) -> dict:
    try:
        value = load_json(path)
    except OSError as exc:
        raise UserError(f"Cannot read provider final result: {exc}", "provider_output_invalid") from exc
    if not isinstance(value, dict):
        raise UserError("Provider final result must be an object", "provider_output_invalid")
    return value


def reported_cost(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise UserError("Invalid reported provider cost", "provider_output_invalid")
    return float(value)


def _private(path: Path, *, create=True):
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if create else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "wb")


def _stop(proc: subprocess.Popen):
    from ..processes import stop_group

    stop_group(proc, TERMINATION_GRACE)


def _execute(
    argv,
    cwd,
    prompt,
    timeout,
    events_stream,
    log_stream,
    result_path,
    *,
    heartbeat=None,
    canceled=None,
    started=None,
):
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    streams = (proc.stdin, proc.stdout, proc.stderr)
    deadline = time.monotonic() + timeout
    heartbeat_at = 0.0
    prompt_bytes = prompt.encode("utf-8")
    position = 0
    sizes = {"events": 0, "log": 0}
    pending = b""
    group_stopped = False
    try:
        if started:
            started(proc)
        with selectors.DefaultSelector() as selector:
            for stream, label in zip(streams, ("prompt", "events", "log"), strict=True):
                os.set_blocking(stream.fileno(), False)
                selector.register(
                    stream, selectors.EVENT_WRITE if label == "prompt" else selectors.EVENT_READ, label
                )
            while True:
                return_code = proc.poll()
                if return_code is not None and not group_stopped:
                    # Descendants can keep inherited pipes open after the provider exits.
                    _stop(proc)
                    group_stopped = True
                if return_code is not None and not selector.get_map():
                    return return_code
                instant = time.monotonic()
                if heartbeat and instant >= heartbeat_at:
                    heartbeat()
                    heartbeat_at = instant + 0.5
                if canceled and canceled():
                    raise UserError(
                        "Provider task was canceled; partial artifacts retained", "provider_canceled"
                    )
                if instant >= deadline:
                    raise UserError("Provider task timed out; partial artifacts retained", "provider_timeout")
                if result_path.stat().st_size > MAX_JSON:
                    raise UserError("Provider final result exceeds the limit", "provider_output_too_large")
                wait = min(0.1, max(0, deadline - instant))
                if not selector.get_map():
                    time.sleep(wait)
                    continue
                for key, _ in selector.select(timeout=wait):
                    stream, label = key.fileobj, key.data
                    if label == "prompt":
                        try:
                            if position < len(prompt_bytes):
                                position += os.write(
                                    stream.fileno(), prompt_bytes[position : position + 4096]
                                )
                            if position == len(prompt_bytes):
                                selector.unregister(stream)
                                stream.close()
                        except BrokenPipeError:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    allowed = MAX_LOG - sizes[label]
                    destination = events_stream if label == "events" else log_stream
                    destination.write(chunk[:allowed])
                    destination.flush()
                    sizes[label] += len(chunk)
                    if sizes[label] > MAX_LOG:
                        raise UserError("Provider output exceeds the file limit", "provider_output_too_large")
                    if label == "events":
                        lines = (pending + chunk).split(b"\n")
                        pending = lines.pop()
                        if len(pending) > MAX_JSON or any(len(line) + 1 > MAX_JSON for line in lines):
                            raise UserError(
                                "Provider event exceeds the line limit", "provider_output_too_large"
                            )
                        for line in lines:
                            if line.strip():
                                event = decode_json(line)
                                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                                    raise UserError("Provider event has no type", "provider_output_invalid")
    finally:
        if not group_stopped:
            _stop(proc)
        for stream in streams:
            stream.close()


class ProviderAdapter:
    name: str

    def __init__(self, *, read_only_attestation=None):
        self.read_only_attestation = read_only_attestation
        self._report = None
        self._helps = {}

    @property
    def binary(self):
        binary = shutil.which(self.name)
        if not binary:
            raise UserError(f"{self.name} is not installed", "provider_unavailable")
        return binary

    def probe(self) -> CapabilityReport:
        self._report, _ = _provider(self.name)
        return CapabilityReport(**{item.name: self._report[item.name] for item in fields(CapabilityReport)})

    def help(self, command: tuple[str, ...]) -> str:
        if command not in self._helps:
            code, output = _run([self.binary, *command, "--help"])
            self._helps[command] = "" if code else output
        return self._helps[command]

    def require_flags(self, help_text: str, flags):
        missing = [flag for flag in flags if not _has_flag(help_text, flag)]
        if missing:
            raise UserError(
                f"{self.name} help does not advertise required flags: {', '.join(missing)}",
                "provider_capability_missing",
            )

    def require(self, task: dict):
        if not isinstance(task, dict) or task.get("access") not in {"read-only", "workspace-write"}:
            raise UserError("Task access must be read-only or workspace-write")
        for key in ("model", "session_id"):
            value = task.get(key)
            if value is not None and (
                not isinstance(value, str)
                or not value
                or value.startswith("-")
                or len(value) > 256
                or "\x00" in value
            ):
                raise UserError(f"Invalid provider {key}")
        capability = (
            self.probe()
            if self._report is None
            else CapabilityReport(**{item.name: self._report[item.name] for item in fields(CapabilityReport)})
        )
        if not capability.available or capability.version == "unknown":
            raise UserError(f"{self.name} is unavailable: {capability.detail}", "provider_unavailable")
        if not capability.authenticated:
            raise UserError(
                f"{self.name} is not authenticated: {capability.remedy}", "provider_unauthenticated"
            )
        if self._report["missing_flags"]:
            raise UserError(
                f"{self.name} is missing flags: {', '.join(self._report['missing_flags'])}",
                "provider_capability_missing",
            )
        if task["access"] == "read-only" and not capability.read_only:
            raise UserError(
                f"{self.name} read-only mode is not verified: {capability.remedy}", "provider_mode_unverified"
            )
        if task["access"] == "workspace-write" and not capability.workspace_write:
            raise UserError(
                f"{self.name} has no supported workspace-write mode", "provider_capability_missing"
            )
        return capability

    def build_command(self, task: dict, cwd: Path, output_path: Path) -> list[str]:
        raise NotImplementedError

    def parse_result(self, path: Path, events_path: Path) -> ProviderResult:
        raise NotImplementedError

    def resume_capability(self) -> bool:
        raise NotImplementedError

    def run(
        self, task: dict, cwd: Path, output_dir: Path, *, heartbeat=None, canceled=None
    ) -> ProviderResult:
        task = dict(task)
        timeout = task.get("timeout_seconds")
        prompt = task.get("prompt")
        if (
            type(timeout) is not int
            or timeout < 1
            or not isinstance(prompt, str)
            or len(prompt.encode()) > MAX_JSON
        ):
            raise UserError("Task requires a positive integer timeout and a bounded prompt")
        if not isinstance(task.get("schema"), dict):
            raise UserError("Task requires a JSON Schema object")
        if task.get("read_only_attestation") is not None:
            self.read_only_attestation = task["read_only_attestation"]
        self._report, self._helps = None, {}
        cwd, output_dir = Path(cwd).resolve(), Path(output_dir)
        if not cwd.is_dir() or output_dir.is_symlink():
            raise UserError("Invalid provider working or output directory")
        output_dir = output_dir.resolve()
        if task.get("schema_path"):
            task["schema_path"] = str(Path(task["schema_path"]).resolve())
        result_path = output_dir / "result.json"
        argv = self.build_command(task, cwd, result_path)
        if task.get("schema_path") and final_json(Path(task["schema_path"])) != task["schema"]:
            raise UserError("Task schema and schema file disagree")
        artifact_names = ("events.jsonl", "provider.log", "result.json", "metadata.json")
        if any((output_dir / name).exists() or (output_dir / name).is_symlink() for name in artifact_names):
            raise UserError(
                "Provider output already exists; inspect it and use a new attempt directory",
                "provider_output_exists",
            )
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_dir.chmod(0o700)
        events_path, log_path = output_dir / "events.jsonl", output_dir / "provider.log"
        metadata_path = output_dir / "metadata.json"
        metadata = {
            "provider": self.name,
            "version": self._report["version"],
            "argv": argv,
            "requested_model": task.get("model"),
            "started_at": now(),
            "status": "running",
        }
        with _private(result_path), _private(metadata_path) as stream:
            stream.write(canonical(metadata).encode())
        try:
            with _private(events_path) as event_stream, _private(log_path) as log_stream:

                def started(proc):
                    from ..processes import identity

                    record = identity(proc.pid)
                    if record is None:
                        return
                    record.update(task_id=task.get("_task_id"), worker_instance=task.get("_worker_instance"))
                    with _private(output_dir / "process.json") as stream:
                        stream.write(canonical(record).encode())

                return_code = _execute(
                    argv,
                    cwd,
                    prompt,
                    timeout,
                    event_stream,
                    log_stream,
                    result_path,
                    heartbeat=heartbeat,
                    canceled=canceled,
                    started=started,
                )
            if return_code:
                detail = redact(log_path.read_text(errors="replace")[-4000:])
                if re.search(r"(?i)permission.{0,80}(?:denied|required)|approval.{0,80}required", detail):
                    raise UserError(f"{self.name} requires permission: {detail}", "permission_required")
                # Parse explicit provider errors, including structured permission denials.
                try:
                    self.parse_result(result_path, events_path)
                except UserError as exc:
                    if exc.code in {"permission_required", "provider_failed"}:
                        raise
                raise UserError(f"{self.name} exited {return_code}: {detail}", "provider_failed")
            result = self.parse_result(result_path, events_path)
            validate_schema(result.data, task["schema"])
            result_path.chmod(0o600)
            metadata.update(
                status="completed",
                session_id=result.session_id,
                model=result.model,
                usage=result.usage,
                cost_usd=result.cost_usd,
                exit_code=return_code,
            )
            return result
        except BaseException as exc:
            metadata.update(
                status="failed", error_code=getattr(exc, "code", "provider_failed"), error=redact(str(exc))
            )
            raise
        finally:
            metadata["finished_at"] = now()
            with _private(metadata_path, create=False) as stream:
                stream.write(canonical(metadata).encode())


def get_adapter(name: str, *, read_only_attestation=None) -> ProviderAdapter:
    if name == "codex":
        from .codex import CodexAdapter

        return CodexAdapter(read_only_attestation=read_only_attestation)
    if name == "claude":
        from .claude import ClaudeAdapter

        return ClaudeAdapter(read_only_attestation=read_only_attestation)
    raise UserError(f"Unknown provider: {name}")
