"""Read-only CLI probes, plus disposable Git and tmux functionality checks.

Provider help describes capabilities; it does not attest that Claude's plan
mode prevents writes. Paid smoke tests are integrated with adapters separately.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import CapabilityReport, UserError, redact

PROBE_TIMEOUT = 10


def _run(argv: list[str], *, env=None) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False,
                                text=True, errors="replace", timeout=PROBE_TIMEOUT, env=env)
        return result.returncode, redact((result.stdout + "\n" + result.stderr).strip())[:16000]
    except subprocess.TimeoutExpired:
        return 1, f"Probe timed out after {PROBE_TIMEOUT} seconds"
    except OSError as exc:
        return 1, redact(str(exc))


def _check(name, status, detail, remedy=""):
    return {"name": name, "status": status, "detail": detail, "remedy": remedy}


def _infrastructure(tool: str, minimum: tuple[int, int]) -> dict:
    remedy = f"Install a working {tool} >= {minimum[0]}.{minimum[1]} and put it on PATH."
    binary = shutil.which(tool)
    if not binary:
        return _check(tool, "error", f"{tool} was not found on PATH", remedy)
    code, version = _run([binary, "--version" if tool == "git" else "-V"])
    match = re.search(r"\b(\d+)\.(\d+)(?:\.\d+)?", version)
    if code or not match:
        return _check(tool, "error", f"Version check failed: {version}", remedy)
    if tuple(map(int, match.groups())) < minimum:
        return _check(tool, "error", f"Unsupported version: {version}", remedy)

    try:
        with TemporaryDirectory(prefix="agentcrew-doctor-") as temporary:
            if tool == "git":
                # Inherited Git variables can redirect even an explicit temporary cwd.
                env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_CONFIG_SYSTEM=os.devnull, GIT_TEMPLATE_DIR="")
                code, detail = _run([binary, "-c", f"core.hooksPath={os.devnull}", "init",
                                     "--quiet", "--initial-branch=main", temporary], env=env)
                if not code:
                    code, detail = _run([binary, "-C", temporary, "rev-parse", "--is-inside-work-tree"],
                                        env=env)
                    if not code and detail.strip() != "true":
                        code = 1
            else:
                # Never address the user's default server or load ~/.tmux.conf.
                env = {key: value for key, value in os.environ.items() if key != "TMUX"}
                argv = [binary, "-S", str(Path(temporary) / "socket"), "-f", os.devnull]
                try:
                    code, detail = _run(argv + ["new-session", "-d", "-s", "doctor", "-c", temporary,
                                               sys.executable, "-c", "import time; time.sleep(30)"], env=env)
                    if not code:
                        code, detail = _run(argv + ["has-session", "-t", "doctor"], env=env)
                finally:
                    cleanup_code, cleanup_detail = _run(argv + ["kill-server"], env=env)
                if not code and cleanup_code:
                    code, detail = cleanup_code, f"Disposable server cleanup failed: {cleanup_detail}"
        if code:
            return _check(tool, "error", f"{version}; functional check failed: {detail}", remedy)
        return _check(tool, "ok", f"{version}; disposable functional check passed")
    except OSError as exc:
        return _check(tool, "error", f"Disposable functional check failed: {redact(str(exc))}",
                      "Check temporary-directory permissions and free disk space.")


def _has_flag(help_text: str, flag: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text) is not None


def _has_command(help_text: str, command: str) -> bool:
    return re.search(r"(?m)^\s*" + re.escape(command) + r"(?:\s|$)", help_text) is not None


def _provider(provider: str) -> tuple[dict, list[dict]]:
    binary = shutil.which(provider)
    capability = CapabilityReport(provider=provider, available=bool(binary))
    flags = (["--json", "--output-schema", "--output-last-message", "-C", "--sandbox"]
             if provider == "codex" else ["--print", "--output-format", "--json-schema", "--permission-mode"])
    report = asdict(capability)
    report.update(supported_flags=[], missing_flags=flags[:], permission_modes=[])
    if not binary:
        report.update(detail=f"{provider} was not found on PATH",
                      remedy=f"Install {provider} and authenticate it, or select another installed provider.")
        return report, [
            _check(f"{provider}.version", "warning", report["detail"], report["remedy"]),
            _check(f"{provider}.capabilities", "skipped", "CLI unavailable", report["remedy"]),
            _check(f"{provider}.auth", "skipped", "CLI unavailable", report["remedy"]),
        ]

    code, version = _run([binary, "--version"])
    report["version"] = version[:512] if not code else "unknown"
    checks = [_check(f"{provider}.version", "warning" if code else "ok", version,
                     f"Repair the {provider} installation." if code else "")]
    help_argv = [binary, "exec", "--help"] if provider == "codex" else [binary, "--help"]
    help_code, help_text = _run(help_argv)
    report["supported_flags"] = [flag for flag in flags if not help_code and _has_flag(help_text, flag)]
    if (provider == "claude" and not help_code and _has_flag(help_text, "-p") and
            "--print" not in report["supported_flags"]):
        report["supported_flags"].append("--print")
    report["missing_flags"] = [flag for flag in flags if flag not in report["supported_flags"]]
    modes = (["read-only", "workspace-write"] if provider == "codex" else ["plan", "acceptEdits"])
    report["permission_modes"] = [mode for mode in modes if not help_code and
                                  re.search(r"\b" + re.escape(mode) + r"\b", help_text)]
    if provider == "codex":
        report["read_only"] = "--sandbox" in report["supported_flags"] and "read-only" in report["permission_modes"]
        report["workspace_write"] = ("--sandbox" in report["supported_flags"] and
                                     "workspace-write" in report["permission_modes"])
        report["resume"] = not help_code and _has_command(help_text, "resume")
        missing_modes = [mode for mode in modes if mode not in report["permission_modes"]]
        ready = not help_code and not report["missing_flags"] and not missing_modes
        detail = ("Required exec flags and sandbox modes advertised; no model was invoked" if ready else
                  "Missing capabilities: " + ", ".join(report["missing_flags"] + missing_modes))
        if help_code:
            detail = f"Help probe failed: {help_text}"
        remedy = "" if ready else "Update Codex to a CLI version supporting the required exec flags and sandbox modes."
        checks.append(_check(f"{provider}.capabilities", "ok" if ready else "warning", detail, remedy))
        auth_help_code, auth_help = _run([binary, "login", "--help"])
        auth_available = not auth_help_code and _has_command(auth_help, "status")
        auth_argv = [binary, "login", "status"]
    else:
        report["resume"] = not help_code and _has_flag(help_text, "--resume")
        detail = "Claude read-only mode requires a version-bound smoke-test attestation; help alone is insufficient"
        if help_code:
            detail = f"Help probe failed: {help_text}"
        elif report["missing_flags"]:
            detail += "; missing flags: " + ", ".join(report["missing_flags"])
        elif "stream-json" not in help_text or "plan" not in report["permission_modes"]:
            detail += "; stream-json or plan mode is not advertised"
        remedy = "Update Claude if required; run the explicit, consented smoke-test to verify read-only mode."
        checks.append(_check(f"{provider}.capabilities", "warning", detail, remedy))
        auth_available = not help_code and _has_command(help_text, "auth")
        if auth_available:
            auth_help_code, auth_help = _run([binary, "auth", "--help"])
            auth_available = not auth_help_code and _has_command(auth_help, "status")
        auth_argv = [binary, "auth", "status"]

    if auth_available:
        auth_code, auth_detail = _run(auth_argv)
        if provider == "codex":
            authenticated = not auth_code and bool(re.search(r"(?i)^logged in\b", auth_detail))
        else:
            try:
                authenticated = not auth_code and json.loads(auth_detail).get("loggedIn") is True
            except (ValueError, AttributeError):
                authenticated = False
        report["authenticated"] = authenticated
        checks.append(_check(f"{provider}.auth", "ok" if authenticated else "warning", auth_detail,
                             "" if authenticated else f"Run {provider} {'login' if provider == 'codex' else 'auth login'} "
                             "yourself, then repeat doctor; no fallback provider is selected."))
    else:
        checks.append(_check(f"{provider}.auth", "warning", "No supported auth-status command advertised",
                             f"Update {provider} to a version providing a non-model auth-status command."))
    warnings = [check for check in checks if check["status"] == "warning"]
    report["detail"] = "; ".join(check["detail"] for check in warnings) or "CLI probes passed; no model was invoked"
    report["remedy"] = " ".join(dict.fromkeys(check["remedy"] for check in warnings))
    return report, checks


def doctor(*, smoke: bool = False, providers=("codex", "claude")) -> dict:
    """Return JSON-ready checks; ``ok`` concerns infrastructure, not provider readiness.

    Provider failures are warnings so an unused provider never blocks another.
    No provider command in this probe can submit a model prompt. Stage 1 rejects
    ``smoke=True`` until consented adapter smoke tests are implemented.
    """
    if smoke:
        raise UserError("Paid smoke tests are unavailable until provider adapter integration.", "smoke_unavailable")
    if isinstance(providers, str) or any(provider not in {"codex", "claude"} for provider in providers):
        raise UserError("Providers must be a sequence containing only codex and claude.")
    python_ready = sys.version_info[:2] >= (3, 12)
    checks = [_check("python", "ok" if python_ready else "error", ".".join(map(str, sys.version_info[:3])),
                     "" if python_ready else "Install Python >= 3.12 and recreate the environment.")]
    checks.extend([_infrastructure("git", (2, 38)), _infrastructure("tmux", (3, 3))])
    provider_reports = {}
    for provider in dict.fromkeys(providers):
        provider_reports[provider], provider_checks = _provider(provider)
        checks.extend(provider_checks)
    return {"ok": not any(check["status"] == "error" for check in checks),
            "checks": checks, "providers": provider_reports}
