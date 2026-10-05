"""Durable tasks, fenced leases, directed messages and atomic handoffs."""

import json
from datetime import UTC, datetime, timedelta

from .models import UserError, canonical, now, uid

WRITING_KINDS = {"implement", "revise", "fix", "team.implement"}


def future(seconds: int = 30) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class Queue:
    def __init__(self, db):
        self.db = db

    def seat(self, scope_id: str, seat_id: str):
        seat = self.db.row("SELECT * FROM seats WHERE id=? AND scope_id=?", (seat_id, scope_id))
        if not seat:
            raise UserError("Seat does not belong to this run/team")
        return seat

    def create(
        self,
        scope_id: str,
        seat: str,
        kind: str,
        body: dict,
        *,
        dependencies=(),
        candidate_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> str:
        if len(canonical(body).encode()) > 1024 * 1024:
            raise UserError("Task body exceeds 1 MiB")
        task = uid()
        with self.db.transaction():
            self.seat(scope_id, seat)
            if candidate_id:
                candidate = self.db.row("SELECT run_id FROM candidates WHERE id=?", (candidate_id,))
                if not candidate or candidate["run_id"] != scope_id:
                    raise UserError("Task candidate belongs to another run")
            self.db.execute(
                "INSERT INTO tasks(id,scope_id,owner_seat,kind,state,body_json,parent_task_id,candidate_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task,
                    scope_id,
                    seat,
                    kind,
                    "pending",
                    canonical(body),
                    parent_task_id,
                    candidate_id,
                    now(),
                    now(),
                ),
            )
            for dependency in dependencies:
                self.add_dependency(task, dependency)
            self.db.event(scope_id, "task.created", {"task_id": task, "owner_seat": seat, "kind": kind})
        return task

    def add_dependency(self, task: str, required: str):
        with self.db.transaction():
            a = self.db.row("SELECT scope_id,state FROM tasks WHERE id=?", (task,))
            b = self.db.row("SELECT scope_id FROM tasks WHERE id=?", (required,))
            if not a or not b or a["scope_id"] != b["scope_id"] or task == required:
                raise UserError("Invalid task dependency")
            if a["state"] != "pending":
                raise UserError("Dependencies are frozen once a task starts")
            cycle = self.db.row(
                """WITH RECURSIVE deps(id) AS (
                SELECT requires_task_id FROM task_dependencies WHERE task_id=?
                UNION SELECT d.requires_task_id FROM task_dependencies d JOIN deps p ON d.task_id=p.id)
                SELECT id FROM deps WHERE id=?""",
                (required, task),
            )
            if cycle:
                raise UserError("Task dependency cycle")
            self.db.execute("INSERT INTO task_dependencies VALUES(?,?)", (task, required))
            self.db.event(a["scope_id"], "task.dependency", {"task_id": task, "requires_task_id": required})

    def claim(self, seat: str, instance: str, lease_seconds: int = 30) -> dict | None:
        with self.db.transaction():
            row = self.db.row(
                """SELECT t.* FROM tasks t JOIN scopes s ON s.id=t.scope_id
                WHERE t.owner_seat=? AND t.state='pending' AND s.state='running'
                AND (t.candidate_id IS NULL OR EXISTS (
                    SELECT 1 FROM runs r WHERE r.id=t.scope_id AND r.current_candidate_id=t.candidate_id))
                AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.requires_task_id
                     WHERE d.task_id=t.id AND (p.state<>'done' OR p.result_json IS NULL))
                AND NOT EXISTS (SELECT 1 FROM tasks busy WHERE busy.owner_seat=t.owner_seat AND busy.state='claimed')
                ORDER BY t.created_at,t.id LIMIT 1""",
                (seat,),
            )
            if not row:
                return None
            configuration = self.db.row(
                "SELECT config_json FROM runs WHERE id=? UNION ALL SELECT config_json FROM teams WHERE id=?",
                (row["scope_id"], row["scope_id"]),
            )
            if configuration and row["kind"] != "checks":
                limit = json.loads(configuration["config_json"]).get("max_parallel_agents", 2)
                active = self.db.row(
                    "SELECT count(*) AS n FROM tasks WHERE scope_id=? AND state='claimed' AND kind<>'checks'",
                    (row["scope_id"],),
                )["n"]
                if active >= limit:
                    return None
            self.db.execute(
                "UPDATE tasks SET state='claimed',lease_owner=?,lease_until=?,attempt=attempt+1,updated_at=? WHERE id=?",
                (instance, future(lease_seconds), now(), row["id"]),
            )
            self.db.event(
                row["scope_id"],
                "task.claimed",
                {"task_id": row["id"], "worker_instance": instance, "attempt": row["attempt"] + 1},
            )
            return self.db.row("SELECT * FROM tasks WHERE id=?", (row["id"],))

    def heartbeat(self, seat: str, instance: str, task: dict | None = None) -> bool:
        with self.db.transaction():
            cursor = self.db.execute(
                "UPDATE seats SET heartbeat_at=? WHERE id=? AND worker_instance=?", (now(), seat, instance)
            )
            if not cursor.rowcount:
                return False
            if task:
                renewal = self.db.execute(
                    "UPDATE tasks SET lease_until=? WHERE id=? AND state='claimed' AND lease_owner=? AND attempt=?",
                    (future(), task["id"], instance, task["attempt"]),
                )
                if not renewal.rowcount:
                    return False
            scope = self.db.row("SELECT scope_id FROM seats WHERE id=?", (seat,))["scope_id"]
            # Heartbeats at worker cadence keep active evidence bounded (~1 event/10s).
            self.db.event(scope, "worker.heartbeat", {"seat_id": seat, "worker_instance": instance})
        return True

    def finish(
        self,
        task_id: str,
        instance: str,
        attempt: int,
        result: dict,
        *,
        candidate_id: str | None = None,
        failed: bool = False,
    ) -> bool:
        with self.db.transaction():
            row = self.db.row("SELECT * FROM tasks WHERE id=?", (task_id,))
            if not row:
                raise UserError("Unknown task")
            owns_attempt = (
                row["state"] == "claimed" and row["lease_owner"] == instance and row["attempt"] == attempt
                and row["candidate_id"] == candidate_id
            )
            current_candidate = True
            if candidate_id is not None:
                run = self.db.row("SELECT current_candidate_id FROM runs WHERE id=?", (row["scope_id"],))
                current_candidate = run is not None and run["current_candidate_id"] == candidate_id
            valid = (
                owns_attempt and current_candidate
                and row["lease_until"] is not None
                and datetime.fromisoformat(row["lease_until"]) >= datetime.now(UTC)
            )
            if not valid:
                self.db.event(row["scope_id"], "task.stale_result", {"task_id": task_id, "attempt": attempt})
                if owns_attempt and not current_candidate:
                    # Its returning worker has finished the old process; keep diagnostics without holding the seat.
                    self.db.set_state("tasks", task_id, "canceled", payload={"reason": "Candidate superseded"})
                    self.db.execute(
                        "UPDATE tasks SET result_json=?,lease_owner=NULL,lease_until=NULL WHERE id=?",
                        (canonical(result), task_id),
                    )
                return False
            self.db.execute(
                "UPDATE tasks SET state=?,result_json=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                ("failed" if failed else "done", canonical(result), now(), task_id),
            )
            self.db.event(
                row["scope_id"],
                "task.failed" if failed else "task.done",
                {"task_id": task_id, "attempt": attempt},
            )
        return True

    def block(self, task_id: str, reason: str, remedy: str):
        if not reason.strip() or not remedy.strip():
            raise UserError("A blocked task needs a reason and remedy")
        with self.db.transaction():
            row = self.db.row("SELECT * FROM tasks WHERE id=?", (task_id,))
            if not row:
                raise UserError("Unknown task")
            self.db.set_state("tasks", task_id, "blocked", payload={"reason": reason, "remedy": remedy})
            self.db.execute(
                "UPDATE tasks SET result_json=?,lease_owner=NULL,lease_until=NULL WHERE id=?",
                (canonical({"reason": reason, "remedy": remedy}), task_id),
            )
            self.db.set_state(
                "scopes", row["scope_id"], "needs_attention", payload={"reason": reason, "remedy": remedy}
            )

    def retry(self, task_id: str, *, inspected: bool = False):
        row = self.db.row("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row or row["state"] not in {"blocked", "failed"}:
            raise UserError("Only failed/blocked tasks can be retried")
        body = json.loads(row["body_json"])
        if row["kind"] in WRITING_KINDS:
            if not inspected:
                raise UserError("Inspect partial writer worktree; retry requires --inspect-writer")
            from .git_snapshot import validate_writer_retry

            # Git validation can take time; do not hold the SQLite write lock across it.
            validate_writer_retry(self.db, row)
            body["_inspected_retry_attempt"] = row["attempt"] + 1
        with self.db.transaction():
            current = self.db.row("SELECT * FROM tasks WHERE id=?", (task_id,))
            if current != row:
                raise UserError("Task changed during retry inspection; inspect it again")
            scope = self.db.scope(row["scope_id"])
            if scope["state"] in {"canceled", "failed"}:
                raise UserError("Cannot retry a canceled or failed workflow; create a new run")
            run = self.db.row("SELECT * FROM runs WHERE id=?", (row["scope_id"],))
            if run:
                if row["candidate_id"] and row["candidate_id"] != run["current_candidate_id"]:
                    raise UserError("Cannot retry a task for a superseded candidate")
                verification = row["kind"] in {"correctness", "tests", "checks"}
                if scope["state"] == "completed" and (run["outcome"] != "partial" or not verification):
                    raise UserError("Only an incomplete current verification can reopen a completed run")
                phase = "evaluate" if run["phase"] == "report" and verification else run["phase"]
                self.db.execute("UPDATE runs SET phase=?,outcome='pending' WHERE id=?", (phase, run["id"]))
            self.db.set_state("tasks", task_id, "pending", payload={"writer_inspected": inspected})
            self.db.execute(
                "UPDATE tasks SET body_json=?,result_json=NULL,lease_owner=NULL,lease_until=NULL WHERE id=?",
                (canonical(body), task_id),
            )
            self.db.set_state("scopes", row["scope_id"], "running")

    def send(self, scope_id: str, sender: str, recipient: str, body: str, idempotency_key: str) -> str:
        if not body.strip() or len(body.encode()) > 1024 * 1024 or not idempotency_key:
            raise UserError("Message needs a bounded body and idempotency key")
        with self.db.transaction():
            self.seat(scope_id, recipient)
            old = self.db.row(
                "SELECT id FROM messages WHERE recipient=? AND idempotency_key=?",
                (recipient, idempotency_key),
            )
            if old:
                return old["id"]
            message = uid()
            self.db.execute(
                "INSERT INTO messages(id,scope_id,sender,recipient,idempotency_key,body,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (message, scope_id, sender, recipient, idempotency_key, body, "queued", now()),
            )
            self.db.event(scope_id, "message.queued", {"message_id": message, "recipient": recipient})
            return message

    def inbox(self, seat: str) -> list[dict]:
        return self.db.rows(
            "SELECT * FROM messages WHERE recipient=? AND state IN ('queued','delivered') ORDER BY created_at",
            (seat,),
        )

    def message_state(self, message_id: str, state: str):
        if state not in {"delivered", "acknowledged", "failed"}:
            raise UserError("Invalid delivery transition")
        with self.db.transaction():
            self.db.set_state("messages", message_id, state)
            if state != "failed":
                column = "delivered_at" if state == "delivered" else "acknowledged_at"
                self.db.execute(f"UPDATE messages SET {column}=? WHERE id=?", (now(), message_id))

    def complete(self, task: str, result: dict):
        if not result.get("artifact") and not result.get("commit_sha") and not result.get("no_artifact"):
            raise UserError("Completion needs an artifact or explicit no_artifact reason")
        with self.db.transaction():
            row = self.db.row("SELECT * FROM tasks WHERE id=?", (task,))
            if not row or row["state"] not in {"pending", "blocked"}:
                raise UserError("Cannot externally complete an active or completed task")
            if self.db.scope(row["scope_id"])["kind"] != "team":
                raise UserError("Workflow tasks require a validated provider/check result; use retry instead")
            self.db.execute(
                "UPDATE tasks SET result_json=?,state='done',updated_at=? WHERE id=?",
                (canonical(result), now(), task),
            )
            self.db.event(row["scope_id"], "task.completed_by_user", {"task_id": task})

    def handoff(self, task: str, to: str, artifact: dict, note: str) -> str:
        with self.db.transaction():
            row = self.db.row("SELECT * FROM tasks WHERE id=?", (task,))
            if not row or row["state"] != "done" or not row["result_json"]:
                raise UserError("Handoff requires a completed task with a controller artifact")
            self.seat(row["scope_id"], to)
            if not artifact.get("commit_sha") and not (artifact.get("artifact") and artifact.get("sha256")):
                raise UserError("Handoff requires a SHA-bound artifact")
            if self.db.row("SELECT id FROM tasks WHERE parent_task_id=?", (task,)):
                raise UserError("This task has already been handed off")
            child = self.create(
                row["scope_id"], to, "team.review", {"artifact": artifact, "note": note}, parent_task_id=task
            )
            self.db.event(
                row["scope_id"],
                "task.handoff",
                {"task_id": task, "next_task_id": child, "to": to, "artifact": artifact},
            )
            return child
