"""Capture the actual Textual UI with fictional SQLite data; start no agents."""

import asyncio
import hashlib
import tempfile
from pathlib import Path

from textual.widgets import DataTable

from agentcrew.db import Database
from agentcrew.models import canonical, now
from agentcrew.tui import CrewApp

ROOT = Path(__file__).resolve().parents[1]


def populate(directory):
    db = Database(directory / "state")
    spec = directory / "task.md"
    spec.write_text(
        "DEMO DATA — no model calls\n\n"
        "Review the new CSV export for the orders CLI.\n"
        "Check empty input, quoting and preservation of existing files.\n",
        encoding="utf-8",
    )
    sid = db.create_scope("run", str(directory), scope_id="demo-review")
    sha = hashlib.sha1(b"fictional screenshot candidate", usedforsecurity=False).hexdigest()
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,outcome,config_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (sid, "review", sha, sha, "report", "rejected", canonical({"_spec_path": str(spec)})),
    )
    db.set_state("scopes", sid, "completed")
    cid = db.add_candidate(sid, sha, hashlib.sha256(b"demo diff").hexdigest(), "demo/csv-export")
    for role, provider in (("correctness", "codex"), ("tests", "claude")):
        seat = db.add_seat(sid, role, provider, str(directory))
        db.set_state("seats", seat, "ready")
        db.execute(
            "INSERT INTO tasks(id,scope_id,owner_seat,kind,state,body_json,candidate_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"demo-{role}", sid, seat, "review", "done",
                canonical({"prompt": "Independent review of the frozen CSV candidate."}), cid, now(), now(),
            ),
        )
    for name, argv in (("unit", ["python", "-m", "pytest"]), ("lint", ["ruff", "check", "."])):
        db.execute(
            "INSERT INTO check_runs(id,candidate_id,name,argv_json,state,exit_code,duration_ms,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f"demo-{name}", cid, name, canonical(argv), "passed", 0, 320, now()),
        )
    db.execute(
        "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "demo-finding", sid, cid, f"correctness@{sid}", "reported", "P1", "orders_export.py", 42,
            "Existing export is overwritten before validation",
            "Opening the destination in write mode truncates it before input validation finishes.",
            "The destination is opened before the empty-input check; no regression test covers this path.",
            "Export empty input to a path that already contains a previous report.",
            "Validate input first; write to a temporary file and replace the destination after success.",
            0.9, "demo-file-preservation",
        ),
    )
    return db


async def capture():
    destination = ROOT / "docs" / "assets" / "agentcrew-tui.svg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentcrew-screenshot-") as temporary:
        db = populate(Path(temporary))
        try:
            app = CrewApp(db)
            async with app.run_test(size=(116, 32)) as pilot:
                app.query_one("#findings", DataTable).focus()
                await pilot.press("enter")
                await pilot.pause()
                svg = app.export_screenshot(title="AgentCrew · Demo data")
                destination.write_text("\n".join(line.rstrip() for line in svg.splitlines()) + "\n", encoding="utf-8")
                assert "Existing export is overwritten" in app.query_one("#task-view").text
        finally:
            db.close()
    print(destination)


if __name__ == "__main__":
    asyncio.run(capture())
