import subprocess

import pytest

from agentcrew.db import Database
from agentcrew.git_snapshot import create_worktree
from agentcrew.models import CapabilityReport, ProviderResult, canonical
from agentcrew.queue import Queue
from agentcrew.review.schema import REVIEW_SCHEMA
from agentcrew.worker import execute


@pytest.mark.parametrize("supports_resume", [True, False])
def test_worker_resumes_real_saved_id_or_documents_fresh_context(tmp_path, monkeypatch, supports_resume):
    from agentcrew.providers import base

    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args):
        return (
            subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
            .stdout.decode()
            .strip()
        )

    git("init", "-b", "main")
    (repo / "app.py").write_text("print('hello')\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "baseline")
    sha = git("rev-parse", "HEAD")
    db = Database(tmp_path / "state")
    scope = db.create_scope("team", str(repo))
    db.execute("INSERT INTO teams VALUES(?,?,?)", (scope, "review", "{}"))
    seat_id = db.add_seat(scope, "dev-check", "codex", "")
    root = create_worktree(db, scope, sha, "review-seat", seat_id=seat_id)
    db.execute(
        "UPDATE seats SET worktree_path=?,worker_instance=?,provider_session_id=? WHERE id=?",
        (str(root), "worker", "native-recorded-session", seat_id),
    )
    db.set_state("scopes", scope, "running")
    queue = Queue(db)
    previous = queue.create(scope, seat_id, "team.review", {})
    db.execute(
        "UPDATE tasks SET state='done',result_json=? WHERE id=?",
        (canonical({"data": {"summary": "Prior reviewed artifact"}}), previous),
    )
    queue.create(
        scope,
        seat_id,
        "team.review",
        {
            "cwd": str(root),
            "candidate_sha": sha,
            "prompt": "Review this current candidate",
            "schema": REVIEW_SCHEMA,
            "access": "read-only",
        },
    )
    requests = []

    class Adapter:
        def resume_capability(self):
            return supports_resume

        def run(self, request, *args, **kwargs):
            requests.append(request)
            return ProviderResult(
                {"schema_version": 1, "summary": "Reviewed", "findings": [], "limitations": []},
                session_id="next-native-session",
            )

        def probe(self):
            return CapabilityReport("codex", True, version="test")

    monkeypatch.setattr(base, "get_adapter", lambda *args: Adapter())
    task = queue.claim(seat_id, "worker")
    execute(db, db.row("SELECT * FROM seats WHERE id=?", (seat_id,)), task, lambda: True, lambda: False)
    if supports_resume:
        assert requests[0]["session_id"] == "native-recorded-session"
    else:
        assert not requests[0].get("session_id")
        assert "Prior reviewed artifact" in requests[0]["prompt"]
        assert db.row("SELECT seq FROM events WHERE kind='session_restarted'")
    assert (
        db.row("SELECT provider_session_id FROM seats WHERE id=?", (seat_id,))["provider_session_id"]
        == "next-native-session"
    )
    db.close()
