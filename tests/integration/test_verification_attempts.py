"""Current check attempts and reviewer retries must not inherit stale decisions."""

import subprocess
import sys

import pytest

from agentcrew.config import validate_config
from agentcrew.db import Database
from agentcrew.models import canonical, digest, uid
from agentcrew.queue import Queue
from agentcrew.review.checks import run_checks
from agentcrew.review.report import build_report
from agentcrew.review.workflow import advance, new_run, persist_findings


@pytest.fixture
def context(tmp_path):
    from agentcrew.git_snapshot import preflight

    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@local")
    (repo / "app.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-m", "baseline")
    git("switch", "-c", "feature")
    (repo / "app.py").write_text("value = 2\n")
    git("commit", "-am", "candidate")
    flag = tmp_path / "fail"
    config = validate_config({"checks": [
        {"name": name, "argv": [sys.executable, "-c",
         f"from pathlib import Path; raise SystemExit(int(Path({str(flag)!r}).exists()))"],
         "timeout_seconds": 5} for name in ("unit", "lint")
    ]})
    db = Database(tmp_path / "state")
    snapshot = preflight(repo, "review", "main")
    rid = new_run(db, snapshot, "review", config)
    cid = db.add_candidate(rid, snapshot["source_head_sha"], snapshot["diff_sha256"], None)
    db.execute("UPDATE runs SET phase='evaluate' WHERE id=?", (rid,))
    db.set_state("scopes", rid, "running")
    queue = Queue(db)
    for role in ("correctness", "tests", "checks"):
        seat = db.add_seat(rid, role, "codex", str(repo))
        if role != "checks":
            task = queue.create(rid, seat, role, {}, candidate_id=cid)
            claimed = queue.claim(seat, "fixture")
            queue.finish(task, "fixture", claimed["attempt"], review(), candidate_id=cid)
    yield db, rid, cid, repo, flag
    db.close()


def review(findings=()):
    return {"data": {"schema_version": 1, "summary": "reviewed", "findings": list(findings),
                     "limitations": []}}


def check_task(db, rid, cid, results, *, state="done"):
    queue = Queue(db)
    task = queue.create(rid, "checks@" + rid, "checks", {}, candidate_id=cid)
    claimed = queue.claim("checks@" + rid, "fixture")
    if state == "done":
        queue.finish(task, "fixture", claimed["attempt"], {"checks": results}, candidate_id=cid)
    elif state == "failed":
        queue.finish(task, "fixture", claimed["attempt"], {"error": "worker failed"},
                     candidate_id=cid, failed=True)
    else:
        queue.block(task, "interrupted", "retry checks")
        db.set_state("scopes", rid, "running")
    return task


def test_latest_check_attempt_failure_cannot_inherit_an_earlier_pass(context):
    db, rid, cid, repo, flag = context
    old = run_checks(db, rid, cid, repo, db.run(rid)["config"]["checks"])
    task = check_task(db, rid, cid, old, state="blocked")
    queue = Queue(db)
    queue.retry(task)
    claimed = queue.claim("checks@" + rid, "fixture")
    flag.touch()
    current = run_checks(db, rid, cid, repo, db.run(rid)["config"]["checks"])
    assert queue.finish(task, "fixture", claimed["attempt"], {"checks": current}, candidate_id=cid)
    assert claimed["attempt"] == 2
    advance(db, rid)
    assert db.run(rid)["outcome"] == "rejected"
    report = build_report(db, rid)
    assert {row["id"] for row in report["checks"]} == {row["id"] for row in current}
    assert len(db.rows("SELECT * FROM check_runs WHERE candidate_id=?", (cid,))) == 4


@pytest.mark.parametrize("current", ["failed", "blocked", "incomplete", "missing_result"])
def test_incomplete_current_attempt_never_uses_previous_check_rows(context, current):
    db, rid, cid, repo, _ = context
    config = db.run(rid)["config"]
    old = run_checks(db, rid, cid, repo, config["checks"])
    check_task(db, rid, cid, old)
    if current == "incomplete":
        results = run_checks(db, rid, cid, repo, config["checks"][:1])
        check_task(db, rid, cid, results)
    else:
        task = check_task(db, rid, cid, [], state=current if current != "missing_result" else "done")
        if current == "missing_result":
            db.execute("UPDATE tasks SET result_json='{}' WHERE id=?", (task,))
    advance(db, rid)
    assert db.run(rid)["outcome"] == "partial"
    report = build_report(db, rid)
    assert report["status"] == "partial"
    assert not {row["id"] for row in old} & {row["id"] for row in report["checks"]}


def test_check_task_cannot_claim_foreign_candidate_evidence(context):
    db, rid, cid, repo, _ = context
    old = run_checks(db, rid, cid, repo, db.run(rid)["config"]["checks"])
    newer = db.add_candidate(rid, db.run(rid)["source_head_sha"], digest("unused"), None)
    check_task(db, rid, newer, old)
    report = build_report(db, rid)
    assert report["status"] == "partial"
    assert report["checks"] == []


def test_corrupted_machine_report_cannot_remain_accepted_evidence(context):
    from pathlib import Path

    db, rid, cid, repo, _ = context
    config = db.run(rid)["config"]
    config["checks"][0].update(
        argv=[sys.executable, "-c", "from pathlib import Path; Path('results.xml').write_text('<testsuite/>')"],
        result_format="junit", result_file="results.xml",
    )
    db.execute("UPDATE runs SET config_json=? WHERE id=?", (canonical(config), rid))
    results = run_checks(db, rid, cid, repo, config["checks"])
    check_task(db, rid, cid, results)
    Path(results[0]["result_path"]).write_text("corrupted after check completion")
    advance(db, rid)
    assert db.run(rid)["outcome"] == "partial"
    assert build_report(db, rid)["status"] == "partial"


def test_requirement_rejects_stale_test_proof_absent_from_current_attempt(context):
    from agentcrew.develop.requirements import evaluate_requirements

    db, rid, cid, repo, _ = context
    db.execute("INSERT INTO requirements VALUES(?,?,?,?,?)", (
        uid(), rid, "A1", "Exact behavior",
        canonical({"kind": "test", "check": "unit", "test_id": "specific behavior"})))
    checks = [{"name": "unit", "argv": [sys.executable, "-c",
               "from pathlib import Path; Path('results.xml').write_text("
               "'<testsuite><testcase name=\"specific behavior\"/></testsuite>')"],
               "timeout_seconds": 5, "result_format": "junit", "result_file": "results.xml"}]
    old = run_checks(db, rid, cid, repo, checks)
    check_task(db, rid, cid, old)
    assert evaluate_requirements(db, rid, cid, old)[0]["status"] == "passed"
    check_task(db, rid, cid, [])
    assert evaluate_requirements(db, rid, cid, old)[0]["status"] == "unknown"


def claim(detail, severity="P1"):
    return {"title": "Changed API", "severity": severity, "file": "app.py", "line": 1,
            "detail": detail, "evidence": detail, "reproduction": "Read app.value",
            "suggestion": "Preserve behavior", "confidence": 0.8}


def test_retry_adds_new_severe_findings_without_reusing_dismissal_for_another_cause(context):
    from agentcrew.cli import finding_decision

    db, rid, cid, repo, _ = context
    db.execute("UPDATE tasks SET result_json=? WHERE kind='correctness' AND scope_id=?",
               (canonical(review([claim("First cause", "P2")])), rid))
    tasks = db.rows("SELECT * FROM tasks WHERE scope_id=?", (rid,))
    original = persist_findings(db, db.run(rid), tasks)[0]
    finding_decision(db, rid, original["id"], "dismissed", "Intended behavior inspected by operator")
    db.execute("UPDATE tasks SET result_json=? WHERE kind='tests' AND scope_id=?",
               (canonical(review([claim("A different cause", "P0")])), rid))
    tasks = db.rows("SELECT * FROM tasks WHERE scope_id=?", (rid,))
    findings = persist_findings(db, db.run(rid), tasks)
    assert len(findings) == 2
    previous = next(row for row in findings if row["id"] == original["id"])
    fresh = next(row for row in findings if row["detail"] == "A different cause")
    assert previous["status"] == "dismissed"
    assert fresh["severity"] == "P0" and fresh["status"] == "reported"
    assert previous["fingerprint"] != fresh["fingerprint"]
    assert {row["id"] for row in persist_findings(db, db.run(rid), tasks)} == {
        row["id"] for row in findings}
    check_task(db, rid, cid, run_checks(db, rid, cid, repo, db.run(rid)["config"]["checks"]))
    advance(db, rid)
    assert db.run(rid)["outcome"] == "rejected"
    assert any(row["severity"] == "P0" and row["status"] == "reported"
               for row in build_report(db, rid)["findings"])


@pytest.mark.parametrize("original,current", [
    ("Authentication accepts 'Root'", "Authentication accepts 'root'"),
    ("Authentication accepts 'a  b'", "Authentication accepts 'a b'"),
])
def test_literal_case_and_whitespace_cannot_transfer_an_audited_dismissal(context, original, current):
    from agentcrew.cli import finding_decision

    db, rid, cid, repo, _ = context
    db.execute("UPDATE tasks SET result_json=? WHERE kind='correctness' AND scope_id=?",
               (canonical(review([claim(original, "P2")])), rid))
    previous = persist_findings(db, db.run(rid), db.rows("SELECT * FROM tasks WHERE scope_id=?", (rid,)))[0]
    finding_decision(db, rid, previous["id"], "dismissed", "Only the exact reviewed literal is intended")
    db.execute("UPDATE tasks SET result_json=? WHERE kind='tests' AND scope_id=?",
               (canonical(review([claim(current, "P1")])), rid))
    check_task(db, rid, cid, run_checks(db, rid, cid, repo, db.run(rid)["config"]["checks"]))
    advance(db, rid)
    assert db.run(rid)["outcome"] == "rejected"
    findings = build_report(db, rid)["findings"]
    assert len(findings) == 2
    assert next(row for row in findings if row["detail"] == current)["status"] == "reported"
    assert next(row for row in findings if row["id"] == previous["id"])["status"] == "dismissed"


def test_verify_phase_preparation_is_idempotent_across_partial_scheduling(context, monkeypatch):
    import agentcrew.review.workflow as workflow

    db, rid, cid, _, _ = context
    db.execute("DELETE FROM tasks WHERE scope_id=?", (rid,))
    db.execute("UPDATE runs SET phase='verify' WHERE id=?", (rid,))
    real_task_for = workflow.task_for

    def fail_second_role(*args, **kwargs):
        if args[2] == "tests":
            raise OSError("simulated controller crash after scheduling correctness")
        return real_task_for(*args, **kwargs)

    monkeypatch.setattr(workflow, "task_for", fail_second_role)
    with pytest.raises(OSError):
        workflow.prepare_verification(db, db.run(rid))
    monkeypatch.setattr(workflow, "task_for", real_task_for)
    workflow.advance(db, rid)
    workflow.prepare_verification(db, db.run(rid))
    assert db.run(rid)["phase"] == "evaluate"
    tasks = db.rows("SELECT * FROM tasks WHERE scope_id=? AND candidate_id=?", (rid, cid))
    assert sorted(row["kind"] for row in tasks) == ["checks", "correctness", "tests"]
    assert len(db.rows("SELECT * FROM worktrees WHERE scope_id=?", (rid,))) == 3
