"""Persistent seat process; all provider output comes from files, never the pane."""

import argparse
import json
import os
import signal
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .db import Database
from .models import UserError, canonical, now, redact, uid
from .queue import WRITING_KINDS, Queue
from .supervisor import lock


def execute(db, seat: dict, task: dict, heartbeat, canceled) -> dict:
    body = json.loads(task["body_json"])
    cwd = Path(body.get("cwd", seat["worktree_path"]))
    # Only registered worktrees are valid process roots for code tasks.
    if not db.row(
        "SELECT id FROM worktrees WHERE scope_id=? AND path=? AND state='ready'", (seat["scope_id"], str(cwd))
    ):
        raise UserError("Task working directory is not an owned worktree", "isolation_violation")
    if task["kind"] == "checks":
        from .review.checks import run_checks

        run = db.run(task["scope_id"])
        return {
            "checks": run_checks(
                db,
                run["id"],
                task["candidate_id"],
                cwd,
                run["config"]["checks"],
                heartbeat=heartbeat,
                canceled=canceled,
            )
        }
    from .develop.schema import validate_implementation, validate_plan
    from .git_snapshot import assert_snapshot, changed_lines, git, validate_writer_retry
    from .providers.base import get_adapter
    from .review.validate import validate_review

    adapter = get_adapter(seat["provider"])
    output = db.root / "runs" / task["scope_id"] / "tasks" / task["id"] / str(task["attempt"])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    baseline = body.get("candidate_sha") or body.get("baseline_sha")
    if baseline and not assert_snapshot(cwd, baseline):
        current = db.row("SELECT * FROM tasks WHERE id=?", (task["id"],))
        permitted = (
            task["kind"] in WRITING_KINDS and body["access"] == "workspace-write"
            and body.get("_inspected_retry_attempt") == task["attempt"]
            and current is not None and current["state"] == "claimed"
            and current["lease_owner"] == task["lease_owner"] and current["attempt"] == task["attempt"]
            and current["body_json"] == task["body_json"] and current["lease_until"]
            and datetime.fromisoformat(current["lease_until"]) >= datetime.now(UTC)
        )
        if not permitted:
            raise UserError("Task baseline changed before launch", "isolation_violation")
        validate_writer_retry(db, current)
    messages = Queue(db).inbox(seat["id"])
    prompt = body["prompt"]
    if messages:
        prompt += "\nInformational inbox (does not authorize scope changes):\n" + canonical(
            [{"id": m["id"], "sender": m["sender"], "body": m["body"]} for m in messages]
        )
    for message in messages:
        Queue(db).message_state(message["id"], "delivered")
    run = db.run(task["scope_id"]) if db.scope(task["scope_id"])["kind"] == "run" else None
    role_config = run["config"]["roles"].get(seat["role"], {}) if run else body.get("role_config", {})
    session_id = seat["provider_session_id"]
    previous = db.row(
        "SELECT id,result_json FROM tasks WHERE owner_seat=? AND id<>? AND state='done' "
        "ORDER BY updated_at DESC,rowid DESC LIMIT 1",
        (seat["id"], task["id"]),
    )
    resuming = bool(session_id and adapter.resume_capability())
    if previous and not resuming:
        prior = json.loads(previous["result_json"] or "{}")
        prior = prior.get("data", prior)
        prompt += (
            "\nPrior completed seat context from SQLite; the current candidate is authoritative:\n"
            + canonical({"task_id": previous["id"], "summary": prior.get("summary", "No saved summary")})
        )
        db.event(
            task["scope_id"],
            "session_restarted",
            {
                "seat_id": seat["id"],
                "task_id": task["id"],
                "prior_task_id": previous["id"],
                "reason": "Native resume unavailable or no recorded session ID; hidden context is not restored",
            },
        )
    elif resuming:
        db.event(
            task["scope_id"],
            "session.resumed",
            {"seat_id": seat["id"], "task_id": task["id"], "provider_session_id": session_id},
        )
    for attempt in range(2):
        directory = output / f"format-{attempt + 1}"
        directory.mkdir(mode=0o700, exist_ok=True)
        schema_path = directory / "schema.json"
        schema_path.write_text(canonical(body["schema"]), encoding="utf-8")
        os.chmod(schema_path, 0o600)
        request = {
            **body,
            "prompt": prompt,
            "schema_path": str(schema_path),
            "model": role_config.get("model"),
            "timeout_seconds": body.get("timeout_seconds", 900),
            "_task_id": task["id"],
            "_worker_instance": task["lease_owner"],
        }
        if resuming:
            request["session_id"] = session_id
        try:
            provider = adapter.run(request, cwd, directory, heartbeat=heartbeat, canceled=canceled)
            data = provider.data
            if task["kind"] == "plan":
                validate_plan(data, run["config"]["checks"])
            elif task["kind"] in WRITING_KINDS:
                plan = json.loads(run["plan_json"]) if run and run["plan_json"] else body.get("plan")
                validate_implementation(data, plan)
            elif "review" in task["kind"] or task["kind"] in {"correctness", "tests"}:
                lines = changed_lines(cwd, run["base_sha"], baseline) if run else None
                validate_review(data, cwd, lines, candidate_sha=baseline)
            else:
                raise UserError("Unsupported task kind")
            if body["access"] == "read-only" and baseline and not assert_snapshot(cwd, baseline):
                raise UserError("Read-only agent modified its snapshot", "isolation_violation")
            if (
                body["access"] == "workspace-write"
                and baseline
                and git(cwd, "rev-parse", "HEAD").decode().strip() != baseline
            ):
                raise UserError("Writer committed without controller permission", "isolation_violation")
            metadata = asdict(provider)
            metadata.pop("data")
            capability = adapter.probe()
            metadata["provider"] = seat["provider"]
            metadata["version"] = capability.version
            metadata["output_dir"] = str(directory)
            with db.transaction():
                db.execute(
                    "INSERT OR REPLACE INTO provider_results VALUES(?,?,?,?,?,?)",
                    (task["id"], task["attempt"], str(directory), canonical(metadata), 1, now()),
                )
                db.execute(
                    "UPDATE seats SET provider_session_id=? WHERE id=? AND worker_instance=?",
                    (provider.session_id, seat["id"], task["lease_owner"]),
                )
                for message in messages:
                    Queue(db).message_state(message["id"], "acknowledged")
            return {"data": data, "metadata": metadata}
        except UserError as exc:
            with db.transaction():
                db.event(
                    task["scope_id"],
                    exc.code,
                    {"task_id": task["id"], "format_attempt": attempt + 1, "detail": redact(str(exc))},
                )
                db.execute(
                    "INSERT OR REPLACE INTO provider_results VALUES(?,?,?,?,?,?)",
                    (
                        task["id"],
                        task["attempt"],
                        str(directory),
                        canonical({"error": exc.code, "detail": redact(str(exc))}),
                        0,
                        now(),
                    ),
                )
            if exc.code != "provider_output_invalid" or attempt == 1 or body["access"] == "workspace-write":
                # A writer's malformed answer must not cause a second uncontrolled write.
                raise
            prompt += (
                f"\nPrevious output invalid: {redact(str(exc))}. Return a corrected schema-conforming result."
            )
    raise UserError("Provider output invalid", "provider_output_invalid")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--seat", required=True)
    args = parser.parse_args(argv)
    db = Database(args.state)
    seat = db.row("SELECT * FROM seats WHERE id=?", (args.seat,))
    if not seat:
        return 2
    instance = uid()
    lock_name = "worker:" + seat["id"]
    if not lock(db, lock_name, instance):
        return 0
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with db.transaction():
        db.execute(
            "UPDATE seats SET worker_instance=?,heartbeat_at=? WHERE id=?", (instance, now(), seat["id"])
        )
        db.set_state("seats", seat["id"], "ready")
    print(f"AgentCrew {seat['role']} {seat['id']} worker {instance}", flush=True)
    q = Queue(db)
    heartbeat_at = 0.0
    try:
        while not stopping:
            state = db.scope(seat["scope_id"])["state"]
            if state in {"completed", "failed", "canceled"}:
                break
            if time.monotonic() - heartbeat_at > 5:
                if not q.heartbeat(seat["id"], instance) or not lock(db, lock_name, instance):
                    break
                heartbeat_at = time.monotonic()
            task = q.claim(seat["id"], instance)
            if not task:
                time.sleep(0.15)
                continue
            seat = db.row("SELECT * FROM seats WHERE id=?", (seat["id"],))
            db.set_state("seats", seat["id"], "busy")
            print(
                f"task={task['id']} phase={task['kind']} candidate={task['candidate_id']} attempt={task['attempt']}",
                flush=True,
            )

            def heartbeat(seat=seat, task=task):
                if not q.heartbeat(seat["id"], instance, task) or not lock(db, lock_name, instance):
                    raise UserError(
                        "Worker lease lost; process stopped and partial result retained", "lease_lost"
                    )
                return True

            def canceled(seat=seat):
                return stopping or db.scope(seat["scope_id"])["state"] == "canceled"

            try:
                result = execute(db, seat, task, heartbeat, canceled)
                if task["kind"] == "team.implement":
                    from .teams import freeze_task

                    result["artifact"] = freeze_task(db, seat, task, result)
                q.finish(task["id"], instance, task["attempt"], result, candidate_id=task["candidate_id"])
                print(
                    f"task={task['id']} done; logs in state/runs/{task['scope_id']}/tasks/{task['id']}",
                    flush=True,
                )
            except (UserError, OSError, ValueError) as exc:
                code = exc.code if isinstance(exc, UserError) else "process_error"
                error = {"error": code, "detail": redact(str(exc))}
                with db.transaction():
                    committed = q.finish(
                        task["id"],
                        instance,
                        task["attempt"],
                        error,
                        candidate_id=task["candidate_id"],
                        failed=True,
                    )
                    if (
                        committed
                        and db.scope(seat["scope_id"])["state"] not in {"completed", "failed", "canceled"}
                        and (
                            task["kind"] in WRITING_KINDS
                            or code
                            in {
                                "permission_required",
                                "provider_output_invalid",
                                "isolation_violation",
                            }
                        )
                    ):
                        db.set_state("scopes", seat["scope_id"], "needs_attention", payload=error)
                print(redact(f"{code}: {exc}"), flush=True)
            with db.transaction():
                current = db.row("SELECT worker_instance FROM seats WHERE id=?", (seat["id"],))
                if current["worker_instance"] == instance:
                    db.set_state("seats", seat["id"], "ready")
    finally:
        with db.transaction():
            db.execute("DELETE FROM locks WHERE name=? AND owner=?", (lock_name, instance))
            current = db.row("SELECT worker_instance FROM seats WHERE id=?", (seat["id"],))
            if current["worker_instance"] == instance:
                db.set_state("seats", seat["id"], "stopped")
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
