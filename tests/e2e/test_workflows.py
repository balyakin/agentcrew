import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentcrew.config import validate_config
from agentcrew.db import Database
from agentcrew.develop.workflow import start_develop
from agentcrew.review.workflow import start_review
from agentcrew.tmux_backend import Tmux


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@local")
    (repo / "app.py").write_text('def greet():\n    return "old"\n')
    (repo / "README.md").write_text("# fixture\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return repo


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "codex"
    binary.write_text("""#!/usr/bin/env python3
import json,sys,pathlib,os,time
args=sys.argv[1:]
if '--version' in args: print('codex-cli 0.160.0');sys.exit(0)
if '--help' in args: print('login\\nstatus\\n--json --output-schema --output-last-message -C --sandbox read-only workspace-write --ask-for-approval --model');sys.exit(0)
if args[:2]==['login','status']: print('Logged in using ChatGPT');sys.exit(0)
prompt=sys.stdin.read()
if 'AGENTCREW_KIND:plan' in prompt:
 result={'schema_version':1,'summary':'greet change','requirements':[{'id':'A1','description':'returns new','verification':{'kind':'test','check':'unit','test_id':'tests/test_app.py::test_greet'}}],'work_items':[{'id':'W1','role':'implementer','description':'implement greet','depends_on':[]}],'constraints':[],'questions':['Which greeting?'] if os.getenv('FAKE_QUESTION') else []}
 if os.getenv('FAKE_UNKNOWN'): result['requirements'][0]['verification']['test_id']='tests/test_app.py::not_executed'
 if os.getenv('FAKE_FIX'): result['requirements'].append({'id':'A2','description':'preserves callable string behavior','verification':{'kind':'test','check':'unit','test_id':'tests/test_app.py::test_type'}})
elif 'AGENTCREW_KIND:implement' in prompt or 'AGENTCREW_KIND:revise' in prompt:
 if os.getenv('FAKE_SLOW'): time.sleep(float(os.environ['FAKE_SLOW']))
 root=pathlib.Path.cwd();(root/'app.py').write_text('def greet():\\n    return "new"\\n');(root/'tests').mkdir(exist_ok=True);(root/'tests/test_app.py').write_text('from app import greet\\ndef test_greet():\\n    assert greet()=="new"\\n')
 if os.getenv('FAKE_REVISE') and 'AGENTCREW_KIND:implement' in prompt: (root/'app.py').write_text('def greet():\\n    return \"bad\"\\n')
 result={'schema_version':1,'summary':'done','changed_files':['app.py'] if 'AGENTCREW_KIND:revise' in prompt else ['app.py','tests/test_app.py'],'addressed_requirements':['A1'],'remaining_requirements':[],'limitations':[]}
 if os.getenv('FAKE_FIX'):
  (root/'tests/test_app.py').write_text((root/'tests/test_app.py').read_text()+'def test_type():\\n    assert isinstance(greet(), str)\\n')
  result['addressed_requirements'].append('A2')
else:
 if os.getenv('FAKE_CRASH_TESTS') and 'AGENTCREW_KIND:tests' in prompt: sys.exit(7)
 result={'schema_version':1,'summary':'reviewed','findings':[],'limitations':[]}
 if os.getenv('FAKE_FINDING') or (os.getenv('FAKE_FIX') and '"new"' not in pathlib.Path('app.py').read_text()): result['findings']=[{'title':'Bad greeting','severity':'P1','file':'app.py','line':2,'detail':'Changed greeting breaks caller','evidence':'Unexpected return','reproduction':'Call greet()','suggestion':'Fix greeting','confidence':.8}]
path=pathlib.Path(args[args.index('--output-last-message')+1]);path.write_text(json.dumps(result))
print(json.dumps({'type':'thread.started','thread_id':'fake-session'}));print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':2}}))
""")
    binary.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", str(bindir) + ":" + os.environ["PATH"])
    return binary


def finish(db, rid):
    tmux = Tmux(db)
    try:
        end = time.monotonic() + 30
        while time.monotonic() < end:
            run = db.run(rid)
            if run["state"] in {"completed", "failed", "needs_attention"}:
                return run
            time.sleep(0.1)
        raise AssertionError("workflow timed out " + str(db.run(rid)))
    finally:
        for seat in db.rows("SELECT * FROM seats WHERE scope_id=?", (rid,)):
            tmux.stop(seat["tmux_session"])
        tmux.stop("supervisor")


def test_develop_freezes_real_code_and_preserves_source(project, tmp_path, fake_codex):
    db = Database(tmp_path / "state")
    spec = tmp_path / "task.md"
    spec.write_text("Change greet to new and test it.")
    config = validate_config(
        {
            "max_develop_seconds": 120,
            "checks": [
                {
                    "name": "unit",
                    "argv": [sys.executable, "-m", "pytest", "-q", "--junitxml=.agentcrew-results/unit.xml"],
                    "timeout_seconds": 10,
                    "result_format": "junit",
                    "result_file": ".agentcrew-results/unit.xml",
                }
            ],
        }
    )
    source = git(project, "rev-parse", "HEAD")
    rid = start_develop(db, project, spec, config)
    result = finish(db, rid)
    assert result["state"] == "completed", json.dumps(db.rows("SELECT * FROM tasks"), indent=2)
    assert result["outcome"] == "accepted"
    assert result["current_candidate_id"]
    assert git(project, "rev-parse", "HEAD") == source
    assert "old" in (project / "app.py").read_text()
    assert len(db.rows("SELECT * FROM tasks WHERE kind IN ('correctness','tests') AND state='done'")) == 2
    assert db.row("SELECT status FROM requirement_results")["status"] == "passed"


def test_review_without_checks_is_partial(project, tmp_path, fake_codex):
    git(project, "switch", "-c", "feature")
    (project / "app.py").write_text('def greet():\n    return "changed"\n')
    git(project, "commit", "-am", "change")
    db = Database(tmp_path / "state")
    config = validate_config({})
    rid = start_review(db, project, "main", config)
    run = finish(db, rid)
    assert run["state"] == "completed"
    assert run["outcome"] == "partial"
    assert len(db.rows("SELECT * FROM tasks WHERE kind IN ('correctness','tests') AND state='done'")) == 2
    assert git(project, "status", "--porcelain") == b""


def develop_config():
    return validate_config(
        {
            "max_develop_seconds": 120,
            "max_revisions": 1,
            "checks": [
                {
                    "name": "unit",
                    "argv": [sys.executable, "-m", "pytest", "-q", "--junitxml=.agentcrew-results/unit.xml"],
                    "timeout_seconds": 10,
                    "result_format": "junit",
                    "result_file": ".agentcrew-results/unit.xml",
                }
            ],
        }
    )


def test_revision_has_fresh_candidate_evidence(project, tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_REVISE", "1")
    spec = tmp_path / "task.md"
    spec.write_text("Implement and test new greeting")
    db = Database(tmp_path / "state")
    rid = start_develop(db, project, spec, develop_config())
    result = finish(db, rid)
    assert result["outcome"] == "accepted"
    candidates = db.rows("SELECT * FROM candidates WHERE run_id=? ORDER BY revision", (rid,))
    assert len(candidates) == 2
    assert candidates[0]["commit_sha"] != candidates[1]["commit_sha"]
    assert (
        db.row("SELECT state FROM check_runs WHERE candidate_id=?", (candidates[0]["id"],))["state"]
        == "failed"
    )
    assert (
        db.row("SELECT state FROM check_runs WHERE candidate_id=?", (candidates[1]["id"],))["state"]
        == "passed"
    )


def test_revision_limit_stops_after_one_extra_attempt(project, tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_FINDING", "1")
    monkeypatch.setenv("FAKE_REVISE", "1")
    spec = tmp_path / "task.md"
    spec.write_text("Change greet to new and test it")
    db = Database(tmp_path / "state")
    rid = start_develop(db, project, spec, develop_config())
    result = finish(db, rid)
    assert result["state"] == "completed" and result["outcome"] == "rejected"
    assert len(db.rows("SELECT * FROM candidates WHERE run_id=?", (rid,))) == 2
    assert len(db.rows("SELECT * FROM tasks WHERE scope_id=? AND kind='revise'", (rid,))) == 1
    assert git(project, "status", "--porcelain") == b""
    db.close()


def test_tmux_server_loss_blocks_writer_without_automatic_replay(project, tmp_path, fake_codex, monkeypatch):
    from agentcrew.supervisor import recover, recovery_plan

    monkeypatch.setenv("FAKE_SLOW", "3")
    spec = tmp_path / "task.md"
    spec.write_text("Change greet to new and test it")
    db = Database(tmp_path / "state")
    rid = start_develop(db, project, spec, develop_config())
    tmux = Tmux(db)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = db.row(
                "SELECT * FROM tasks WHERE scope_id=? AND kind='implement' AND state='claimed'", (rid,)
            )
            if task and list((db.root / "runs" / rid / "tasks" / task["id"]).rglob("process.json")):
                break
            time.sleep(0.05)
        assert task and task["state"] == "claimed"
        tmux.call("kill-server")
        plan = recovery_plan(db, rid)
        assert any(row["id"] == task["id"] and row["writer"] for row in plan["blocked_tasks"])
        recover(db, rid)
        recover(db, rid)
        assert db.scope(rid)["state"] == "needs_attention"
        assert db.row("SELECT state,attempt FROM tasks WHERE id=?", (task["id"],)) == {
            "state": "blocked",
            "attempt": 1,
        }
        assert len(db.rows("SELECT * FROM tasks WHERE scope_id=? AND kind='implement'", (rid,))) == 1
        assert git(project, "status", "--porcelain") == b""
        assert 'return "old"' in (project / "app.py").read_text()
        from agentcrew.processes import active_attempt

        deadline = time.monotonic() + 8
        while active_attempt(db.root / "runs" / rid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not active_attempt(db.root / "runs" / rid)
    finally:
        for seat in db.rows("SELECT * FROM seats WHERE scope_id=?", (rid,)):
            tmux.stop(seat["tmux_session"])
        tmux.stop("supervisor")
        db.close()


def test_unknown_test_cannot_be_accepted(project, tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_UNKNOWN", "1")
    spec = tmp_path / "task.md"
    spec.write_text("Implement and test new greeting")
    db = Database(tmp_path / "state")
    rid = start_develop(db, project, spec, develop_config())
    result = finish(db, rid)
    assert result["outcome"] == "partial"
    assert result["state"] == "needs_attention"
    assert db.row("SELECT status FROM requirement_results")["status"] == "unknown"


def test_failed_reviewer_report_is_partial(project, tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_TESTS", "1")
    git(project, "switch", "-c", "feature")
    (project / "app.py").write_text('def greet():\n    return "changed"\n')
    git(project, "commit", "-am", "change")
    db = Database(tmp_path / "state")
    rid = start_review(db, project, "main", validate_config({}))
    result = finish(db, rid)
    assert result["outcome"] == "partial"
    from agentcrew.review.report import build_report

    report = build_report(db, rid)
    failed = next(a for a in report["agents"] if a["role"] == "tests")
    assert failed["valid"] is False
    assert failed["findings_count"] is None


def test_two_reviewers_corroborate_without_reproduction(project, tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_FINDING", "1")
    git(project, "switch", "-c", "feature")
    (project / "app.py").write_text('def greet():\n    return "changed"\n')
    git(project, "commit", "-am", "change")
    db = Database(tmp_path / "state")
    rid = start_review(db, project, "main", validate_config({}))
    finish(db, rid)
    findings = db.rows("SELECT * FROM findings WHERE run_id=?", (rid,))
    assert len(findings) == 1
    assert findings[0]["status"] == "corroborated"


def test_fix_child_rechecks_new_sha_before_marking_original_fixed(project, tmp_path, fake_codex, monkeypatch):
    from agentcrew.git_snapshot import apply_candidate
    from agentcrew.review.fix import start_fix

    monkeypatch.setenv("FAKE_FIX", "1")
    git(project, "switch", "-c", "feature")
    (project / "app.py").write_text('def greet():\n    return "broken"\n')
    git(project, "commit", "-am", "defect")
    source = git(project, "rev-parse", "HEAD").decode().strip()
    db = Database(tmp_path / "state")
    parent_id = start_review(db, project, "main", develop_config())
    parent = finish(db, parent_id)
    finding = db.row("SELECT * FROM findings WHERE run_id=?", (parent_id,))
    original = db.row("SELECT * FROM candidates WHERE id=?", (parent["current_candidate_id"],))
    assert finding["status"] == "corroborated"
    fix_id = start_fix(db, parent_id, "F-001")
    fixed = finish(db, fix_id)
    assert fixed["outcome"] == "accepted", json.dumps(
        db.rows("SELECT * FROM tasks WHERE scope_id=?", (fix_id,)), indent=2
    )
    assert fixed["parent_run_id"] == parent_id
    assert fixed["base_sha"] == original["commit_sha"]
    assert fixed["source_head_sha"] == source
    candidate = db.row("SELECT * FROM candidates WHERE id=?", (fixed["current_candidate_id"],))
    assert candidate["commit_sha"] != original["commit_sha"]
    assert candidate["branch_name"].startswith("agentcrew/fix/")
    assert db.row("SELECT status FROM findings WHERE id=?", (finding["id"],))["status"] == "fixed"
    assert (
        db.row(
            "SELECT count(*) AS n FROM requirement_results WHERE candidate_id=? AND status='passed'",
            (candidate["id"],),
        )["n"]
        == 2
    )
    assert (
        len(
            db.rows(
                "SELECT * FROM tasks WHERE scope_id=? AND kind IN ('correctness','tests') AND state='done'",
                (fix_id,),
            )
        )
        == 2
    )
    assert len(db.rows("SELECT * FROM candidates WHERE run_id=?", (fix_id,))) == 1
    assert git(project, "rev-parse", "HEAD").decode().strip() == source
    assert git(project, "status", "--porcelain") == b""
    apply_candidate(db, fix_id, dry_run=False)
    assert 'return "new"' in (project / "app.py").read_text()
    assert git(project, "rev-parse", "HEAD").decode().strip() == source
    assert (
        subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=project, capture_output=True).returncode
        == 0
    )
    db.close()


def test_answer_is_durable_and_does_not_create_second_writer(project, tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_QUESTION", "1")
    spec = tmp_path / "task.md"
    spec.write_text("Change greeting and test it")
    db = Database(tmp_path / "state")
    rid = start_develop(db, project, spec, develop_config())
    deadline = time.monotonic() + 10
    while db.run(rid)["state"] != "needs_attention" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert db.run(rid)["state"] == "needs_attention"
    from agentcrew.develop.workflow import answer

    one = answer(db, rid, "Use the greeting new.")
    two = answer(db, rid, "Use the greeting new.")
    assert one["answer_id"] == two["answer_id"]
    result = finish(db, rid)
    assert result["outcome"] == "accepted"
    assert db.row("SELECT count(*) AS n FROM tasks WHERE scope_id=? AND kind='implement'", (rid,))["n"] == 1


def test_pause_allows_current_writer_and_cancel_preserves_partial(project, tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_SLOW", "2")
    spec = tmp_path / "task.md"
    spec.write_text("Change greeting and test it")
    db = Database(tmp_path / "state")
    rid = start_develop(db, project, spec, develop_config())
    from agentcrew.supervisor import control

    end = time.monotonic() + 10
    while time.monotonic() < end:
        task = db.row("SELECT * FROM tasks WHERE scope_id=? AND kind='implement' AND state='claimed'", (rid,))
        if task:
            break
        time.sleep(0.05)
    assert task
    control(db, rid, "pause")
    end = time.monotonic() + 10
    while (
        time.monotonic() < end
        and db.row("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"] == "claimed"
    ):
        time.sleep(0.05)
    assert db.row("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"] == "done"
    assert not db.rows("SELECT * FROM tasks WHERE scope_id=? AND kind='correctness'", (rid,))
    control(db, rid, "cancel")
    seat = db.row("SELECT * FROM seats WHERE id=?", (task["owner_seat"],))
    body = json.loads(task["body_json"])
    assert "new" in (Path(body["cwd"]) / "app.py").read_text()
    assert db.scope(rid)["state"] == "canceled"
    assert "old" in (project / "app.py").read_text()
    tmux = Tmux(db)
    for seat in db.rows("SELECT * FROM seats WHERE scope_id=?", (rid,)):
        tmux.stop(seat["tmux_session"])
    tmux.stop("supervisor")
