"""Codex exec commands and strict structured-result parsing."""

from pathlib import Path

from ..models import ProviderResult, UserError, redact
from .base import ProviderAdapter, events, final_json, reported_cost


class CodexAdapter(ProviderAdapter):
    name = "codex"

    def resume_capability(self) -> bool:
        capability = self.probe() if self._report is None else None
        if not (capability.resume if capability else self._report["resume"]):
            return False
        try:
            self.require_flags(self.help(()), ["--sandbox", "-C", "--ask-for-approval"])
            self.require_flags(self.help(("exec", "resume")), ["--json", "--output-schema", "--output-last-message"])
            return True
        except UserError:
            return False

    def build_command(self, task: dict, cwd: Path, output_path: Path) -> list[str]:
        self.require(task)
        root_help = self.help(())
        self.require_flags(root_help, ["--ask-for-approval"])
        argv = [self.binary, "--ask-for-approval", "on-request"]
        resuming = bool(task.get("session_id"))
        if resuming:
            if not self.resume_capability():
                raise UserError("This Codex CLI has no verified structured resume path", "provider_resume_unavailable")
            argv += ["--sandbox", task["access"], "-C", str(cwd), "exec", "resume", "--json"]
            help_text = self.help(("exec", "resume"))
        else:
            argv += ["exec", "--json", "--sandbox", task["access"], "-C", str(cwd)]
            help_text = self.help(("exec",))
        if task.get("model"):
            self.require_flags(help_text, ["--model"])
            argv += ["--model", task["model"]]
        if not task.get("schema_path"):
            raise UserError("Codex requires a schema_path")
        argv += ["--output-schema", str(task["schema_path"]), "--output-last-message", str(output_path)]
        if resuming:
            argv += [task["session_id"]]
        return argv + ["-"]

    def parse_result(self, path: Path, events_path: Path) -> ProviderResult:
        session_id = model = None
        usage = {}
        cost = None
        completed = 0
        for event in events(events_path):
            kind = event["type"]
            if kind in {"error", "turn.failed"}:
                detail = redact(str(event.get("error", event.get("message", "Codex task failed"))))
                if "permission" in detail.lower() or "approval" in detail.lower():
                    raise UserError(detail, "permission_required")
                raise UserError(detail, "provider_failed")
            if kind == "thread.started":
                session_id = event.get("thread_id")
            if isinstance(event.get("model"), str):
                model = event["model"]
            if kind == "turn.completed":
                completed += 1
                if "usage" in event:
                    if not isinstance(event["usage"], dict):
                        raise UserError("Invalid Codex usage", "provider_output_invalid")
                    usage = event["usage"]
                cost = reported_cost(event.get("cost_usd"))
        if completed != 1 or (session_id is not None and not isinstance(session_id, str)):
            raise UserError("Codex stream requires one completed turn and a valid session ID", "provider_output_invalid")
        return ProviderResult(final_json(path), session_id=session_id, model=model, usage=usage, cost_usd=cost)
