import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentcrew.db import Database
from agentcrew.models import canonical, digest, uid
from agentcrew.queue import Queue


def git(repo, *args):
    return (
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )


def record_checks(db, run, candidate, repo, results):
    """Mirror the worker's fenced completion; standalone rows are not workflow proof."""
    seat = "checks@" + run
    if not db.row("SELECT id FROM seats WHERE id=?", (seat,)):
        db.add_seat(run, "checks", "codex", str(repo))
    db.set_state("scopes", run, "running")
    queue = Queue(db)
    task = queue.create(run, seat, "checks", {}, candidate_id=candidate)
    claimed = queue.claim(seat, "fixture")
    assert queue.finish(task, "fixture", claimed["attempt"], {"checks": results}, candidate_id=candidate)


@pytest.fixture
def context(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@local")
    (repo / "tests").mkdir()
    (repo / "tests/test_export.py").write_text("def test_empty(): pass\n")
    (repo / "result.xml").write_text(
        '<testsuite><testcase classname="tests.test_export" name="test_old"/></testsuite>'
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    sha = git(repo, "rev-parse", "HEAD")
    db = Database(tmp_path / "state")
    run = db.create_scope("run", str(repo))
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) VALUES(?,?,?,?,?,?)",
        (run, "develop", sha, sha, "checks", "{}"),
    )
    candidate = db.add_candidate(run, sha, digest(""), None)
    yield db, run, candidate, repo, sha
    db.close()


def test_checks_run_in_separate_fresh_worktrees_and_persist_exact_proof(context):
    from agentcrew.review.checks import run_checks

    db, run, candidate, repo, sha = context
    xml = '<testsuite><testcase classname="tests.test_export" name="test_empty"/></testsuite>'
    script = (
        "from pathlib import Path; assert not Path('marker').exists(); Path('marker').write_text('yes');"
        f"Path('result.xml').write_text({xml!r})"
    )
    checks = [
        {
            "name": name,
            "argv": [sys.executable, "-c", script],
            "timeout_seconds": 5,
            "result_format": "junit",
            "result_file": "result.xml",
        }
        for name in ("unit", "second")
    ]
    results = run_checks(db, run, candidate, repo, checks)
    assert [r["state"] for r in results] == ["passed", "passed"]
    assert results[0]["tests"] == {"tests/test_export.py::test_empty": "passed"}
    assert results[0]["candidate_sha"] == sha
    assert not (repo / "marker").exists()
    assert "test_old" in (repo / "result.xml").read_text()
    assert len(db.rows("SELECT * FROM worktrees WHERE candidate_id=?", (candidate,))) == 2
    assert len(db.rows("SELECT * FROM check_evidence")) == 2
    assert Path(results[0]["log_path"]).stat().st_mode & 0o777 == 0o600


def test_real_pytest_junit_names_map_to_exact_requirement_ids(context):
    from agentcrew.review.checks import run_checks

    db, run, candidate, repo, _ = context
    result = run_checks(
        db,
        run,
        candidate,
        repo,
        [
            {
                "name": "unit",
                "argv": [sys.executable, "-m", "pytest", "-q", "--junitxml=.agentcrew-results/unit.xml"],
                "timeout_seconds": 10,
                "result_format": "junit",
                "result_file": ".agentcrew-results/unit.xml",
            }
        ],
    )[0]
    assert result["state"] == "passed"
    assert result["tests"] == {"tests/test_export.py::test_empty": "passed"}


def test_real_node_junit_uses_native_test_names_and_confined_absolute_paths(tmp_path):
    from agentcrew.review.checks import parse_junit

    if shutil.which("node") is None:
        pytest.skip("Node not installed")
    (tmp_path / "export.test.mjs").write_text("import test from 'node:test';test('exports header',()=>{});\n")
    report = tmp_path / "results.xml"
    subprocess.run(
        ["node", "--test", "--test-reporter=junit", f"--test-reporter-destination={report}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert parse_junit(report, tmp_path) == {"exports header": "passed"}


def test_stale_junit_is_removed_and_cannot_make_passing_evidence(context):
    from agentcrew.review.checks import run_checks

    db, run, candidate, repo, _ = context
    result = run_checks(
        db,
        run,
        candidate,
        repo,
        [
            {
                "name": "unit",
                "argv": [sys.executable, "-c", "pass"],
                "timeout_seconds": 5,
                "result_format": "junit",
                "result_file": "result.xml",
            }
        ],
    )[0]
    assert result["state"] == "failed"
    assert result["tests"] == {}
    assert "result" in result["error"].lower()


def test_check_cannot_prove_a_candidate_after_modifying_tracked_source(context):
    from agentcrew.review.checks import run_checks

    db, run, candidate, repo, _ = context
    script = "from pathlib import Path; Path('tests/test_export.py').write_text('def test_empty(): pass\\n# modified during check\\n')"
    result = run_checks(
        db,
        run,
        candidate,
        repo,
        [{"name": "unit", "argv": [sys.executable, "-c", script], "timeout_seconds": 5}],
    )[0]
    assert result["state"] == "failed"
    assert "snapshot" in result["error"].lower()


def test_review_rejects_file_absent_from_frozen_commit_even_if_it_exists(context):
    from agentcrew.models import UserError
    from agentcrew.review.validate import validate_review

    _, _, _, repo, sha = context
    (repo / "ghost.py").write_text("uncommitted ignored text\n")
    review = {
        "schema_version": 1,
        "summary": "claim",
        "limitations": [],
        "findings": [
            {
                "title": "Ghost",
                "severity": "P1",
                "file": "ghost.py",
                "line": 1,
                "detail": "Changed behavior in the diff",
                "evidence": "text",
                "reproduction": "read file",
                "suggestion": "",
                "confidence": 0.8,
            }
        ],
    }
    with pytest.raises(UserError, match="frozen commit"):
        validate_review(review, repo, candidate_sha=sha)


def test_junit_skipped_failed_duplicate_and_entities_are_not_clean_passes(tmp_path):
    from agentcrew.models import UserError
    from agentcrew.review.checks import parse_junit

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_export.py").touch()
    path = tmp_path / "results.xml"
    path.write_text(
        '<testsuite><testcase classname="tests.test_export" name="skip"><skipped/></testcase>'
        '<testcase classname="tests.test_export" name="bad"><failure/></testcase>'
        '<testcase classname="tests.test_export" name="duplicate"/>'
        '<testcase classname="tests.test_export" name="duplicate"/></testsuite>'
    )
    assert parse_junit(path, tmp_path) == {
        "tests/test_export.py::skip": "unknown",
        "tests/test_export.py::bad": "failed",
        "tests/test_export.py::duplicate": "unknown",
    }
    path.write_text('<!DOCTYPE suite [<!ENTITY secret SYSTEM "file:///etc/passwd">]><testsuite/>')
    with pytest.raises(UserError):
        parse_junit(path, tmp_path)
    path.write_text('<testsuite><testcase file="../outside.py" name="bad"/></testsuite>')
    with pytest.raises(UserError):
        parse_junit(path, tmp_path)


def test_timeout_kills_process_group_and_records_failure(context):
    from agentcrew.review.checks import run_checks

    db, run, candidate, repo, _ = context
    result = run_checks(
        db,
        run,
        candidate,
        repo,
        [
            {
                "name": "hung",
                "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                "timeout_seconds": 1,
            }
        ],
    )[0]
    assert result["state"] == "failed"
    assert result["timed_out"] is True
    assert result["duration_ms"] < 5000


def test_failed_test_in_nonzero_check_proves_requirement_failure(context):
    from agentcrew.develop.requirements import evaluate_requirements
    from agentcrew.review.checks import run_checks

    db, run, candidate, repo, _ = context
    db.execute(
        "INSERT INTO requirements VALUES(?,?,?,?,?)",
        (
            uid(),
            run,
            "A1",
            "Export works",
            canonical({"kind": "test", "check": "unit", "test_id": "tests/test_export.py::test_empty"}),
        ),
    )
    xml = '<testsuite><testcase classname="tests.test_export" name="test_empty"><failure/></testcase></testsuite>'
    script = f"from pathlib import Path; Path('result.xml').write_text({xml!r}); raise SystemExit(1)"
    results = run_checks(
        db,
        run,
        candidate,
        repo,
        [
            {
                "name": "unit",
                "argv": [sys.executable, "-c", script],
                "timeout_seconds": 5,
                "result_format": "junit",
                "result_file": "result.xml",
            }
        ],
    )
    record_checks(db, run, candidate, repo, results)
    assert evaluate_requirements(db, run, candidate, results)[0]["status"] == "failed"


def test_junit_no_execution_status_cannot_prove_pass(tmp_path):
    from agentcrew.review.checks import parse_junit

    path = tmp_path / "results.xml"
    path.write_text('<testsuite><testcase classname="Export" name="empty" result="suppressed"/></testsuite>')
    assert parse_junit(path, tmp_path) == {"Export::empty": "unknown"}


def test_requirements_need_recorded_same_candidate_exact_executed_test(context):
    from agentcrew.develop.requirements import evaluate_requirements
    from agentcrew.review.checks import run_checks

    db, run, candidate, repo, sha = context
    requirement = uid()
    db.execute(
        "INSERT INTO requirements VALUES(?,?,?,?,?)",
        (
            requirement,
            run,
            "A1",
            "Exports empty orders",
            canonical({"kind": "test", "check": "unit", "test_id": "tests/test_export.py::test_empty"}),
        ),
    )
    plain = run_checks(
        db,
        run,
        candidate,
        repo,
        [{"name": "unit", "argv": [sys.executable, "-c", "pass"], "timeout_seconds": 5}],
    )
    record_checks(db, run, candidate, repo, plain)
    assert evaluate_requirements(db, run, candidate, plain)[0]["status"] == "unknown"
    plain[0]["tests"] = {"tests/test_export.py::test_empty": "passed"}
    assert evaluate_requirements(db, run, candidate, plain)[0]["status"] == "unknown"
    script = "from pathlib import Path; Path('result.xml').write_text('<testsuite><testcase classname=\"tests.test_export\" name=\"test_empty\"/></testsuite>')"
    actual = run_checks(
        db,
        run,
        candidate,
        repo,
        [
            {
                "name": "unit",
                "argv": [sys.executable, "-c", script],
                "timeout_seconds": 5,
                "result_format": "junit",
                "result_file": "result.xml",
            }
        ],
    )
    record_checks(db, run, candidate, repo, actual)
    assert evaluate_requirements(db, run, candidate, actual)[0]["status"] == "passed"
    newer = db.add_candidate(run, sha, digest(""), None)
    assert evaluate_requirements(db, run, newer, actual)[0]["status"] == "unknown"
    Path(actual[0]["result_path"]).write_text("tampered")
    assert evaluate_requirements(db, run, candidate, actual)[0]["status"] == "unknown"


def test_manual_confirmation_is_candidate_bound_and_audited(context):
    from agentcrew.develop.requirements import evaluate_requirements
    from agentcrew.models import now

    db, run, candidate, _, sha = context
    requirement = uid()
    db.execute(
        "INSERT INTO requirements VALUES(?,?,?,?,?)",
        (requirement, run, "A1", "Observe export", canonical({"kind": "manual", "check": "", "test_id": ""})),
    )
    assert evaluate_requirements(db, run, candidate, [])[0]["status"] == "unknown"
    db.execute(
        "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
        (
            uid(),
            run,
            candidate,
            "requirement",
            requirement,
            canonical({"status": "passed", "actor": "user", "evidence": "Opened the exported CSV"}),
            now(),
        ),
    )
    assert evaluate_requirements(db, run, candidate, [])[0]["status"] == "passed"
    newer = db.add_candidate(run, sha, digest(""), None)
    assert evaluate_requirements(db, run, newer, [])[0]["status"] == "unknown"
