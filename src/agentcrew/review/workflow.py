"""Review and the common candidate verification/evaluation phase machine."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from ..git_snapshot import create_worktree, diff, git, preflight
from ..models import UserError, canonical, digest, now, uid
from ..queue import Queue
from ..supervisor import ensure_supervisor
from .prompts import review_prompt
from .schema import REVIEW_SCHEMA


def new_run(db, snapshot: dict, workflow: str, config: dict, *, parent_run_id=None, spec=None) -> str:
    rid = db.create_scope("run", snapshot["repo_root"])
    state = db.root / "runs" / rid
    state.mkdir(parents=True, mode=0o700)
    saved = dict(config)
    saved["_meta"] = {"command": config.get("_command", f"agentcrew {workflow}"), "started_at": now()}
    spec_hash = None
    if spec is not None:
        spec_hash = digest(spec)
        path = state / "spec.md"
        path.write_text(spec, encoding="utf-8")
        os.chmod(path, 0o600)
        saved["_spec_path"] = str(path)
    with db.transaction():
        db.execute(
            "INSERT INTO runs(id,workflow,parent_run_id,source_head_sha,base_sha,spec_sha256,phase,config_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                rid,
                workflow,
                parent_run_id,
                snapshot["source_head_sha"],
                snapshot["base_sha"],
                spec_hash,
                "verify" if workflow == "review" else "plan",
                canonical(saved),
            ),
        )
        db.event(
            rid,
            "run.preflight",
            {
                "source_head_sha": snapshot["source_head_sha"],
                "base_sha": snapshot["base_sha"],
                "files": snapshot["changed_files"],
                "special_files": snapshot["special_files"],
                "spec_sha256": spec_hash,
            },
        )
    return rid


def seat_worktree(db, run: dict, role: str, sha: str, *, candidate_id=None, branch=None) -> tuple[str, Path]:
    existing = db.row("SELECT * FROM seats WHERE scope_id=? AND role=?", (run["id"], role))
    provider = run["config"]["roles"].get(role, {}).get("provider", run["config"]["default_provider"])
    seat = existing["id"] if existing else db.add_seat(run["id"], role, provider, str(db.root))
    path = create_worktree(db, run["id"], sha, role, candidate_id=candidate_id, seat_id=seat, branch=branch)
    with db.transaction():
        db.execute("UPDATE seats SET worktree_path=? WHERE id=?", (str(path), seat))
        db.event(
            run["id"], "seat.snapshot", {"seat_id": seat, "candidate_id": candidate_id, "path": str(path)}
        )
    return seat, path


def task_for(
    db,
    run: dict,
    role: str,
    kind: str,
    path: Path,
    prompt: str,
    schema: dict,
    *,
    candidate=None,
    baseline_sha=None,
    dependencies=(),
) -> str:
    config = run["config"]["roles"].get(role, {})
    timeout = config.get(
        "timeout_seconds",
        run["config"]["max_review_seconds"]
        if run["workflow"] == "review"
        else run["config"]["max_develop_seconds"],
    )
    return Queue(db).create(
        run["id"],
        role + "@" + run["id"],
        kind,
        {
            "cwd": str(path),
            "prompt": prompt,
            "schema": schema,
            "access": config.get("access", "read-only"),
            "candidate_sha": candidate["commit_sha"] if candidate else None,
            "baseline_sha": baseline_sha,
            "timeout_seconds": timeout,
        },
        candidate_id=candidate["id"] if candidate else None,
        dependencies=dependencies,
    )


def start_review(db, repo: Path, base: str, config: dict) -> str:
    snap = preflight(repo, "review", base)
    rid = new_run(db, snap, "review", config)
    db.add_candidate(rid, snap["source_head_sha"], snap["diff_sha256"], None)
    prepare_verification(db, db.run(rid))
    db.set_state("scopes", rid, "running")
    ensure_supervisor(db)
    return rid


def prepare_verification(db, run: dict):
    candidate = db.row("SELECT * FROM candidates WHERE id=?", (run["current_candidate_id"],))
    if not candidate:
        raise UserError("No immutable candidate for verification")
    root = Path(run["repo_root"])
    patch = diff(root, run["base_sha"], candidate["commit_sha"])
    if digest(patch) != candidate["diff_sha256"]:
        raise UserError("Candidate diff hash changed", "isolation_violation")
    files = [
        os.fsdecode(f)
        for f in git(root, "diff", "--name-only", "-z", run["base_sha"], candidate["commit_sha"], "--").split(
            b"\0"
        )
        if f
    ]
    spec = Path(run["config"]["_spec_path"]).read_text() if run["config"].get("_spec_path") else ""
    roles = ["correctness", "tests"] + (["checks"] if run["config"]["checks"] else [])
    for role in roles:
        if db.row(
            "SELECT t.id FROM tasks t JOIN seats s ON s.id=t.owner_seat "
            "WHERE t.scope_id=? AND t.candidate_id=? AND t.kind=? AND s.role=? LIMIT 1",
            (run["id"], candidate["id"], role, role),
        ):
            continue
        existing = db.row(
            "SELECT w.path FROM worktrees w JOIN seats s ON s.id=w.seat_id "
            "WHERE w.scope_id=? AND w.candidate_id=? AND s.role=? AND w.state='ready' "
            "ORDER BY w.created_at DESC,w.rowid DESC LIMIT 1", (run["id"], candidate["id"], role)
        )
        if existing:
            path = Path(existing["path"])
        else:
            _, path = seat_worktree(db, run, role, candidate["commit_sha"], candidate_id=candidate["id"])
        if role == "checks":
            Queue(db).create(
                run["id"], "checks@" + run["id"], "checks", {"cwd": str(path)}, candidate_id=candidate["id"]
            )
        else:
            task_for(
                db, run, role, role, path,
                review_prompt(role, run, candidate, files, patch.decode("utf-8", "replace"), spec),
                REVIEW_SCHEMA, candidate=candidate,
            )
    with db.transaction():
        db.execute("UPDATE runs SET phase='evaluate' WHERE id=?", (run["id"],))
        db.event(run["id"], "run.phase", {"phase": "evaluate", "candidate_id": candidate["id"]})


def persist_findings(db, run: dict, tasks: list[dict]) -> list[dict]:
    from .aggregate import aggregate

    raw = []
    for task in tasks:
        if task["kind"] not in {"correctness", "tests"} or task["state"] != "done":
            continue
        result = json.loads(task["result_json"])
        for finding in result["data"]["findings"]:
            raw.append(
                {
                    **finding,
                    "origin_seat": task["owner_seat"],
                    "source": task["owner_seat"],
                    "model": result.get("metadata", {}).get("model"),
                    "status": "reported",
                    "candidate_id": task["candidate_id"],
                }
            )
    combined = aggregate(raw)
    with db.transaction():
        existing = {
            aggregate([row])[0]["fingerprint"]: row
            for row in db.rows("SELECT * FROM findings WHERE candidate_id=?", (run["current_candidate_id"],))
        }
        for finding in combined:
            previous = existing.get(finding["fingerprint"])
            if previous:
                audited = db.row(
                    "SELECT id FROM decisions WHERE scope_id=? AND candidate_id=? AND kind='finding' "
                    "AND target=? LIMIT 1", (run["id"], run["current_candidate_id"], previous["id"])
                )
                status = previous["status"] if audited else finding["status"]
                db.execute(
                    "UPDATE findings SET severity=?,status=?,fingerprint=? WHERE id=?",
                    (min(previous["severity"], finding["severity"]), status, finding["fingerprint"], previous["id"]),
                )
                continue
            fid = uid()
            db.execute(
                "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    run["id"],
                    run["current_candidate_id"],
                    finding["origin_seat"],
                    finding["status"],
                    finding["severity"],
                    finding["file"],
                    finding["line"],
                    finding["title"],
                    finding["detail"],
                    finding["evidence"],
                    finding["reproduction"],
                    finding["suggestion"],
                    finding["confidence"],
                    finding["fingerprint"],
                ),
            )
            db.event(
                run["id"],
                "finding.recorded",
                {
                    "finding_id": fid,
                    "candidate_id": run["current_candidate_id"],
                    "sources": finding.get("sources", []),
                    "possible_duplicate": finding.get("possible_duplicate", []),
                },
            )
    return db.rows("SELECT * FROM findings WHERE candidate_id=?", (run["current_candidate_id"],))


def complete_run(db, run_id: str, outcome: str):
    with db.transaction():
        if db.scope(run_id)["state"] != "running":
            return
        db.execute("UPDATE runs SET outcome=?,phase='report' WHERE id=?", (outcome, run_id))
        db.set_state("scopes", run_id, "completed", payload={"outcome": outcome})
        db.event(run_id, "run.report", {"outcome": outcome})


def advance(db, run_id: str):
    run = db.run(run_id)
    if run["state"] != "running":
        return
    limit = (
        run["config"]["max_review_seconds"]
        if run["workflow"] == "review"
        else run["config"]["max_develop_seconds"]
    )
    elapsed = (datetime.now(UTC) - datetime.fromisoformat(run["created_at"])).total_seconds()
    if elapsed > limit:
        from ..supervisor import control

        control(db, run_id, "cancel")
        db.event(run_id, "run.limit_reached", {"limit_seconds": limit})
        return
    if run["phase"] in {"plan", "implement", "revise"}:
        from ..develop.workflow import advance_develop

        advance_develop(db, run)
        return
    if run["phase"] == "verify":
        prepare_verification(db, run)
        return
    if run["phase"] != "evaluate":
        return
    candidate_id = run["current_candidate_id"]
    tasks = db.rows(
        "SELECT * FROM tasks WHERE scope_id=? AND candidate_id=? AND kind IN ('correctness','tests','checks') ORDER BY created_at",
        (run_id, candidate_id),
    )
    if not tasks or any(t["state"] in {"pending", "claimed"} for t in tasks):
        return
    findings = persist_findings(db, run, tasks)
    from ..develop.requirements import development_outcome, evaluate_requirements
    from .checks import current_check_results

    checks = current_check_results(db, run_id, candidate_id)
    check_results = [json.loads(c["result_json"]) if c["result_json"] else c for c in checks]
    required = {
        c["name"]: next((r["state"] for r in checks if r["name"] == c["name"]), "unknown")
        for c in run["config"]["checks"]
    }
    reviewers = {
        role: "valid" if any(t["kind"] == role and t["state"] == "done" for t in tasks) else "failed"
        for role in ("correctness", "tests")
    }
    unresolved = [f for f in findings if f["status"] not in {"dismissed", "fixed"}]
    isolation = not db.row(
        "SELECT seq FROM events WHERE scope_id=? AND kind='isolation_violation' LIMIT 1", (run_id,)
    )
    if run["workflow"] == "review":
        outcome = (
            "partial"
            if not required
            or any(v != "valid" for v in reviewers.values())
            or any(v not in {"passed", "failed"} for v in required.values())
            else "rejected"
            if unresolved or any(v == "failed" for v in required.values())
            else "accepted"
        )
        if not isolation:
            outcome = "rejected"
        complete_run(db, run_id, outcome)
        return
    requirements = evaluate_requirements(db, run_id, candidate_id, check_results)
    outcome = development_outcome(
        required_checks=required,
        requirements={r["label"]: r["status"] for r in requirements},
        reviewers=reviewers,
        unresolved_findings=len(unresolved),
        isolation_ok=bool(isolation),
    )
    candidate = db.row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    empty = not diff(Path(run["repo_root"]), run["base_sha"], candidate["commit_sha"])
    if empty:
        outcome = "rejected"
    if outcome == "accepted":
        if run["workflow"] == "fix":
            target = run["config"].get("_finding_id")
            if target:
                db.set_state(
                    "findings", target, "fixed", payload={"fix_run_id": run_id, "candidate_id": candidate_id}
                )
        complete_run(db, run_id, outcome)
    elif (
        run["workflow"] != "fix"
        and candidate["revision"] <= run["config"]["max_revisions"]
        and isolation
        and outcome == "rejected"
    ):
        from ..develop.workflow import revise

        revise(db, run, {"checks": check_results, "requirements": requirements, "findings": unresolved})
    elif outcome == "partial" and any(r["status"] == "unknown" for r in requirements):
        with db.transaction():
            db.execute("UPDATE runs SET outcome='partial' WHERE id=?", (run_id,))
            db.set_state(
                "scopes",
                run_id,
                "needs_attention",
                payload={
                    "reason": "Requirement evidence unknown; inspect plan/report and record candidate-bound confirmation"
                },
            )
    else:
        complete_run(db, run_id, outcome)
