"""Singleton durable controller. Terminal clients own no running workflow."""

import argparse
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .db import Database
from .models import UserError, redact, uid
from .queue import WRITING_KINDS, Queue, future
from .tmux_backend import Tmux


def lock(db, name: str, owner: str) -> bool:
    with db.transaction():
        old = db.row("SELECT * FROM locks WHERE name=?", (name,))
        if old and old["owner"] != owner and datetime.fromisoformat(old["expires_at"]) > datetime.now(UTC):
            return False
        db.execute(
            "INSERT INTO locks VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
            (name, owner, future()),
        )
    return True


def ensure_supervisor(db):
    tmux = Tmux(db)
    if not tmux.alive("supervisor"):
        tmux.stop("supervisor")
        with db.transaction():
            db.execute("DELETE FROM locks WHERE name='supervisor'")
        tmux.ensure(
            "supervisor", [sys.executable, "-m", "agentcrew.supervisor", "--state", str(db.root)], db.root
        )


def ensure_worker(db, seat: dict):
    tmux = Tmux(db)
    if tmux.alive(seat["tmux_session"]):
        return
    if seat["state"] in {"busy", "crashed"}:
        return  # A vanished writer needs recovery/inspection, never an implicit relaunch.
    if not Path(seat["worktree_path"]).is_dir():
        db.set_state("scopes", seat["scope_id"], "needs_attention", payload={"reason": "Worktree missing"})
        return
    tmux.stop(seat["tmux_session"])
    with db.transaction():
        db.execute("DELETE FROM locks WHERE name=?", ("worker:" + seat["id"],))
        db.set_state("seats", seat["id"], "starting")
    pane = tmux.ensure(
        seat["tmux_session"],
        [sys.executable, "-m", "agentcrew.worker", "--state", str(db.root), "--seat", seat["id"]],
        Path(seat["worktree_path"]),
    )
    with db.transaction():
        db.execute("UPDATE seats SET pane_id=? WHERE id=?", (pane, seat["id"]))
        db.event(seat["scope_id"], "worker.started", {"seat_id": seat["id"], "pane_id": pane})


def recovery_plan(db, scope_id: str | None = None) -> dict:
    tmux = Tmux(db)
    scopes = [db.scope(scope_id)] if scope_id else db.rows("SELECT * FROM scopes")
    result = {
        "scopes": [],
        "sessions": [],
        "blocked_tasks": [],
        "worktrees": [],
        "actions": [],
        "provider_processes": [],
    }
    for scope in scopes:
        sid = scope["id"]
        result["scopes"].append(scope)
        seats = db.rows("SELECT * FROM seats WHERE scope_id=?", (sid,))
        for seat in seats:
            alive = tmux.alive(seat["tmux_session"])
            result["sessions"].append({"seat_id": seat["id"], "alive": alive, "state": seat["state"]})
            for task in db.rows("SELECT * FROM tasks WHERE owner_seat=? AND state='claimed'", (seat["id"],)):
                from .processes import active_attempt

                live_provider = active_attempt(db.root / "runs" / sid / "tasks" / task["id"])
                result["provider_processes"].extend(live_provider)
                expired = not task["lease_until"] or datetime.fromisoformat(
                    task["lease_until"]
                ) < datetime.now(UTC)
                if not alive or expired:
                    result["blocked_tasks"].append(
                        {
                            "id": task["id"],
                            "seat_id": seat["id"],
                            "scope_id": sid,
                            "kind": task["kind"],
                            "writer": task["kind"] in WRITING_KINDS,
                            "worktree": seat["worktree_path"],
                            "reason": "dead worker" if not alive else "expired lease",
                            "process_unverified": alive,
                            "live_provider_processes": live_provider,
                        }
                    )
            if not alive and scope["state"] in {"running", "paused"}:
                result["actions"].append(
                    {"scope_id": sid, "seat_id": seat["id"], "action": "restart only if no blocked writer"}
                )
        for wt in db.rows("SELECT * FROM worktrees WHERE scope_id=?", (sid,)):
            result["worktrees"].append({**wt, "exists": Path(wt["path"]).is_dir()})
        run = db.row("SELECT phase,outcome,current_candidate_id FROM runs WHERE id=?", (sid,))
        if run:
            scope["report"] = run
    return result


def recover(db, scope_id: str | None = None) -> dict:
    plan = recovery_plan(db, scope_id)
    for task in plan["blocked_tasks"]:
        with db.transaction():
            Queue(db).block(
                task["id"],
                task["reason"],
                f"Inspect {task['worktree']}; stop old worker then queue retry {task['id']}"
                + (" --inspect-writer" if task["writer"] else ""),
            )
            db.set_state("seats", task["seat_id"], "crashed")
    for wt in plan["worktrees"]:
        if wt["state"] == "creating":
            # Preserve it for ownership reconciliation, never remove an uncertain path.
            db.set_state(
                "scopes",
                wt["scope_id"],
                "needs_attention",
                payload={"reason": "Interrupted worktree creation", "path": wt["path"]},
            )
    for scope in plan["scopes"]:
        current = db.scope(scope["id"])
        if current["state"] in {"running", "paused"}:
            for seat in db.rows("SELECT * FROM seats WHERE scope_id=?", (scope["id"],)):
                if seat["state"] != "crashed":
                    ensure_worker(db, seat)
    if any(db.scope(s["id"])["state"] in {"running", "paused"} for s in plan["scopes"]):
        ensure_supervisor(db)
    return recovery_plan(db, scope_id)


def control(db, scope_id: str, action: str):
    scope = db.scope(scope_id)
    if scope["state"] in {"completed", "failed", "canceled"}:
        raise UserError("Workflow is terminal; inspect report or create a new run")
    if action == "pause":
        db.set_state("scopes", scope_id, "paused")
    elif action == "resume":
        if scope["state"] != "paused":
            raise UserError("Resume requires paused state; use recover --plan after a crash")
        db.set_state("scopes", scope_id, "running")
        ensure_supervisor(db)
    elif action == "cancel":
        with db.transaction():
            db.set_state("scopes", scope_id, "canceled")
            for task in db.rows(
                "SELECT id FROM tasks WHERE scope_id=? AND state IN ('pending','claimed','blocked')",
                (scope_id,),
            ):
                db.set_state("tasks", task["id"], "canceled")
            db.execute("UPDATE runs SET outcome='partial' WHERE id=?", (scope_id,))
        # Workers observe canceled and terminate their own live Popen group.
    else:
        raise UserError("Unknown control action")


def tick(db):
    tmux = Tmux(db)
    for scope in db.rows("SELECT * FROM scopes WHERE state IN ('running','paused')"):
        for seat in db.rows("SELECT * FROM seats WHERE scope_id=?", (scope["id"],)):
            if seat["state"] == "busy" and not tmux.alive(seat["tmux_session"]):
                for task in db.rows(
                    "SELECT id FROM tasks WHERE owner_seat=? AND state='claimed'", (seat["id"],)
                ):
                    Queue(db).block(
                        task["id"],
                        "Worker died during task",
                        "recover --plan, inspect worktree, then queue retry",
                    )
                db.set_state("seats", seat["id"], "crashed")
            elif scope["state"] == "running":
                ensure_worker(db, seat)
        if scope["kind"] == "run" and db.scope(scope["id"])["state"] == "running":
            from .review.workflow import advance

            advance(db, scope["id"])
        elif scope["kind"] == "team" and db.scope(scope["id"])["state"] == "running":
            from .teams import advance

            advance(db, scope["id"])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    db = Database(args.state)
    instance = uid()
    if not lock(db, "supervisor", instance):
        return 0
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("AgentCrew supervisor " + instance, flush=True)
    try:
        while not stopping:
            if not lock(db, "supervisor", instance):
                break
            try:
                tick(db)
            except (UserError, OSError) as exc:
                print(redact(str(exc)), flush=True)
            time.sleep(0.25)
    finally:
        db.execute("DELETE FROM locks WHERE name='supervisor' AND owner=?", (instance,))
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
