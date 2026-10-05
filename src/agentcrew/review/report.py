"""Partial failures, exact candidate identities, and redacted user-facing reports."""

import json
import math
from pathlib import Path

from agentcrew.i18n import t
from agentcrew.models import MAX_LOG, UserError, redact_data
from agentcrew.review.aggregate import aggregate
from agentcrew.review.checks import current_check_results


def build_report(db, run_id: str, include_logs=False) -> dict:
    run = db.run(run_id)
    candidate = db.row("SELECT * FROM candidates WHERE id=?", (run["current_candidate_id"],))
    candidate_id = candidate["id"] if candidate else None
    worktrees = db.rows("SELECT * FROM worktrees WHERE scope_id=? ORDER BY created_at", (run_id,))
    current_worktree = next(
        (
            row
            for row in worktrees
            if row["candidate_id"] == candidate_id and row["state"] == "ready" and row["branch_name"]
        ),
        None,
    )
    if current_worktree is None:
        current_worktree = next(
            (row for row in worktrees if row["candidate_id"] == candidate_id and row["state"] == "ready"),
            None,
        )
    agents = []
    changed_files = []
    for seat in db.rows("SELECT * FROM seats WHERE scope_id=? ORDER BY role", (run_id,)):
        if seat["role"] in {"correctness", "tests"}:
            task = db.row(
                "SELECT * FROM tasks WHERE owner_seat=? AND candidate_id IS ? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (seat["id"], candidate_id),
            )
        else:
            task = db.row(
                "SELECT * FROM tasks WHERE owner_seat=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (seat["id"],),
            )
        payload = json.loads(task["result_json"]) if task and task["result_json"] else None
        result = payload.get("data", payload) if isinstance(payload, dict) else None
        provider_result = (
            db.row(
                "SELECT metadata_json,valid FROM provider_results WHERE task_id=? "
                "ORDER BY attempt DESC LIMIT 1",
                (task["id"],),
            )
            if task
            else None
        )
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        if provider_result:
            metadata = json.loads(provider_result["metadata_json"])
        valid = bool(
            task
            and task["state"] == "done"
            and result is not None
            and (provider_result is None or provider_result["valid"])
        )
        count = len(result["findings"]) if valid and "findings" in result else None
        if seat["role"] == "implementer" and valid:
            changed_files = result.get("changed_files", [])
        agents.append(
            {
                "role": seat["role"],
                "provider": seat["provider"],
                "model": metadata.get("model"),
                "state": task["state"] if task else seat["state"],
                "valid": valid,
                "findings_count": count,
                "kind": task["kind"] if task else None,
                "error": payload.get("detail", payload.get("error"))
                if isinstance(payload, dict) and not valid
                else None,
                "limitations": result.get("limitations", []) if result else [],
                "usage": metadata.get("usage"),
                "cost_usd": metadata.get("cost_usd"),
                "version": metadata.get("version", "unknown"),
            }
        )
    checks = current_check_results(db, run_id, candidate_id)
    for check in checks:
        metadata = json.loads(check.pop("result_json")) if check.get("result_json") else {}
        check.pop("result_json", None)
        check.update(
            {
                key: metadata.get(key)
                for key in ("error", "tests", "result_path", "result_sha256", "timed_out")
            }
        )
        check["argv"] = json.loads(check.pop("argv_json"))
    requirements = db.rows(
        "SELECT r.id,r.label,r.description,e.status,e.evidence_json FROM requirements r "
        "LEFT JOIN requirement_results e ON e.requirement_id=r.id AND e.candidate_id=? "
        "WHERE r.run_id=? ORDER BY r.label",
        (candidate_id, run_id),
    )
    for requirement in requirements:
        requirement["status"] = requirement["status"] or "unknown"
        requirement["evidence"] = json.loads(requirement.pop("evidence_json") or "{}")
    findings = db.rows(
        "SELECT f.*,s.role,s.provider FROM findings f JOIN seats s ON s.id=f.origin_seat "
        "WHERE f.run_id=? AND f.candidate_id=?",
        (run_id, candidate_id),
    )
    reviewers = {agent["role"]: agent["valid"] for agent in agents}
    requested = {check["name"] for check in run["config"].get("checks", [])}
    finished = {check["name"] for check in checks if check["state"] in {"passed", "failed"}}
    complete = (
        all(reviewers.get(role) for role in ("correctness", "tests"))
        and bool(requested)
        and requested <= finished
    )
    if candidate:
        from agentcrew.git_snapshot import git

        try:
            files = git(
                Path(run["repo_root"]),
                "diff",
                "--name-only",
                "-z",
                run["base_sha"],
                candidate["commit_sha"],
                "--",
            )
            changed_files = [file.decode("utf-8", errors="replace") for file in files.split(b"\x00") if file]
        except UserError:
            pass
    model_agents = [agent for agent in agents if agent["kind"] != "checks"]
    attempts = db.rows(
        "SELECT p.metadata_json,p.valid FROM provider_results p JOIN tasks t ON t.id=p.task_id "
        "WHERE t.scope_id=? ORDER BY t.created_at,p.attempt",
        (run_id,),
    )
    if attempts:
        model_agents = [json.loads(attempt["metadata_json"]) for attempt in attempts]
    unknown_attempt = (
        any(not attempt["valid"] for attempt in attempts)
        or bool(
            db.row(
                "SELECT seq FROM events WHERE scope_id=? AND kind='provider_output_invalid' LIMIT 1",
                (run_id,),
            )
        )
        or bool(
            db.row(
                "SELECT id FROM tasks WHERE scope_id=? AND kind<>'checks' AND state IN ('claimed','blocked','failed') LIMIT 1",
                (run_id,),
            )
        )
    )
    measured = (
        not unknown_attempt
        and bool(model_agents)
        and all(
            isinstance(agent.get("usage"), dict)
            and agent["usage"]
            and all(
                type(value) in (int, float) and math.isfinite(value) and value >= 0
                for value in agent["usage"].values()
            )
            for agent in model_agents
        )
    )
    usage = {} if measured else None
    if measured:
        for agent in model_agents:
            for key, value in agent["usage"].items():
                usage[key] = usage.get(key, 0) + value
    costs_measured = (
        not unknown_attempt
        and bool(model_agents)
        and all(
            type(agent.get("cost_usd")) in (int, float)
            and math.isfinite(agent["cost_usd"])
            and agent["cost_usd"] >= 0
            for agent in model_agents
        )
    )
    report = {
        "run_id": run_id,
        "workflow": run["workflow"],
        "state": run["state"],
        "status": "complete" if complete else "partial",
        "outcome": run["outcome"],
        "source_head_sha": run["source_head_sha"],
        "base_sha": run["base_sha"],
        "candidate_id": candidate_id,
        "candidate_sha": candidate["commit_sha"] if candidate else None,
        "branch_name": candidate["branch_name"] if candidate else None,
        "worktree_path": current_worktree["path"] if current_worktree else None,
        "revision": candidate["revision"] if candidate else None,
        "candidates": db.rows("SELECT * FROM candidates WHERE run_id=? ORDER BY revision", (run_id,)),
        "worktrees": worktrees,
        "diff_sha256": candidate["diff_sha256"] if candidate else None,
        "created_at": run["created_at"],
        "config": run["config"],
        "agents": agents,
        "checks": checks,
        "requirements": requirements,
        "findings": aggregate(findings),
        "limitations": [],
        "changed_files": changed_files,
        "command": run["config"].get("_meta", {}).get("command", "unknown"),
        "started_at": run["config"].get("_meta", {}).get("started_at", run["created_at"]),
        "usage": usage,
        "cost_usd": sum(agent["cost_usd"] for agent in model_agents) if costs_measured else None,
        "diff_reference": {
            "base_sha": run["base_sha"],
            "candidate_sha": candidate["commit_sha"] if candidate else None,
            "diff_sha256": candidate["diff_sha256"] if candidate else None,
        },
    }
    if not complete:
        report["limitations"].append("Independent review or configured checks are incomplete")
    if include_logs:
        report["logs"] = {}
        for check in checks:
            if check["log_path"]:
                path = Path(check["log_path"])
                if (
                    path.resolve().is_relative_to(db.root)
                    and path.is_file()
                    and path.stat().st_size <= MAX_LOG
                ):
                    report["logs"][check["id"]] = path.read_text(encoding="utf-8", errors="replace")
    return redact_data(report)


def markdown(report: dict, language: str = "en") -> str:
    report = redact_data(report)

    def label(key):
        return t(key, language)

    lines = [
        f"# AgentCrew {report['workflow']} — {report['run_id']}",
        "",
        f"{label('status')}: **{label(report['status'])}**; {label('outcome')}: **{label(report['outcome'])}**",
        "",
    ]
    for key in (
        "source_head_sha",
        "base_sha",
        "candidate_id",
        "candidate_sha",
        "diff_sha256",
        "branch_name",
        "worktree_path",
        "revision",
    ):
        lines.append(f"- {key}: `{report.get(key) or label('unknown')}`")
    lines.append(f"- usage: `{report.get('usage') if report.get('usage') is not None else label('unknown')}`")
    lines.append(
        f"- cost_usd: `{report.get('cost_usd') if report.get('cost_usd') is not None else label('unknown')}`"
    )
    lines.extend(
        [
            "",
            "## " + ("Агенты" if language == "ru" else "Agents"),
            "",
            f"| {label('role')} | {label('provider_model')} | {label('status')} | {label('findings')} |",
            "|---|---|---|---|",
        ]
    )
    for agent in report["agents"]:
        count = agent["findings_count"] if agent["findings_count"] is not None else label("unknown")
        lines.append(
            f"| {agent['role']} | {agent['provider']}/{agent['model'] or label('unknown')} | {label(agent['state'])} | {count} |"
        )
        if agent.get("error"):
            lines.append(f"\n{agent['role']}: {agent['error']}\n")
    lines.extend(["", "## " + label("changed_files"), ""])
    lines.extend(f"- `{file}`" for file in report.get("changed_files", []))
    lines.extend(["", "## " + label("checks"), ""])
    for check in report["checks"]:
        lines.append(
            f"- {check['name']}: {label(check['state'])} (exit {check['exit_code']}, {check['duration_ms']} ms)"
        )
        if check.get("error"):
            lines.append(f"  {check['error']}")
    lines.extend(
        [
            "",
            "## " + label("requirements"),
            "",
            f"| ID | {label('description')} | {label('status')} |",
            "|---|---|---|",
        ]
    )
    for requirement in report["requirements"]:
        lines.append(
            f"| {requirement['label']} | {requirement['description']} | {label(requirement['status'])} |"
        )
    lines.extend(["", "## " + label("findings"), ""])
    for finding in report["findings"]:
        lines.extend(
            [
                f"- **{finding['severity']}** `{finding['file']}:{finding['line']}` {finding['title']}",
                f"  {label('status')}: {label(finding['status'])}; {label('evidence')}: {finding['evidence']}",
                f"  {finding['detail']}",
                f"  {label('reproduction')}: {finding.get('reproduction') or label('unknown')}",
            ]
        )
    if not report["findings"]:
        lines.append(label("no_findings"))
    lines.extend(["", "## " + label("limitations"), ""])
    for limitation in report["limitations"]:
        lines.append(f"- {limitation}")
    for agent in report["agents"]:
        for limitation in agent["limitations"]:
            lines.append(f"- {agent['role']}: {limitation}")
    return "\n".join(lines) + "\n"
