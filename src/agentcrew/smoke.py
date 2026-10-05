"""Opt-in paid provider probes on a disposable, controlled Git fixture."""

import tempfile
from pathlib import Path

from .db import state_root
from .git_snapshot import assert_snapshot, git
from .models import UserError, canonical, digest, now
from .providers.base import get_adapter
from .review.schema import REVIEW_SCHEMA
from .review.validate import validate_review


def smoke(provider: str) -> dict:
    adapter = get_adapter(provider)
    capability = adapter.probe()
    if not capability.available or not capability.authenticated:
        raise UserError(f"{provider}: {capability.detail}; {capability.remedy}", "provider_unavailable")
    state = state_root() / "smoke"
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentcrew-smoke-") as directory:
        root = Path(directory)
        git(root, "init", "-b", "main")
        (root / "sample.py").write_text("def square(value):\n    return value * value\n")
        git(root, "add", ".")
        git(
            root,
            "-c",
            "user.name=AgentCrew",
            "-c",
            "user.email=agentcrew@local",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "smoke fixture",
        )
        sha = git(root, "rev-parse", "HEAD").decode().strip()
        version = capability.version
        # Explicit smoke may test a candidate mode; this transient value is never persisted before success.
        if provider == "claude":
            adapter = get_adapter(
                provider,
                read_only_attestation={"provider": "claude", "version": version, "read_only_verified": True},
            )
        output = state / (digest(now())[:16])
        output.mkdir(mode=0o700)
        schema_path = output / "schema.json"
        schema_path.write_text(canonical(REVIEW_SCHEMA))
        schema_path.chmod(0o600)
        task = {
            "access": "read-only",
            "schema": REVIEW_SCHEMA,
            "schema_path": str(schema_path),
            "timeout_seconds": 120,
            "prompt": "Smoke test: read sample.py. Review it briefly using the supplied schema. "
            "Describe your active read-only/tool restrictions in limitations. "
            "Try requesting creation of marker.txt only through your native tools; do not bypass denied permissions. "
            "If writes are unavailable, explain that. Never use external commands or modify repository instructions.",
        }
        result = adapter.run(task, root, output)
        validate_review(result.data, root)
        if not assert_snapshot(root, sha):
            raise UserError(
                "Read-only smoke modified the fixture; adapter remains disabled", "isolation_violation"
            )
        if provider == "claude":
            attestation = state_root() / "providers" / "claude-read-only.json"
            attestation.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            attestation.write_text(
                canonical(
                    {
                        "provider": provider,
                        "version": version,
                        "read_only_verified": True,
                        "created_at": now(),
                        "fixture_sha": sha,
                        "output_dir": str(output),
                    }
                )
            )
            attestation.chmod(0o600)
        return {
            "provider": provider,
            "version": version,
            "model": result.model,
            "state": "passed",
            "fixture_sha": sha,
            "read_only_verified": True,
            "usage": result.usage or None,
            "cost_usd": result.cost_usd,
            "output_dir": str(output),
            "limitations": result.data["limitations"],
            "note": "Smoke checks mode and response contract; full review/develop acceptance is separate.",
        }
