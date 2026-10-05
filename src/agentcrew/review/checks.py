"""Configured argv checks and fresh, candidate-bound JUnit evidence."""

import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from agentcrew.config import validate_config
from agentcrew.git_snapshot import create_worktree, git
from agentcrew.models import MAX_LOG, UserError, canonical, digest, now, uid
from agentcrew.review.validate import validate_relative_path


def current_check_results(db, run_id: str, candidate_id: str | None) -> list[dict]:
    """Only IDs returned by the latest completed checks task are current evidence."""
    task = db.row(
        "SELECT * FROM tasks WHERE scope_id=? AND candidate_id=? AND kind='checks' "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1", (run_id, candidate_id)
    )
    candidate = db.row("SELECT * FROM candidates WHERE id=? AND run_id=?", (candidate_id, run_id))
    if not task or task["state"] != "done" or not candidate:
        return []
    try:
        supplied = json.loads(task["result_json"])["checks"]
        if not isinstance(supplied, list):
            return []
        results, identifiers, names = [], set(), set()
        for result in supplied:
            identifier = result["id"]
            if identifier in identifiers or result["name"] in names:
                return []
            row = db.row(
                "SELECT c.*,e.result_json FROM check_runs c JOIN check_evidence e ON e.check_id=c.id "
                "WHERE c.id=? AND c.candidate_id=?", (identifier, candidate_id)
            )
            if (not row or json.loads(row["result_json"]) != result
                    or result["check_id"] != identifier or result["name"] != row["name"]
                    or result["state"] != row["state"] or result["exit_code"] != row["exit_code"]
                    or result["candidate_id"] != candidate_id
                    or result["candidate_sha"] != candidate["commit_sha"]
                    or (row["state"] == "passed" and row["exit_code"] != 0)):
                return []
            if result["log_sha256"]:
                log = Path(result["log_path"]).resolve(strict=True)
                if (not log.is_relative_to(db.root) or log.stat().st_size > MAX_LOG
                        or digest(log.read_bytes()) != result["log_sha256"]):
                    return []
            elif row["state"] == "passed":
                return []
            if result["result_path"]:
                report = Path(result["result_path"]).resolve(strict=True)
                owned = db.row(
                    "SELECT path FROM worktrees WHERE candidate_id=? AND state='ready' "
                    "AND ? LIKE path || '/%'", (candidate_id, str(report))
                )
                if (not owned or not report.is_relative_to(Path(owned["path"]).resolve())
                        or report.stat().st_size > MAX_LOG
                        or digest(report.read_bytes()) != result["result_sha256"]):
                    return []
            identifiers.add(identifier)
            names.add(row["name"])
            results.append(row)
        return results
    except (OSError, ValueError, TypeError, KeyError):
        return []


def _report_path(root: Path, relative: str) -> Path:
    lexical = root / validate_relative_path(relative)
    target = lexical.resolve()
    if not target.is_relative_to(root.resolve()) or lexical.is_symlink():
        raise UserError("JUnit result path escapes checks worktree", "check_output_invalid")
    return target


def _test_id(case, root):
    name = case.get("name", "")
    classname = case.get("classname", "")
    file = case.get("file")
    if not name or len(name) > 4096 or len(classname) > 4096:
        raise UserError("JUnit testcase has invalid identity", "check_output_invalid")
    if file:
        if Path(file).is_absolute():
            resolved = Path(file).resolve()
            if not resolved.is_relative_to(root.resolve()):
                raise UserError("JUnit testcase file escapes checks worktree", "check_output_invalid")
            file = resolved.relative_to(root.resolve()).as_posix()
        path = _report_path(root, file)
        if classname == "test" and path.suffix in {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}:
            return name
        relative = path.relative_to(root.resolve()).as_posix()
        module = relative.removesuffix(".py").replace("/", ".")
        suffix = classname[len(module) :].lstrip(".") if classname.startswith(module) else ""
        return "::".join(part for part in (relative, suffix.replace(".", "::"), name) if part)
    parts = classname.split(".")
    for length in range(len(parts), 0, -1):
        relative = "/".join(parts[:length]) + ".py"
        path = _report_path(root, relative)
        if path.is_file():
            return "::".join([relative, *parts[length:], name])
    # Native non-pytest JUnit IDs remain literal; no fuzzy matching earns a PASS.
    return f"{classname}::{name}" if classname else name


def parse_junit(path: Path, root: Path) -> dict[str, str]:
    try:
        resolved = _report_path(root, path.relative_to(root).as_posix())
        if not resolved.is_file() or resolved.stat().st_size > MAX_LOG:
            raise UserError("Missing or oversized JUnit result", "check_output_invalid")
        data = resolved.read_bytes()
        if (
            len(data) > MAX_LOG
            or b"\x00" in data
            or b"<!DOCTYPE" in data.upper()
            or b"<!ENTITY" in data.upper()
        ):
            raise UserError("Unsafe or oversized JUnit XML", "check_output_invalid")
        tree = ET.fromstring(data.decode("utf-8-sig"))
        if tree.tag not in {"testsuite", "testsuites"}:
            raise UserError("Unsupported JUnit root", "check_output_invalid")
        tests = {}
        for case in tree.iter("testcase"):
            if len(tests) >= 100000:
                raise UserError("Too many JUnit testcases", "check_output_invalid")
            identifier = _test_id(case, root)
            status = "passed"
            if case.find("skipped") is not None:
                status = "unknown"
            elif case.find("failure") is not None or case.find("error") is not None:
                status = "failed"
            elif case.get("status", "").lower() not in {"", "run", "passed", "success"} or case.get(
                "result", ""
            ).lower() not in {"", "completed", "passed", "success"}:
                status = "unknown"
            tests[identifier] = "unknown" if identifier in tests else status
        return tests
    except (OSError, UnicodeError, ValueError, ET.ParseError) as exc:
        raise UserError(f"Invalid JUnit result: {exc}", "check_output_invalid") from exc


def _stop_group(process):
    from ..processes import stop_group

    stop_group(process)


def run_checks(
    db, run_id: str, candidate_id: str, root: Path, checks: list[dict], *, canceled=None, heartbeat=None
) -> list[dict]:
    checks = validate_config({"checks": checks})["checks"]
    candidate = db.row("SELECT * FROM candidates WHERE id=? AND run_id=?", (candidate_id, run_id))
    if not candidate or not root.is_dir():
        raise UserError("Checks require an existing candidate in this run")
    results = []
    for index, check in enumerate(checks):
        if canceled and canceled():
            break
        identifier = uid()
        logs = db.root / "scopes" / run_id / "checks"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = logs / f"{identifier}.log"
        result = {
            "id": identifier,
            "check_id": identifier,
            "name": check["name"],
            "candidate_id": candidate_id,
            "candidate_sha": candidate["commit_sha"],
            "state": "running",
            "exit_code": None,
            "tests": {},
            "log_path": str(log),
            "result_path": None,
            "result_sha256": None,
            "timed_out": False,
            "error": "",
        }
        with db.transaction():
            db.execute(
                "INSERT INTO check_runs(id,candidate_id,name,argv_json,state,created_at) VALUES(?,?,?,?,?,?)",
                (identifier, candidate_id, check["name"], canonical(check["argv"]), "running", now()),
            )
            db.event(run_id, "check.started", {"check_id": identifier, "candidate_id": candidate_id})
        started = time.monotonic()
        process = None
        try:
            worktree = create_worktree(
                db,
                run_id,
                candidate["commit_sha"],
                f"check-{index}-{identifier[:8]}",
                candidate_id=candidate_id,
            )
            report = _report_path(worktree, check["result_file"]) if "result_file" in check else None
            if report is not None:
                if report.exists():
                    if not report.is_file():
                        raise UserError("JUnit result path is not a file", "check_output_invalid")
                    report.unlink()
                report.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                process = subprocess.Popen(
                    check["argv"],
                    cwd=worktree,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                pulse = 0
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if heartbeat and elapsed >= pulse:
                        heartbeat()
                        pulse = elapsed + 1
                    if canceled and canceled():
                        result["state"], result["error"] = "canceled", "Check canceled"
                        break
                    if elapsed >= check["timeout_seconds"]:
                        result["timed_out"], result["error"] = True, "Check timed out"
                        break
                    if log.stat().st_size > MAX_LOG:
                        result["error"] = "Check output exceeds 20 MiB"
                        break
                    time.sleep(0.05)
                _stop_group(process)
                result["exit_code"] = process.returncode
                if log.stat().st_size > MAX_LOG:
                    output.truncate(MAX_LOG)
                    result["error"] = "Check output exceeds 20 MiB"
                if result["state"] != "canceled":
                    result["state"] = (
                        "passed" if process.returncode == 0 and not result["error"] else "failed"
                    )
            paths = ["."]
            if report is not None:
                paths.append(":(literal,exclude)" + report.relative_to(worktree).as_posix())
            if git(worktree, "rev-parse", "HEAD").decode().strip() != candidate["commit_sha"] or git(
                worktree, "diff", "--no-ext-diff", "--name-only", "-z", "HEAD", "--", *paths
            ):
                raise UserError("Check modified the tracked candidate snapshot", "isolation_violation")
            if check.get("result_format") == "junit" and result["state"] in {"passed", "failed"}:
                result["tests"] = parse_junit(report, worktree)
                result["result_path"] = str(report)
                result["result_sha256"] = digest(report.read_bytes())
        except (OSError, UserError) as exc:
            result["state"], result["error"] = "failed", str(exc)
        finally:
            if process is not None:
                _stop_group(process)
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["log_sha256"] = digest(log.read_bytes()) if log.exists() else None
        with db.transaction():
            db.execute(
                "UPDATE check_runs SET state=?,exit_code=?,duration_ms=?,log_path=?,log_sha256=? WHERE id=?",
                (
                    result["state"],
                    result["exit_code"],
                    result["duration_ms"],
                    result["log_path"],
                    result["log_sha256"],
                    identifier,
                ),
            )
            db.execute("INSERT INTO check_evidence VALUES(?,?)", (identifier, canonical(result)))
            db.event(
                run_id,
                "check.completed",
                {"check_id": identifier, "state": result["state"], "candidate_id": candidate_id},
            )
        results.append(result)
    return results
