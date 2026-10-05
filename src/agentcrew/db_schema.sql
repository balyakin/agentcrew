CREATE TABLE scopes (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, repo_root TEXT NOT NULL,
  state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY REFERENCES scopes(id),
  workflow TEXT NOT NULL CHECK(workflow IN ('develop','review','fix')),
  parent_run_id TEXT REFERENCES runs(id),
  source_head_sha TEXT NOT NULL, base_sha TEXT NOT NULL,
  current_candidate_id TEXT REFERENCES candidates(id),
  spec_sha256 TEXT, plan_json TEXT, phase TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT 'pending', config_json TEXT NOT NULL
);
CREATE TABLE candidates (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  parent_candidate_id TEXT REFERENCES candidates(id),
  commit_sha TEXT NOT NULL, diff_sha256 TEXT NOT NULL,
  branch_name TEXT, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(run_id, revision)
);
CREATE TABLE teams (
  id TEXT PRIMARY KEY REFERENCES scopes(id),
  template_name TEXT NOT NULL, config_json TEXT NOT NULL
);
CREATE TABLE seats (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(id),
  role TEXT NOT NULL, tmux_session TEXT NOT NULL, pane_id TEXT,
  provider TEXT NOT NULL, provider_session_id TEXT, worker_instance TEXT,
  state TEXT NOT NULL, heartbeat_at TEXT, worktree_path TEXT NOT NULL,
  UNIQUE(scope_id, role)
);
CREATE TABLE worktrees (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(id),
  candidate_id TEXT REFERENCES candidates(id), seat_id TEXT REFERENCES seats(id),
  path TEXT NOT NULL UNIQUE, branch_name TEXT, state TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(id),
  owner_seat TEXT NOT NULL REFERENCES seats(id), kind TEXT NOT NULL,
  state TEXT NOT NULL, body_json TEXT NOT NULL, result_json TEXT,
  parent_task_id TEXT REFERENCES tasks(id),
  candidate_id TEXT REFERENCES candidates(id),
  lease_owner TEXT, lease_until TEXT, attempt INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE task_dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  requires_task_id TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY(task_id, requires_task_id), CHECK(task_id <> requires_task_id)
);
CREATE TABLE requirements (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  label TEXT NOT NULL, description TEXT NOT NULL, verification_json TEXT NOT NULL,
  UNIQUE(run_id, label)
);
CREATE TABLE check_runs (
  id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id),
  name TEXT NOT NULL, argv_json TEXT NOT NULL, state TEXT NOT NULL,
  exit_code INTEGER, duration_ms INTEGER, log_path TEXT, log_sha256 TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE requirement_results (
  requirement_id TEXT NOT NULL REFERENCES requirements(id),
  candidate_id TEXT NOT NULL REFERENCES candidates(id),
  status TEXT NOT NULL CHECK(status IN ('passed','failed','unknown')),
  evidence_json TEXT NOT NULL,
  PRIMARY KEY(requirement_id, candidate_id)
);
CREATE TABLE messages (
  id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(id),
  sender TEXT NOT NULL, recipient TEXT NOT NULL REFERENCES seats(id),
  idempotency_key TEXT NOT NULL,
  body TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
  delivered_at TEXT, acknowledged_at TEXT,
  UNIQUE(recipient, idempotency_key)
);
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL REFERENCES scopes(id),
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE findings (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  candidate_id TEXT NOT NULL REFERENCES candidates(id),
  origin_seat TEXT NOT NULL REFERENCES seats(id),
  status TEXT NOT NULL, severity TEXT NOT NULL, file_path TEXT NOT NULL,
  line INTEGER NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
  evidence TEXT NOT NULL, reproduction TEXT, suggestion TEXT,
  confidence REAL NOT NULL, fingerprint TEXT NOT NULL
);