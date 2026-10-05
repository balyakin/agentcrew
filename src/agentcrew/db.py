"""Transactional SQLite state and immutable candidate identities."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import STATES, UserError, canonical, now, uid


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agentcrew"


class Database:
    def __init__(self, root: Path | str | None = None, *, read_only: bool = False):
        self.read_only = read_only
        self.root = Path(root) if root is not None else state_root()
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise UserError("State directory cannot be a symlink")
        self.root = self.root.resolve()
        if not read_only:
            os.chmod(self.root, 0o700)
        self.path = self.root / "state.sqlite3"
        if self.path.is_symlink():
            raise UserError("State database cannot be a symlink")
        self.conn = sqlite3.connect(
            self.path.as_uri() + "?mode=ro" if read_only else self.path,
            uri=read_only,
            timeout=5,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        if not read_only:
            os.chmod(self.path, 0o600)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.migrate()

    def migrate(self):
        with self.transaction():
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            if self.row("SELECT version FROM migrations WHERE version=1"):
                return
            schema = Path(__file__).with_name("db_schema.sql").read_text()
            for statement in schema.split(";"):
                if statement.strip():
                    self.conn.execute(statement)
            self.conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            self.conn.execute(
                "CREATE TABLE locks(name TEXT PRIMARY KEY,owner TEXT NOT NULL,expires_at TEXT NOT NULL)"
            )
            self.conn.execute(
                "CREATE TABLE approvals(repo_root TEXT NOT NULL,config_hash TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(repo_root,config_hash))"
            )
            self.conn.execute(
                "CREATE TABLE decisions(id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(id),candidate_id TEXT REFERENCES candidates(id),kind TEXT NOT NULL,target TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL)"
            )
            self.conn.execute(
                "CREATE TABLE provider_results(task_id TEXT NOT NULL REFERENCES tasks(id),attempt INTEGER NOT NULL,raw_path TEXT NOT NULL,metadata_json TEXT NOT NULL,valid INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(task_id,attempt))"
            )
            self.conn.execute(
                "CREATE TABLE check_evidence(check_id TEXT PRIMARY KEY REFERENCES check_runs(id),result_json TEXT NOT NULL)"
            )
            self.conn.execute("CREATE INDEX tasks_owner_state ON tasks(owner_seat,state)")
            self.conn.execute("CREATE INDEX events_scope_seq ON events(scope_id,seq)")
            self.conn.execute(
                "CREATE TRIGGER candidates_immutable BEFORE UPDATE ON candidates BEGIN SELECT RAISE(ABORT,'candidate is immutable'); END"
            )
            self.conn.execute("""CREATE TRIGGER evidence_scope BEFORE INSERT ON requirement_results
              WHEN (SELECT run_id FROM requirements WHERE id=NEW.requirement_id) <>
                   (SELECT run_id FROM candidates WHERE id=NEW.candidate_id)
              BEGIN SELECT RAISE(ABORT,'evidence scope mismatch'); END""")
            for table in ("tasks", "findings"):
                field = "scope_id" if table == "tasks" else "run_id"
                self.conn.execute(f"""CREATE TRIGGER {table}_candidate_scope BEFORE INSERT ON {table}
                  WHEN NEW.candidate_id IS NOT NULL AND NEW.{field} <>
                       (SELECT run_id FROM candidates WHERE id=NEW.candidate_id)
                  BEGIN SELECT RAISE(ABORT,'candidate scope mismatch'); END""")
            self.conn.execute("""CREATE TRIGGER findings_origin_scope BEFORE INSERT ON findings
              WHEN NEW.run_id <> (SELECT scope_id FROM seats WHERE id=NEW.origin_seat)
              BEGIN SELECT RAISE(ABORT,'finding origin mismatch'); END""")
            self.conn.execute("""CREATE TRIGGER check_candidate_scope BEFORE UPDATE OF candidate_id ON check_runs
              BEGIN SELECT RAISE(ABORT,'check evidence cannot move'); END""")
            self.conn.execute("""CREATE TRIGGER requirement_no_delete BEFORE DELETE ON requirements
              BEGIN SELECT RAISE(ABORT,'requirements cannot be deleted'); END""")
            self.conn.execute("INSERT INTO migrations VALUES(1,?)", (now(),))

    @contextmanager
    def transaction(self):
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN" if self.read_only else "BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def execute(self, sql: str, args=()):
        return self.conn.execute(sql, args)

    def row(self, sql: str, args=()) -> dict | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def rows(self, sql: str, args=()) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, args)]

    def event(self, scope_id: str, kind: str, payload: dict):
        self.execute(
            "INSERT INTO events(scope_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
            (scope_id, kind, canonical(payload), now()),
        )

    def create_scope(self, kind: str, repo_root: str, *, scope_id: str | None = None) -> str:
        if kind not in {"run", "team"}:
            raise UserError("Unknown scope kind")
        scope_id = scope_id or uid()
        with self.transaction():
            self.execute(
                "INSERT INTO scopes VALUES(?,?,?,?,?,?)",
                (scope_id, kind, str(repo_root), "preparing", now(), now()),
            )
            self.event(scope_id, "scope.created", {"kind": kind})
        return scope_id

    def add_seat(self, scope_id: str, role: str, provider: str, worktree_path: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", role) or provider not in {"codex", "claude"}:
            raise UserError("Invalid role or provider")
        seat = role + "@" + scope_id
        with self.transaction():
            self.execute(
                "INSERT INTO seats(id,scope_id,role,tmux_session,provider,state,worktree_path) VALUES(?,?,?,?,?,?,?)",
                (
                    seat,
                    scope_id,
                    role,
                    "seat-" + scope_id.replace("-", "") + "-" + role,
                    provider,
                    "starting",
                    worktree_path,
                ),
            )
            self.event(scope_id, "seat.created", {"seat_id": seat})
        return seat

    def set_state(self, table: str, identifier: str, state: str, *, payload: dict | None = None):
        types = {
            "scopes": "run",
            "seats": "seat",
            "tasks": "task",
            "messages": "message",
            "findings": "finding",
        }
        if table not in types or state not in STATES[types[table]]:
            raise UserError("Invalid state")
        with self.transaction():
            row = self.row(f"SELECT * FROM {table} WHERE id=?", (identifier,))
            if not row:
                raise UserError(f"Unknown {table} ID")
            if row["state" if table != "findings" else "status"] == state:
                return
            column = "status" if table == "findings" else "state"
            if table in {"scopes", "tasks"}:
                self.execute(
                    f"UPDATE {table} SET {column}=?,updated_at=? WHERE id=?", (state, now(), identifier)
                )
            else:
                self.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (state, identifier))
            if table == "scopes" and state in {"needs_attention", "canceled", "failed"}:
                self.execute(
                    "UPDATE runs SET outcome='partial' WHERE id=? AND outcome='pending'", (identifier,)
                )
            scope = row.get("scope_id", row.get("run_id", identifier))
            self.event(scope, f"{types[table]}.state", {"id": identifier, "state": state, **(payload or {})})

    def add_candidate(
        self, run_id: str, sha: str, diff_hash: str, branch_name: str | None, parent_id: str | None = None
    ) -> str:
        candidate = uid()
        with self.transaction():
            if (
                parent_id
                and self.row("SELECT run_id FROM candidates WHERE id=?", (parent_id,))["run_id"] != run_id
            ):
                raise UserError("Candidate parent belongs to another run")
            revision = self.row(
                "SELECT coalesce(max(revision),0)+1 AS r FROM candidates WHERE run_id=?", (run_id,)
            )["r"]
            self.execute(
                "INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?)",
                (candidate, run_id, parent_id, sha, diff_hash, branch_name, revision, now()),
            )
            self.execute("UPDATE runs SET current_candidate_id=? WHERE id=?", (candidate, run_id))
            self.event(
                run_id,
                "candidate.frozen",
                {"candidate_id": candidate, "commit_sha": sha, "revision": revision},
            )
        return candidate

    def scope(self, scope_id: str) -> dict:
        row = self.row("SELECT * FROM scopes WHERE id=?", (scope_id,))
        if not row:
            raise UserError("Unknown run/team ID; use status")
        return row

    def run(self, run_id: str) -> dict:
        row = self.row(
            "SELECT r.*,s.repo_root,s.state,s.created_at,s.updated_at FROM runs r JOIN scopes s ON s.id=r.id WHERE r.id=?",
            (run_id,),
        )
        if not row:
            raise UserError("Unknown run ID; use status")
        row["config"] = json.loads(row["config_json"])
        return row

    def close(self):
        self.conn.close()
