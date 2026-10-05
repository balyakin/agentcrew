import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentcrew.db import Database
from agentcrew.models import UserError, uid
from agentcrew.queue import Queue


def scope(db, repo="/tmp/repo"):
    rid = db.create_scope("run", repo)
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) VALUES(?,?,?,?,?,?)",
        (rid, "develop", "a" * 40, "a" * 40, "plan", "{}"),
    )
    db.add_seat(rid, "implementer", "codex", "/tmp/work")
    db.set_state("scopes", rid, "running")
    return rid, "implementer@" + rid


def test_claim_race_and_stale_completion(tmp_path):
    path = tmp_path / "state"
    db = Database(path)
    rid, seat = scope(db)
    q = Queue(db)
    task = q.create(rid, seat, "implement", {"prompt": "hello"})

    def claim(instance):
        other = Database(path)
        try:
            return Queue(other).claim(seat, instance)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["one", "two"]))
    won = [r for r in results if r]
    assert len(won) == 1
    assert db.row("SELECT state,attempt FROM tasks WHERE id=?", (task,)) == {"state": "claimed", "attempt": 1}
    assert not q.finish(task, "wrong", 1, {"summary": "late"})
    assert q.finish(task, won[0]["lease_owner"], 1, {"summary": "done"})
    assert not q.finish(task, won[0]["lease_owner"], 1, {"summary": "overwrite"})
    assert json.loads(db.row("SELECT result_json FROM tasks WHERE id=?", (task,))["result_json"]) == {
        "summary": "done"
    }
    assert db.row("SELECT count(*) AS n FROM events WHERE kind='task.claimed'")["n"] == 1


def test_scope_fence_and_paused_claim(tmp_path):
    db = Database(tmp_path / "state")
    r1, s1 = scope(db)
    r2, s2 = scope(db)
    q = Queue(db)
    with pytest.raises(UserError):
        q.create(r1, s2, "implement", {})
    q.create(r1, s1, "implement", {})
    db.set_state("scopes", r1, "paused")
    assert q.claim(s1, "instance") is None
    with pytest.raises(UserError):
        q.send(r1, "user", s2, "hello", "key")


def test_dependencies_and_message_idempotency(tmp_path):
    db = Database(tmp_path / "state")
    rid, seat = scope(db)
    q = Queue(db)
    a = q.create(rid, seat, "plan", {})
    b = q.create(rid, seat, "implement", {}, dependencies=[a])
    with pytest.raises(UserError):
        q.add_dependency(a, b)
    first = q.claim(seat, "one")
    assert first["id"] == a
    assert q.claim(seat, "two") is None
    q.finish(a, "one", 1, {"ok": True})
    assert q.claim(seat, "two")["id"] == b
    msg = q.send(rid, "user", seat, "hello", "key")
    assert q.send(rid, "user", seat, "hello", "key") == msg
    assert db.row("SELECT count(*) AS n FROM messages")["n"] == 1


def test_immutable_candidates_and_evidence(tmp_path):
    db = Database(tmp_path / "state")
    rid, seat = scope(db)
    one = db.add_candidate(rid, "a" * 40, "hash1", None)
    two = db.add_candidate(rid, "b" * 40, "hash2", None, parent_id=one)
    with pytest.raises(Exception):
        db.execute("UPDATE candidates SET commit_sha=? WHERE id=?", ("c" * 40, one))
    assert db.row("SELECT current_candidate_id FROM runs WHERE id=?", (rid,))["current_candidate_id"] == two
    r2, s2 = scope(db)
    req = uid()
    db.execute("INSERT INTO requirements VALUES(?,?,?,?,?)", (req, rid, "A1", "sample", "{}"))
    other = db.add_candidate(r2, "d" * 40, "hash", None)
    with pytest.raises(Exception):
        db.execute("INSERT INTO requirement_results VALUES(?,?,?,?)", (req, other, "passed", "{}"))
    assert Path(db.path).stat().st_mode & 0o777 == 0o600


def test_result_for_previous_candidate_is_fenced(tmp_path):
    db = Database(tmp_path / "state")
    rid, seat = scope(db)
    first = db.add_candidate(rid, "a" * 40, "hash", None)
    q = Queue(db)
    tid = q.create(rid, seat, "correctness", {}, candidate_id=first)
    claimed = q.claim(seat, "worker")
    db.add_candidate(rid, "b" * 40, "new", None, parent_id=first)
    assert not q.finish(tid, "worker", claimed["attempt"], {"data": {}}, candidate_id=first)
