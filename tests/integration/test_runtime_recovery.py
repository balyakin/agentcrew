"""Recovery preserves a writer's patch and advances durable workflow checkpoints."""

import json
import subprocess
import sys

import pytest

from agentcrew.config import validate_config
from agentcrew.db import Database
from agentcrew.develop import workflow as development
from agentcrew.develop.schema import IMPLEMENTATION_SCHEMA
from agentcrew.develop.workflow import advance_develop
from agentcrew.git_snapshot import create_worktree, freeze, preflight
from agentcrew.models import CapabilityReport, ProviderResult, UserError, canonical
from agentcrew.queue import Queue
from agentcrew.review import workflow
from agentcrew.review.checks import run_checks
from agentcrew.worker import execute


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Recovery test")
    git(source, "config", "user.email", "recovery@local")
    (source / "app.py").write_text("value = 1\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "baseline")
    return source


def implementation():
    return {"schema_version": 1, "summary": "Changed value", "changed_files": ["app.py"],
            "addressed_requirements": [], "remaining_requirements": [], "limitations": []}


def writer_task(db, project):
    snapshot = preflight(project, "develop")
    run = workflow.new_run(db, snapshot, "develop", validate_config({}), spec="Change value to 2")
    seat, path = workflow.seat_worktree(db, db.run(run), "implementer", snapshot["source_head_sha"],
                                       branch=f"agentcrew/develop/{run[:8]}/r1")
    db.execute("UPDATE runs SET phase='implement',plan_json=? WHERE id=?",
               (canonical({"requirements": []}), run))
    db.set_state("scopes", run, "running")
    workflow.task_for(db, db.run(run), "implementer", "implement", path, "Change value",
                      IMPLEMENTATION_SCHEMA, baseline_sha=snapshot["source_head_sha"])
    task = Queue(db).claim(seat, "old-writer")
    (path / "app.py").write_text("value = 2\n")
    return run, seat, path, task


@pytest.fixture
def writer_adapter(monkeypatch):
    from agentcrew.providers import base

    class Adapter:
        def resume_capability(self):
            return False

        def run(self, request, cwd, output, **kwargs):
            # A real provider is excluded: the regression concerns launch/recovery policy.
            assert (cwd / "app.py").read_text() == "value = 2\n"
            (cwd / "app.py").write_text("value = 3\n")
            return ProviderResult(implementation())

        def probe(self):
            return CapabilityReport("codex", True, version="fixture")

    monkeypatch.setattr(base, "get_adapter", lambda *_: Adapter())


def test_inspected_retry_continues_partial_writer_without_changing_source(tmp_path, project, writer_adapter):
    db = Database(tmp_path / "state")
    run, seat, path, task = writer_task(db, project)
    queue = Queue(db)
    queue.block(task["id"], "Worker died", "Inspect and retry")
    with pytest.raises(UserError):
        queue.retry(task["id"])
    queue.retry(task["id"], inspected=True)
    retry = queue.claim(seat, "replacement")
    result = execute(db, db.row("SELECT * FROM seats WHERE id=?", (seat,)), retry, lambda: True, lambda: False)
    assert queue.finish(retry["id"], "replacement", retry["attempt"], result)
    assert retry["attempt"] == 2
    assert (path / "app.py").read_text() == "value = 3\n"
    assert (project / "app.py").read_text() == "value = 1\n"
    assert db.scope(run)["state"] == "running"


@pytest.mark.parametrize("mutation", ["head", "pointer", "escape"])
def test_inspected_retry_refuses_unsafe_writer_before_requeue(tmp_path, project, mutation):
    db = Database(tmp_path / "state")
    _, _, path, task = writer_task(db, project)
    queue = Queue(db)
    queue.block(task["id"], "Worker died", "Inspect and retry")
    if mutation == "head":
        git(path, "commit", "-am", "unauthorized writer commit")
    elif mutation == "pointer":
        (path / ".git").write_text(f"gitdir: {project / '.git'}\n")
    else:
        (tmp_path / "outside").write_text("private")
        (path / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(UserError):
        queue.retry(task["id"], inspected=True)
    assert db.row("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"] == "blocked"


def test_inspected_writer_permission_does_not_authorize_a_later_attempt(tmp_path, project, writer_adapter):
    db = Database(tmp_path / "state")
    _, seat, _, task = writer_task(db, project)
    queue = Queue(db)
    queue.block(task["id"], "Worker died", "Inspect and retry")
    queue.retry(task["id"], inspected=True)
    second = queue.claim(seat, "second")
    queue.block(task["id"], "Replacement died", "Inspect again")
    # Simulate a mistaken automatic requeue; the earlier inspection must not carry over.
    db.execute("UPDATE tasks SET state='pending' WHERE id=?", (task["id"],))
    db.set_state("scopes", task["scope_id"], "running")
    third = queue.claim(seat, "third")
    assert third["attempt"] == second["attempt"] + 1
    with pytest.raises(UserError, match="baseline changed"):
        execute(db, db.row("SELECT * FROM seats WHERE id=?", (seat,)), third, lambda: True, lambda: False)


@pytest.mark.parametrize("mutation", ["head", "pointer", "escape"])
def test_inspected_retry_rechecks_writer_ownership_at_launch(tmp_path, project, mutation, writer_adapter):
    db = Database(tmp_path / "state")
    _, seat, path, task = writer_task(db, project)
    queue = Queue(db)
    queue.block(task["id"], "Worker died", "Inspect and retry")
    queue.retry(task["id"], inspected=True)
    retry = queue.claim(seat, "replacement")
    if mutation == "head":
        git(path, "commit", "-am", "late unauthorized commit")
    elif mutation == "pointer":
        (path / ".git").write_text(f"gitdir: {project / '.git'}\n")
    else:
        (tmp_path / "outside").write_text("private")
        (path / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(UserError):
        execute(db, db.row("SELECT * FROM seats WHERE id=?", (seat,)), retry, lambda: True, lambda: False)
    assert (path / "app.py").read_text() == "value = 2\n"
    assert (project / "app.py").read_text() == "value = 1\n"


def test_queue_skips_pending_verification_for_superseded_candidate(tmp_path, project):
    db = Database(tmp_path / "state")
    snap = preflight(project, "develop")
    run = workflow.new_run(db, snap, "develop", validate_config({}))
    seat = db.add_seat(run, "correctness", "codex", str(project))
    db.set_state("scopes", run, "running")
    queue = Queue(db)
    old = db.add_candidate(run, snap["source_head_sha"], "old", None)
    queue.create(run, seat, "correctness", {}, candidate_id=old)
    current = db.add_candidate(run, "b" * 40, "current", None, parent_id=old)
    fresh = queue.create(run, seat, "correctness", {}, candidate_id=current)
    assert queue.claim(seat, "reviewer")["id"] == fresh


def test_only_returning_owner_releases_obsolete_claim_without_accepting_result(tmp_path, project):
    db = Database(tmp_path / "state")
    snap = preflight(project, "develop")
    run = workflow.new_run(db, snap, "develop", validate_config({}))
    seat = db.add_seat(run, "correctness", "codex", str(project))
    db.set_state("scopes", run, "running")
    queue = Queue(db)
    old = db.add_candidate(run, snap["source_head_sha"], "old", None)
    task_id = queue.create(run, seat, "correctness", {}, candidate_id=old)
    old_task = queue.claim(seat, "old-reviewer")
    current = db.add_candidate(run, "b" * 40, "current", None, parent_id=old)
    fresh = queue.create(run, seat, "correctness", {}, candidate_id=current)
    assert not queue.finish(task_id, "stranger", old_task["attempt"], {"data": {}}, candidate_id=old)
    assert not queue.finish(task_id, "old-reviewer", old_task["attempt"] - 1, {"data": {}}, candidate_id=old)
    assert db.row("SELECT state FROM tasks WHERE id=?", (task_id,))["state"] == "claimed"
    assert queue.claim(seat, "new-reviewer") is None
    assert not queue.finish(task_id, "old-reviewer", old_task["attempt"], {"data": {}}, candidate_id=old)
    assert db.row("SELECT state,lease_owner FROM tasks WHERE id=?", (task_id,)) == {
        "state": "canceled", "lease_owner": None}
    assert queue.claim(seat, "new-reviewer")["id"] == fresh


def test_verification_creation_crash_resumes_same_candidate_and_tasks(tmp_path, project, monkeypatch):
    db = Database(tmp_path / "state")
    run, _, _, task = writer_task(db, project)
    assert Queue(db).finish(task["id"], "old-writer", task["attempt"], {"data": implementation()})
    original = workflow.seat_worktree

    class Crash(BaseException):
        pass

    def crash_at_tests(db, run, role, *args, **kwargs):
        if role == "tests":
            raise Crash()
        return original(db, run, role, *args, **kwargs)

    monkeypatch.setattr(workflow, "seat_worktree", crash_at_tests)
    with pytest.raises(Crash):
        advance_develop(db, db.run(run))
    candidate = db.run(run)["current_candidate_id"]
    assert db.run(run)["phase"] == "verify"
    existing = db.row("SELECT id FROM tasks WHERE kind='correctness'")["id"]
    monkeypatch.setattr(workflow, "seat_worktree", original)
    workflow.advance(db, run)
    workflow.prepare_verification(db, db.run(run))
    assert db.run(run)["current_candidate_id"] == candidate
    assert len(db.rows("SELECT id FROM candidates WHERE run_id=?", (run,))) == 1
    assert len(db.rows("SELECT id FROM tasks WHERE kind='correctness'")) == 1
    assert db.row("SELECT id FROM tasks WHERE kind='correctness'")["id"] == existing
    queue = Queue(db)
    for role in ("correctness", "tests"):
        reviewer = role + "@" + run
        review = queue.claim(reviewer, role)
        assert review is not None
        assert queue.finish(review["id"], role, review["attempt"],
                            {"data": {"schema_version": 1, "summary": "Reviewed", "findings": [], "limitations": []}},
                            candidate_id=candidate)
    workflow.advance(db, run)
    assert db.scope(run)["state"] == "completed"


def test_freeze_database_failure_rolls_back_candidate_and_checkpoint(tmp_path, project, monkeypatch):
    db = Database(tmp_path / "state")
    run, _, path, _ = writer_task(db, project)
    original = db.execute

    def fail_worktree_update(sql, args=()):
        if sql.startswith("UPDATE worktrees SET candidate_id="):
            raise UserError("Simulated database write failure")
        return original(sql, args)

    monkeypatch.setattr(db, "execute", fail_worktree_update)
    with pytest.raises(UserError, match="database write"):
        freeze(path, db, run)
    assert db.rows("SELECT id FROM candidates WHERE run_id=?", (run,)) == []
    assert db.run(run)["current_candidate_id"] is None
    assert db.run(run)["phase"] == "implement"
    assert db.row("SELECT candidate_id FROM worktrees WHERE path=?", (str(path),))["candidate_id"] is None
    assert (project / "app.py").read_text() == "value = 1\n"


def test_failed_reviewer_retry_reopens_completed_partial_evaluation(tmp_path, project):
    git(project, "switch", "-c", "feature")
    (project / "app.py").write_text("value = 2\n")
    git(project, "commit", "-am", "feature")
    config = validate_config({"checks": [{"name": "unit", "argv": [sys.executable, "-c", "pass"],
                                           "timeout_seconds": 5}]})
    db = Database(tmp_path / "state")
    snap = preflight(project, "review", "main")
    run = workflow.new_run(db, snap, "review", config)
    candidate = db.add_candidate(run, snap["source_head_sha"], snap["diff_sha256"], None)
    db.execute("UPDATE runs SET phase='evaluate' WHERE id=?", (run,))
    db.set_state("scopes", run, "running")
    queue = Queue(db)
    results = {"data": {"schema_version": 1, "summary": "Reviewed", "findings": [], "limitations": []}}
    tasks = {}
    for role in ("correctness", "tests"):
        seat = db.add_seat(run, role, "codex", str(project))
        tid = queue.create(run, seat, role, {}, candidate_id=candidate)
        task = queue.claim(seat, role)
        queue.finish(tid, role, task["attempt"], results if role == "correctness" else {"error": "provider_error"},
                     candidate_id=candidate, failed=role == "tests")
        tasks[role] = tid, seat
    checks_seat = db.add_seat(run, "checks", "codex", "")
    checks = create_worktree(db, run, snap["source_head_sha"], "checks", candidate_id=candidate,
                             seat_id=checks_seat)
    checks_id = queue.create(run, checks_seat, "checks", {"cwd": str(checks)}, candidate_id=candidate)
    checks_task = queue.claim(checks_seat, "checks")
    check_results = run_checks(db, run, candidate, checks, config["checks"])
    assert queue.finish(checks_id, "checks", checks_task["attempt"], {"checks": check_results},
                        candidate_id=candidate)
    workflow.advance(db, run)
    assert db.run(run)["outcome"] == "partial"
    assert db.scope(run)["state"] == "completed"
    tid, seat = tasks["tests"]
    queue.retry(tid)
    retry = queue.claim(seat, "replacement")
    assert queue.finish(tid, "replacement", retry["attempt"], results, candidate_id=candidate)
    workflow.advance(db, run)
    assert db.run(run)["outcome"] == "accepted"
    assert db.scope(run)["state"] == "completed"


@pytest.mark.parametrize("kind", ["plan", "implement", "correctness", "checks"])
def test_external_completion_cannot_forge_workflow_result(tmp_path, project, kind):
    db = Database(tmp_path / "state")
    run = workflow.new_run(db, preflight(project, "develop"), "develop", validate_config({}))
    seat = db.add_seat(run, "implementer", "codex", str(project))
    queue = Queue(db)
    task = queue.create(run, seat, kind, {})
    with pytest.raises(UserError):
        queue.complete(task, {"no_artifact": "Handled manually"})
    assert db.row("SELECT state,result_json FROM tasks WHERE id=?", (task,)) == {"state": "pending", "result_json": None}


def test_team_external_completion_still_accepts_manual_reason(tmp_path):
    db = Database(tmp_path / "state")
    team = db.create_scope("team", str(tmp_path))
    db.execute("INSERT INTO teams VALUES(?,?,?)", (team, "pair", "{}"))
    seat = db.add_seat(team, "dev-owner", "codex", str(tmp_path))
    queue = Queue(db)
    task = queue.create(team, seat, "team.implement", {})
    queue.complete(task, {"no_artifact": "Answered verbally"})
    row = db.row("SELECT state,result_json FROM tasks WHERE id=?", (task,))
    assert row["state"] == "done"
    assert json.loads(row["result_json"]) == {"no_artifact": "Answered verbally"}


@pytest.mark.parametrize("kind", ["implement", "revise"])
@pytest.mark.parametrize("crash_at", ["ready_worktree", "task_insert", "phase_checkpoint"])
def test_writer_scheduling_crash_reuses_owned_branch(tmp_path, project, monkeypatch, kind, crash_at):
    from agentcrew import git_snapshot

    db = Database(tmp_path / "state")
    snap = preflight(project, "develop")
    run = workflow.new_run(db, snap, "develop", validate_config({}), spec="Change value")
    db.execute("UPDATE runs SET plan_json=? WHERE id=?", (canonical({"requirements": []}), run))
    db.set_state("scopes", run, "running")
    old_phase = "plan"
    if kind == "revise":
        db.add_candidate(run, snap["source_head_sha"], "empty", None)
        old_phase = "evaluate"
        db.execute("UPDATE runs SET phase='evaluate' WHERE id=?", (run,))

    def schedule():
        if kind == "implement":
            development.implement(db, db.run(run))
        else:
            development.revise(db, db.run(run), {"reason": "Change needed"})

    class Crash(BaseException):
        pass

    real_task_for = development.task_for
    real_set_phase = development.set_phase
    real_git = git_snapshot.git

    def crash_task(*args, **kwargs):
        if crash_at == "ready_worktree":
            raise Crash()
        task = real_task_for(*args, **kwargs)
        if crash_at == "task_insert":
            raise Crash()
        return task

    def crash_phase(*args, **kwargs):
        raise Crash()

    def git_without_database_lock(*args, **kwargs):
        assert not db.conn.in_transaction, "Git must not hold the SQLite write lock"
        return real_git(*args, **kwargs)

    monkeypatch.setattr(git_snapshot, "git", git_without_database_lock)
    monkeypatch.setattr(development, "task_for", crash_task)
    if crash_at == "phase_checkpoint":
        monkeypatch.setattr(development, "set_phase", crash_phase)
    with pytest.raises(Crash):
        schedule()
    assert db.run(run)["phase"] == old_phase
    worktree = db.row("SELECT * FROM worktrees WHERE scope_id=?", (run,))
    assert worktree["state"] == "ready"
    monkeypatch.setattr(development, "task_for", real_task_for)
    monkeypatch.setattr(development, "set_phase", real_set_phase)
    schedule()
    schedule()
    assert db.run(run)["phase"] == kind
    tasks = db.rows("SELECT * FROM tasks WHERE scope_id=? AND kind=?", (run, kind))
    assert len(tasks) == 1
    assert json.loads(tasks[0]["body_json"])["cwd"] == worktree["path"]
    assert len(db.rows("SELECT * FROM worktrees WHERE scope_id=?", (run,))) == 1
    assert git(project, "status", "--porcelain") == ""
    assert (project / "app.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("kind", ["implement", "revise"])
@pytest.mark.parametrize("task_state", ["pending", "done"])
def test_writer_scheduling_recovers_existing_task_without_reissuing(tmp_path, project, kind, task_state):
    db = Database(tmp_path / "state")
    snap = preflight(project, "develop")
    run = workflow.new_run(db, snap, "develop", validate_config({}), spec="Change value")
    db.execute("UPDATE runs SET plan_json=? WHERE id=?", (canonical({"requirements": []}), run))
    db.set_state("scopes", run, "running")
    if kind == "revise":
        db.add_candidate(run, snap["source_head_sha"], "empty", None)

    def schedule():
        if kind == "implement":
            development.implement(db, db.run(run))
        else:
            development.revise(db, db.run(run), {"reason": "Change needed"})

    schedule()
    task = db.row("SELECT * FROM tasks WHERE scope_id=? AND kind=?", (run, kind))
    if task_state == "done":
        queue = Queue(db)
        claimed = queue.claim(task["owner_seat"], "writer")
        assert queue.finish(task["id"], "writer", claimed["attempt"], {"data": implementation()},
                            candidate_id=task["candidate_id"])
    # Reconcile a task persisted by the previous non-atomic scheduling code.
    db.execute("UPDATE runs SET phase=? WHERE id=?", ("plan" if kind == "implement" else "evaluate", run))
    schedule()
    assert db.run(run)["phase"] == kind
    rows = db.rows("SELECT id,state FROM tasks WHERE scope_id=? AND kind=?", (run, kind))
    assert rows == [{"id": task["id"], "state": task_state}]
    assert len(db.rows("SELECT id FROM worktrees WHERE scope_id=?", (run,))) == 1
