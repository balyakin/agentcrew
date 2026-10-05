"""Opt-in paid acceptance. No fake provider or manually inserted model finding."""

import json
import os
import time
from pathlib import Path

import pytest
from test_csv import hidden_behaviors
from test_workflows import git

from agentcrew.config import load_config
from agentcrew.db import Database
from agentcrew.develop.scaffold import scaffold
from agentcrew.develop.workflow import start_develop
from agentcrew.git_snapshot import apply_candidate
from agentcrew.review.report import build_report
from agentcrew.review.workflow import start_review
from agentcrew.supervisor import control
from agentcrew.tmux_backend import Tmux

pytestmark = pytest.mark.real_provider
SPEC = Path(__file__).resolve().parents[2] / "examples" / "task.md"


@pytest.fixture(params=["codex", "claude", "mixed"])
def provider(request):
    selected = set(os.environ.get("AGENTCREW_REAL_PROVIDERS", "").split(","))
    required = {"codex", "claude"} if request.param == "mixed" else {request.param}
    if not required <= selected:
        pytest.skip("Paid calls require explicit AGENTCREW_REAL_PROVIDERS=codex,claude opt-in")
    return request.param


def configuration(project, provider):
    config = load_config(project, provider="codex" if provider == "mixed" else provider)
    if provider == "mixed":
        config["roles"]["planner"]["provider"] = "claude"
        config["roles"]["tests"]["provider"] = "claude"
    return config


def wait_and_record(db, run_id, directory):
    tmux = Tmux(db)
    try:
        deadline = time.monotonic() + 1850
        while time.monotonic() < deadline:
            run = db.run(run_id)
            if run["state"] in {"completed", "failed", "needs_attention", "canceled"}:
                return run
            time.sleep(0.5)
        raise AssertionError("Real workflow exceeded its controller budget")
    finally:
        run = db.run(run_id)
        if run["state"] in {"running", "paused", "needs_attention"}:
            control(db, run_id, "cancel")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{run_id}.json").write_text(
            json.dumps(build_report(db, run_id), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        from agentcrew.processes import active_attempt

        stop_deadline = time.monotonic() + 8
        while active_attempt(db.root / "runs" / run_id) and time.monotonic() < stop_deadline:
            time.sleep(0.2)
        for seat in db.rows("SELECT * FROM seats WHERE scope_id=?", (run_id,)):
            tmux.stop(seat["tmux_session"])
        tmux.stop("supervisor")


@pytest.mark.parametrize("template", ["python-cli", "typescript-cli"])
def test_real_csv_develop_apply_and_hidden_behavior(tmp_path, provider, template):
    project = scaffold(tmp_path / "orders", template, SPEC)
    source = git(project, "rev-parse", "HEAD")
    with_db = Database(tmp_path / "state")
    try:
        run_id = start_develop(with_db, project, SPEC, configuration(project, provider))
        result = wait_and_record(with_db, run_id, tmp_path / "evidence")
        assert git(project, "rev-parse", "HEAD") == source
        assert git(project, "status", "--porcelain") == b""
        assert result["outcome"] == "accepted", str(tmp_path / "evidence")
        apply_candidate(with_db, run_id, dry_run=False)
        hidden_behaviors(project, template, tmp_path)
        assert git(project, "rev-parse", "HEAD") == source
    finally:
        with_db.close()


def test_real_review_detects_preregistered_zero_division(tmp_path, provider):
    project = scaffold(tmp_path / "review", "python-cli", SPEC)
    (project / "app.py").write_text("def ratio(total, count):\n    return total / count if count else 0\n")
    (project / "tests/test_ratio.py").write_text(
        "from app import ratio\ndef test_empty_count():\n    assert ratio(10, 0) == 0\n"
    )
    git(project, "add", ".")
    git(project, "commit", "-m", "safe baseline")
    git(project, "branch", "baseline")
    (project / "app.py").write_text("def ratio(total, count):\n    return total / count\n")
    git(project, "commit", "-am", "preregistered defect")
    source = git(project, "rev-parse", "HEAD")
    db = Database(tmp_path / "state")
    try:
        run_id = start_review(db, project, "baseline", configuration(project, provider))
        result = wait_and_record(db, run_id, tmp_path / "evidence")
        assert git(project, "rev-parse", "HEAD") == source
        assert git(project, "status", "--porcelain") == b""
        assert result["outcome"] == "rejected", str(tmp_path / "evidence")
        findings = db.rows("SELECT * FROM findings WHERE run_id=?", (run_id,))
        assert any(f["file_path"] == "app.py" and f["line"] == 2 for f in findings), (
            "Model missed planted defect"
        )
    finally:
        db.close()
