"""Claude print-mode commands, version-bound review attestation and results."""

import os
from pathlib import Path

from ..models import CapabilityReport, ProviderResult, UserError, canonical, load_json, redact
from .base import ProviderAdapter, _private, events, reported_cost


class ClaudeAdapter(ProviderAdapter):
    name = "claude"

    def probe(self) -> CapabilityReport:
        capability = super().probe()
        help_text = self.help(()) if capability.available else ""
        flags = ["--verbose", "--tools", "--disallowedTools"]
        try:
            self.require_flags(help_text, flags)
            complete = not self._report["missing_flags"] and "stream-json" in help_text
        except UserError:
            complete = False
        capability.workspace_write = complete and "acceptEdits" in self._report["permission_modes"]
        attestation = self.read_only_attestation
        if attestation is None:
            state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agentcrew"
            attestation = state / "providers" / "claude-read-only.json"
        if isinstance(attestation, (str, Path)):
            try:
                attestation = load_json(Path(attestation))
            except (OSError, UserError):
                attestation = None
        capability.read_only = (complete and "plan" in self._report["permission_modes"] and
                                isinstance(attestation, dict) and attestation.get("provider") == "claude" and
                                attestation.get("version") == capability.version and
                                attestation.get("read_only_verified") is True)
        if capability.read_only:
            capability.detail = "Required flags advertised; version-bound read-only smoke attestation present"
            capability.remedy = ""
        self._report.update(read_only=capability.read_only, workspace_write=capability.workspace_write,
                            detail=capability.detail, remedy=capability.remedy)
        return capability

    def resume_capability(self) -> bool:
        if self._report is None:
            self.probe()
        return bool(self._report["resume"])

    def build_command(self, task: dict, cwd: Path, output_path: Path) -> list[str]:
        self.require(task)
        help_text = self.help(())
        self.require_flags(help_text, ["--verbose", "--tools", "--disallowedTools"])
        if not isinstance(task.get("schema"), dict):
            raise UserError("Claude requires a JSON Schema object")
        mode = "plan" if task["access"] == "read-only" else "acceptEdits"
        argv = [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--json-schema",
                canonical(task["schema"]), "--permission-mode", mode, "--disallowedTools", "mcp__*"]
        if task["access"] == "read-only":
            argv += ["--tools", "Read,Glob,Grep"]
        for optional in ("--bare", "--permission-prompts"):
            try:
                self.require_flags(help_text, [optional])
            except UserError:
                continue
            argv += [optional] + (["none"] if optional == "--permission-prompts" else [])
        if task.get("model"):
            self.require_flags(help_text, ["--model"])
            argv += ["--model", task["model"]]
        if task.get("session_id"):
            if not self.resume_capability():
                raise UserError("Claude resume is not advertised by this version", "provider_resume_unavailable")
            argv += ["--resume", task["session_id"]]
        return argv

    def parse_result(self, path: Path, events_path: Path) -> ProviderResult:
        final = None
        session_id = model = None
        for event in events(events_path):
            if event["type"] == "system" and event.get("subtype") == "init":
                session_id, model = event.get("session_id"), event.get("model")
            if event["type"] == "result":
                if final is not None:
                    raise UserError("Claude emitted multiple terminal results", "provider_output_invalid")
                final = event
        if final is None:
            raise UserError("Claude stream contains no terminal result", "provider_output_invalid")
        if final.get("permission_denials"):
            raise UserError("Claude needs unavailable non-interactive tool permission", "permission_required")
        if final.get("is_error") is True or final.get("subtype", "success") != "success":
            detail = redact(str(final.get("errors", final.get("result", "Claude task failed"))))
            if "permission" in detail.lower():
                raise UserError(detail, "permission_required")
            raise UserError(detail, "provider_failed")
        value = final.get("structured_output")
        if not isinstance(value, dict):
            raise UserError("Claude has no structured_output object", "provider_output_invalid")
        session_id = final.get("session_id", session_id)
        usage = final.get("usage", {})
        if not isinstance(usage, dict) or (session_id is not None and not isinstance(session_id, str)):
            raise UserError("Invalid Claude usage or session ID", "provider_output_invalid")
        if model is not None and not isinstance(model, str):
            raise UserError("Invalid Claude model", "provider_output_invalid")
        # Keep the raw structured final separate from the event stream.
        with _private(path, create=not path.exists()) as stream:
            stream.write(canonical(value).encode())
        return ProviderResult(value, session_id=session_id, model=model, usage=usage,
                              cost_usd=reported_cost(final.get("total_cost_usd")))
