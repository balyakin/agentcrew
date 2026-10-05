"""Original specification → frozen plan → sole writer → immutable candidate."""

import json
import os
from pathlib import Path

from ..git_snapshot import _owned, assert_snapshot, freeze, git, preflight
from ..models import MAX_JSON, UserError, canonical, digest, uid
from ..review.workflow import new_run, prepare_verification, seat_worktree, task_for
from ..supervisor import ensure_supervisor
from .prompts import implementation_prompt, plan_prompt
from .schema import IMPLEMENTATION_SCHEMA, PLAN_SCHEMA, validate_plan


def read_spec(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_JSON:
            raise UserError("Specification must be a UTF-8 file <= 1 MiB")
        spec = path.read_text(encoding="utf-8")
        if not spec.strip() or "\x00" in spec:
            raise UserError("Specification cannot be empty or contain NUL")
        return spec
    except (OSError, UnicodeError) as exc:
        raise UserError(f"Cannot read specification: {exc}") from exc


def start_develop(
    db, repo: Path, spec_path: Path, config: dict, *, workflow="develop", snapshot=None, parent_run_id=None
) -> str:
    spec = read_spec(spec_path)
    if not config["checks"]:
        raise UserError(
            "Develop/new/fix require configured checks and verifiable criteria", "checks_required"
        )
    snap = snapshot or preflight(repo, "develop")
    rid = new_run(db, snap, workflow, config, parent_run_id=parent_run_id, spec=spec)
    run = db.run(rid)
    _, path = seat_worktree(db, run, "planner", snap["base_sha"])
    task_for(
        db,
        run,
        "planner",
        "plan",
        path,
        plan_prompt(spec, config),
        PLAN_SCHEMA,
        baseline_sha=snap["base_sha"],
    )
    db.set_state("scopes", rid, "running")
    ensure_supervisor(db)
    return rid


def set_phase(db, run_id: str, phase: str):
    with db.transaction():
        db.execute("UPDATE runs SET phase=? WHERE id=?", (phase, run_id))
        db.event(run_id, "run.phase", {"phase": phase})


def _schedule_writer(db, run: dict, kind: str, branch: str, sha: str, prompt: str, *, candidate=None):
    """Reconcile an interrupted writer dispatch before issuing any new work."""
    seat = "implementer@" + run["id"]
    candidate_id = candidate["id"] if candidate else None
    phase = "revise" if kind == "revise" else "implement"
    query = "SELECT id FROM tasks WHERE scope_id=? AND owner_seat=? AND kind=? AND candidate_id IS ? LIMIT 1"
    args = (run["id"], seat, kind, candidate_id)
    if db.row(query, args):
        set_phase(db, run["id"], phase)
        return
    worktree = db.row("SELECT * FROM worktrees WHERE scope_id=? AND branch_name=?", (run["id"], branch))
    if worktree:
        if (worktree["state"] != "ready" or worktree["seat_id"] != seat
                or worktree["candidate_id"] != candidate_id):
            raise UserError("Writer worktree creation was interrupted; inspect recover --plan", "worktree_ownership")
        path, _ = _owned(db, worktree)
        if (not assert_snapshot(path, sha)
                or git(path, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip() != branch):
            raise UserError("Unassigned writer worktree changed; inspect it before continuing", "isolation_violation")
    else:
        _, path = seat_worktree(db, run, "implementer", sha, candidate_id=candidate_id, branch=branch)
    # Worktree creation/validation precedes this transaction; task and phase commit together.
    with db.transaction():
        if not db.row(query, args):
            task_for(db, run, "implementer", kind, path, prompt, IMPLEMENTATION_SCHEMA,
                     candidate=candidate, baseline_sha=sha)
        db.execute("UPDATE seats SET worktree_path=? WHERE id=?", (str(path), seat))
        set_phase(db, run["id"], phase)


def implement(db, run: dict):
    plan = json.loads(run["plan_json"])
    spec = Path(run["config"]["_spec_path"]).read_text()
    answers = db.rows(
        "SELECT body FROM decisions WHERE scope_id=? AND kind='answer' ORDER BY created_at", (run["id"],)
    )
    if answers:
        spec += "\nRecorded operator answers:\n" + "\n".join(a["body"] for a in answers)
    revision = 1
    finding = run["config"].get("_finding_id", "finding")[:8]
    branch = (
        f"agentcrew/fix/{run['id'][:8]}/{finding}-1"
        if run["workflow"] == "fix"
        else f"agentcrew/develop/{run['id'][:8]}/r{revision}"
    )
    kind = "fix" if run["workflow"] == "fix" else "implement"
    _schedule_writer(db, run, kind, branch, run["base_sha"], implementation_prompt(spec, plan))


def revise(db, run: dict, feedback: dict):
    candidate = db.row("SELECT * FROM candidates WHERE id=?", (run["current_candidate_id"],))
    branch = f"agentcrew/develop/{run['id'][:8]}/r{candidate['revision'] + 1}"
    plan = json.loads(run["plan_json"])
    spec = Path(run["config"]["_spec_path"]).read_text()
    _schedule_writer(db, run, "revise", branch, candidate["commit_sha"],
                     implementation_prompt(spec, plan, revision=True, feedback=feedback), candidate=candidate)


def advance_develop(db, run: dict):
    kind = (
        "plan"
        if run["phase"] == "plan"
        else ("revise" if run["phase"] == "revise" else "fix" if run["workflow"] == "fix" else "implement")
    )
    task = db.row(
        "SELECT * FROM tasks WHERE scope_id=? AND kind=? ORDER BY created_at DESC LIMIT 1", (run["id"], kind)
    )
    if not task or task["state"] in {"pending", "claimed"}:
        return
    if task["state"] != "done":
        db.set_state(
            "scopes",
            run["id"],
            "needs_attention",
            payload={"reason": "Mandatory task did not complete", "task_id": task["id"]},
        )
        return
    data = json.loads(task["result_json"])["data"]
    if kind == "plan":
        plan = validate_plan(data, run["config"]["checks"])
        if not plan["requirements"] or not plan["work_items"]:
            db.set_state(
                "scopes",
                run["id"],
                "needs_attention",
                payload={"reason": "Planner returned no executable requirements/work"},
            )
            return
        with db.transaction():
            if run["plan_json"] is None:
                db.execute("UPDATE runs SET plan_json=? WHERE id=?", (canonical(plan), run["id"]))
                for req in plan["requirements"]:
                    db.execute(
                        "INSERT INTO requirements VALUES(?,?,?,?,?)",
                        (uid(), run["id"], req["id"], req["description"], canonical(req["verification"])),
                    )
                db.event(
                    run["id"],
                    "plan.frozen",
                    {"sha256": digest(canonical(plan)), "requirements": len(plan["requirements"])},
                )
            if plan["questions"] and not db.row(
                "SELECT id FROM decisions WHERE scope_id=? AND kind='answer'", (run["id"],)
            ):
                db.set_state(
                    "scopes",
                    run["id"],
                    "needs_attention",
                    payload={
                        "questions": plan["questions"],
                        "remedy": f"answer {run['id']} --body-file answer.md",
                    },
                )
                return
        implement(db, db.run(run["id"]))
        return
    body = json.loads(task["body_json"])
    path = Path(body["cwd"])
    baseline = body.get("candidate_sha") or body.get("baseline_sha")
    # Include staged and untracked files, without trusting the writer's list.
    tracked = {
        os.fsdecode(f) for f in git(path, "diff", "--name-only", "-z", baseline, "--").split(b"\0") if f
    }
    untracked = {
        os.fsdecode(f)
        for f in git(path, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if f
    }
    if set(data["changed_files"]) != tracked | untracked:
        db.set_state(
            "scopes",
            run["id"],
            "needs_attention",
            payload={
                "reason": "Writer changed_files disagrees with Git",
                "actual": sorted(tracked | untracked),
            },
        )
        return
    freeze(path, db, run["id"], parent_id=run["current_candidate_id"])
    prepare_verification(db, db.run(run["id"]))


def answer(db, run_id: str, body: str) -> dict:
    if not body.strip() or len(body.encode()) > MAX_JSON:
        raise UserError("Answer must be nonempty UTF-8 text <= 1 MiB")
    run = db.run(run_id)
    key = digest(body)
    old = db.row("SELECT id FROM decisions WHERE scope_id=? AND kind='answer' AND target=?", (run_id, key))
    if old:
        return {"run_id": run_id, "answer_id": old["id"], "already_saved": True}
    if run["state"] != "needs_attention" or run["phase"] != "plan" or not run["plan_json"]:
        raise UserError(
            "No pending planner question; inspect report/recover for failed tasks or unknown evidence"
        )
    plan = json.loads(run["plan_json"])
    if not plan["questions"]:
        raise UserError("No pending question")
    identifier = uid()
    from ..models import now

    with db.transaction():
        db.execute(
            "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
            (identifier, run_id, None, "answer", key, body, now()),
        )
        db.event(run_id, "question.answered", {"answer_id": identifier, "sha256": key})
        db.set_state("scopes", run_id, "running")
    ensure_supervisor(db)
    return {"run_id": run_id, "answer_id": identifier, "already_saved": False}
