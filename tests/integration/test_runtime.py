import time

from agentcrew.db import Database
from agentcrew.queue import Queue
from agentcrew.supervisor import ensure_supervisor, recover, recovery_plan
from agentcrew.tmux_backend import Tmux


def test_worker_lifecycle_and_crash_is_not_replayed(tmp_path):
    db = Database(tmp_path / "state")
    rid = db.create_scope("team", str(tmp_path))
    db.execute("INSERT INTO teams VALUES(?,?,?)", (rid, "pair", "{}"))
    seat = db.add_seat(rid, "dev-owner", "codex", str(tmp_path))
    db.set_state("scopes", rid, "running")
    tmux = Tmux(db)
    try:
        ensure_supervisor(db)
        ensure_supervisor(db)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            worker = db.row("SELECT * FROM seats WHERE id=?", (seat,))
            if worker["state"] == "ready":
                break
            time.sleep(0.05)
        assert worker["state"] == "ready"
        task = Queue(db).create(rid, seat, "implement", {"prompt": "pending"})
        # A manual claim models an interrupted write, no provider needed at this stage.
        with db.transaction():
            db.set_state("scopes", rid, "paused")
            db.execute(
                "UPDATE tasks SET state='claimed',lease_owner='lost',lease_until='2000-01-01T00:00:00Z',attempt=1 WHERE id=?",
                (task,),
            )
        tmux.stop(worker["tmux_session"])
        plan = recovery_plan(db, rid)
        assert any(t["id"] == task for t in plan["blocked_tasks"])
        recover(db, rid)
        recover(db, rid)
        assert db.row("SELECT state,attempt FROM tasks WHERE id=?", (task,)) == {
            "state": "blocked",
            "attempt": 1,
        }
        assert db.scope(rid)["state"] == "needs_attention"
    finally:
        tmux.stop(worker["tmux_session"])
        tmux.stop("supervisor")


def test_cancel_during_writer_does_not_resurrect_scope(tmp_path, monkeypatch):
    from agentcrew import worker
    from agentcrew.models import UserError
    from agentcrew.supervisor import control

    db = Database(tmp_path / "state")
    rid = db.create_scope("team", str(tmp_path))
    db.execute("INSERT INTO teams VALUES(?,?,?)", (rid, "pair", "{}"))
    seat = db.add_seat(rid, "dev-owner", "codex", str(tmp_path))
    db.set_state("scopes", rid, "running")
    task = Queue(db).create(rid, seat, "team.implement", {})

    def interrupted(db, seat, task, heartbeat, canceled):
        control(db, rid, "cancel")
        raise UserError("Canceled", "provider_canceled")

    monkeypatch.setattr(worker, "execute", interrupted)

    def stop_loop(_):
        raise RuntimeError("loop stop")

    monkeypatch.setattr(worker.time, "sleep", stop_loop)
    try:
        worker.main(["--state", str(db.root), "--seat", seat])
    except RuntimeError as exc:
        assert str(exc) == "loop stop"
    assert db.scope(rid)["state"] == "canceled"
    assert db.row("SELECT state FROM tasks WHERE id=?", (task,))["state"] == "canceled"
