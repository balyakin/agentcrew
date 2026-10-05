"""Keyboard TUI checks against real SQLite state, without starting agents."""

import asyncio
import importlib
import json
import time
from contextlib import nullcontext

import pytest
from textual.widgets import DataTable, OptionList, TabbedContent, TextArea

from agentcrew.db import Database
from agentcrew.models import canonical, now


@pytest.fixture
def state(tmp_path):
    db = Database(tmp_path / "state")
    spec = tmp_path / "task.md"
    spec.write_text(
        "Build a CSV export.\n[red]literal[/red]\n\x1b[31mterminal escape\x1b[0m\ntoken=private-value"
    )
    sid = db.create_scope("run", str(tmp_path), scope_id="run-one")
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) VALUES(?,?,?,?,?,?)",
        (sid, "develop", "a" * 40, "a" * 40, "reviewing", canonical({"_spec_path": str(spec), "checks": []})),
    )
    db.set_state("scopes", sid, "running")
    candidate = db.add_candidate(sid, "b" * 40, "c" * 64, "candidate-branch")
    seat = db.add_seat(sid, "correctness", "codex", str(tmp_path))
    other_seat = db.add_seat(sid, "tests", "claude", str(tmp_path))
    db.set_state("seats", seat, "busy")
    db.set_state("seats", other_seat, "crashed")
    db.execute(
        "INSERT INTO tasks(id,scope_id,owner_seat,kind,state,body_json,candidate_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "task-one",
            sid,
            seat,
            "review",
            "claimed",
            canonical({"prompt": "Review CSV"}),
            candidate,
            now(),
            now(),
        ),
    )
    db.execute(
        "INSERT INTO check_runs(id,candidate_id,name,argv_json,state,exit_code,duration_ms,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("check-one", candidate, "unit", '["pytest"]', "failed", 1, 30, now()),
    )
    for identifier, label in (("requirement-one", "R1"), ("requirement-two", "R2")):
        db.execute(
            "INSERT INTO requirements VALUES(?,?,?,?,?)",
            (identifier, sid, label, "CSV has correct headers", canonical({"kind": "manual"})),
        )
    db.execute(
        "INSERT INTO requirement_results VALUES(?,?,?,?)", ("requirement-one", candidate, "unknown", "{}")
    )
    db.execute(
        "INSERT INTO requirement_results VALUES(?,?,?,?)", ("requirement-two", candidate, "failed", "{}")
    )
    db.execute(
        "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "finding-one",
            sid,
            candidate,
            seat,
            "reported",
            "P1",
            "export.py",
            12,
            "CSV is truncated",
            "Final record is missing",
            "One-line input drops the output row",
            "Check a one-line fixture",
            "Flush output",
            0.9,
            "fingerprint",
        ),
    )
    db.create_scope("team", str(tmp_path), scope_id="team-two")
    yield db, sid, candidate, seat
    db.close()


def crew(db, language="en"):
    return importlib.import_module("agentcrew.tui").CrewApp(db, language=language)


def test_80x24_keyboard_navigation_report_and_quit_leave_workflow_running(state, monkeypatch):
    db, sid, _, _ = state
    monkeypatch.setattr(
        "subprocess.Popen", lambda *args, **kwargs: pytest.fail("TUI startup started a process")
    )
    monkeypatch.setattr(
        "agentcrew.review.report.build_report", lambda *args, **kwargs: {"test": "independent result"}
    )
    monkeypatch.setattr(
        "agentcrew.review.report.markdown", lambda report, language="en": "Independent reviewer result"
    )

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)) as pilot:
            for identifier in ("scopes", "seats", "tasks", "checks", "findings"):
                table = app.query_one(f"#{identifier}", DataTable)
                assert table.region.width > 0 and table.region.height >= 3
                assert table.region.bottom <= 24
            await pilot.press("down", "up", "enter", "tab", "enter", "r")
            assert app.query_one("#details", TabbedContent).active == "report-tab"
            assert "Independent reviewer result" in app.query_one("#report-view", TextArea).text
            await pilot.press("q")
        assert db.scope(sid)["state"] == "running"
        assert db.row("SELECT state FROM tasks WHERE id='task-one'")["state"] == "claimed"

    asyncio.run(scenario())


def test_task_and_events_are_literal_sanitized_and_redacted(state):
    db, sid, _, _ = state
    db.event(sid, "provider.progress", {"text": "\x1b]0;title\x07[red]literal[/red]\ntoken=private-value"})

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)):
            text = app.query_one("#task-view", TextArea).text
            assert "[red]literal[/red]" in text
            assert "\x1b" not in text and "private-value" not in text
            app.query_one("#details", TabbedContent).active = "events-tab"
            app.refresh_state()
            text = app.query_one("#events-view", TextArea).text
            assert "[red]literal[/red]" in text
            assert "\x1b" not in text and "private-value" not in text

    asyncio.run(scenario())


def test_filter_and_help_work_with_keyboard_in_russian(state):
    db, _, _, _ = state

    async def scenario():
        app = crew(db, "ru")
        async with app.run_test(size=(80, 24)) as pilot:
            before = app.query_one("#scopes", DataTable).row_count
            await pilot.press("f")
            assert app.query_one("#scopes", DataTable).row_count < before
            await pilot.press("question_mark")
            assert app.screen.__class__.__name__ == "HelpScreen"
            assert "выход" in app.screen.query_one("#help-body", TextArea).text.lower()
            await pilot.press("escape")
            assert app.screen.__class__.__name__ != "HelpScreen"

    asyncio.run(scenario())


def test_narrow_terminal_uses_selectable_vertical_cards(state):
    db, _, _, _ = state

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(60, 24)) as pilot:
            cards = app.query_one("#cards", OptionList)
            assert cards.display
            assert cards.region.width <= 60 and cards.option_count >= 5
            await pilot.press("down", "enter", "tab")
            assert app.query_one("#details", TabbedContent).region.width <= 60

    asyncio.run(scenario())


def test_thousand_events_open_under_two_seconds_without_processes(state, monkeypatch):
    db, sid, _, _ = state
    with db.transaction():
        for index in range(1000):
            db.event(sid, "provider.progress", {"index": index, "text": "working"})
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("TUI started a process"))

    async def scenario():
        app = crew(db)
        started = time.monotonic()
        async with app.run_test(size=(80, 24)):
            assert time.monotonic() - started < 2
            app.query_one("#details", TabbedContent).active = "events-tab"
            app.refresh_state()
            assert len(app.query_one("#events-view", TextArea).text.splitlines()) <= 120

    asyncio.run(scenario())


def test_finding_reproduction_and_dismissal_require_human_audit(state):
    db, sid, candidate, _ = state

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#findings", DataTable).focus()
            await pilot.press("enter", "v")
            body = app.screen.query_one("#decision-body", TextArea)
            body.load_text("Reproduced using the one-line CSV fixture.")
            await pilot.press("ctrl+enter")
            assert db.row("SELECT status FROM findings WHERE id='finding-one'")["status"] == "reproduced"
            await pilot.press("x")
            app.screen.query_one("#decision-body", TextArea).load_text("no")
            await pilot.press("ctrl+enter")
            assert db.row("SELECT status FROM findings WHERE id='finding-one'")["status"] == "reproduced"
            app.screen.query_one("#decision-body", TextArea).load_text(
                "The complete CSV fixture includes the final record; this finding is false."
            )
            await pilot.press("ctrl+enter")
        decisions = db.rows("SELECT * FROM decisions WHERE scope_id=? AND kind='finding'", (sid,))
        assert len(decisions) == 2
        assert all(row["candidate_id"] == candidate and json.loads(row["body"])["actor"] for row in decisions)
        assert json.loads(decisions[0]["body"])["evidence"]
        assert json.loads(decisions[1]["body"])["reason"]
        assert db.row("SELECT status FROM findings WHERE id='finding-one'")["status"] == "dismissed"

    asyncio.run(scenario())


def test_manual_requirement_confirmation_is_candidate_bound_and_unknown_stays_distinct(state):
    db, sid, candidate, _ = state

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#details", TabbedContent).active = "requirements-tab"
            app.refresh_state()
            table = app.query_one("#requirements", DataTable)
            visible = " ".join(
                str(cell) for index in range(table.row_count) for cell in table.get_row_at(index)
            )
            assert "unknown" in visible and "failed" in visible
            table.focus()
            await pilot.press("enter", "m")
            app.screen.query_one("#decision-body", TextArea).load_text(
                "Manually checked CSV headers against the agreed fixture."
            )
            await pilot.press("ctrl+enter")
        row = db.row("SELECT * FROM decisions WHERE scope_id=? AND kind='requirement'", (sid,))
        assert row["candidate_id"] == candidate
        assert json.loads(row["body"])["status"] == "passed"
        assert (
            db.row(
                "SELECT status FROM requirement_results WHERE requirement_id='requirement-one' AND candidate_id=?",
                (candidate,),
            )["status"]
            == "passed"
        )

    asyncio.run(scenario())


def test_stale_candidate_cannot_receive_a_confirmation_from_an_open_modal(state):
    db, sid, _, _ = state

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#findings", DataTable).focus()
            await pilot.press("v")
            db.add_candidate(sid, "d" * 40, "e" * 64, "new-candidate")
            app.screen.query_one("#decision-body", TextArea).load_text(
                "Reproduced the defect on the old candidate."
            )
            await pilot.press("ctrl+enter")
        assert db.rows("SELECT * FROM decisions") == []
        assert db.row("SELECT status FROM findings WHERE id='finding-one'")["status"] == "reported"

    asyncio.run(scenario())


def test_pause_resume_cancel_call_supervisor_and_attach_uses_selected_seat(state, monkeypatch):
    db, sid, _, seat = state
    control_calls, attach_calls = [], []
    monkeypatch.setattr(
        "agentcrew.supervisor.control", lambda database, scope, action: control_calls.append((scope, action))
    )

    class Tmux:
        def __init__(self, database):
            assert database is db

        def attach(self, name):
            attach_calls.append(name)
            return 0

    monkeypatch.setattr("agentcrew.tmux_backend.Tmux", Tmux)

    async def scenario():
        app = crew(db)
        monkeypatch.setattr(app, "suspend", lambda: nullcontext())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("p", "s", "c")
            app.query_one("#seats", DataTable).focus()
            await pilot.press("enter", "a")
        assert control_calls == [(sid, "pause"), (sid, "resume"), (sid, "cancel")]
        assert attach_calls == [db.row("SELECT tmux_session FROM seats WHERE id=?", (seat,))["tmux_session"]]

    asyncio.run(scenario())


def test_i18n_has_english_and_russian_and_preserves_unknown_keys():
    t = importlib.import_module("agentcrew.i18n").t
    assert t("unknown", "en") == "unknown"
    assert t("unknown", "ru") != t("failed", "ru")
    assert t("report", "ru") == "Отчёт"
    assert t("structural_json_key", "ru") == "structural_json_key"


def test_answer_to_saved_questions_is_audited_once_and_resume_is_idempotent(state, monkeypatch):
    db, sid, _, _ = state
    db.execute(
        "UPDATE runs SET phase='plan',plan_json=? WHERE id=?",
        (canonical({"questions": ["Which CSV dialect?"]}), sid),
    )
    db.set_state("scopes", sid, "needs_attention")
    starts = []
    monkeypatch.setattr("agentcrew.develop.workflow.ensure_supervisor", lambda database: starts.append(True))

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(2):
                await pilot.press("b")
                app.screen.query_one("#decision-body", TextArea).load_text(
                    "Use the standard comma-separated CSV dialect."
                )
                await pilot.press("ctrl+enter")
        assert len(db.rows("SELECT id FROM decisions WHERE scope_id=? AND kind='answer'", (sid,))) == 1
        audit = db.rows("SELECT payload_json FROM events WHERE scope_id=? AND kind='tui.answer'", (sid,))
        assert len(audit) == 1 and json.loads(audit[0]["payload_json"])["actor"]
        assert db.scope(sid)["state"] == "running"
        assert starts == [True]

    asyncio.run(scenario())


def test_changed_candidate_invalidates_the_displayed_report(state, monkeypatch):
    db, sid, candidate, _ = state
    monkeypatch.setattr("agentcrew.review.report.build_report", lambda database, rid: database.run(rid))
    monkeypatch.setattr(
        "agentcrew.review.report.markdown",
        lambda report, language="en": "candidate=" + str(report["current_candidate_id"]),
    )

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("r")
            assert candidate in app.query_one("#report-view", TextArea).text
            new_candidate = db.add_candidate(sid, "e" * 40, "f" * 64, "new-branch")
            app.refresh_state()
            assert candidate not in app.query_one("#report-view", TextArea).text
            await pilot.press("r")
            assert new_candidate in app.query_one("#report-view", TextArea).text

    asyncio.run(scenario())


def test_view_refreshes_saved_status_without_writing_or_starting_agents(state, monkeypatch):
    db, _, _, seat = state
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("View started an agent"))

    async def scenario():
        app = crew(db)
        before = db.conn.total_changes
        async with app.run_test(size=(80, 24)) as pilot:
            assert db.conn.total_changes == before
            db.set_state("seats", seat, "crashed")
            written = db.conn.total_changes
            await pilot.pause(0.6)
            table = app.query_one("#seats", DataTable)
            assert any(
                "agent crashed" in str(cell)
                for index in range(table.row_count)
                for cell in table.get_row_at(index)
            )
            assert db.conn.total_changes == written

    asyncio.run(scenario())


def test_failure_incomplete_review_and_revision_limit_have_distinct_labels(state):
    db, sid, _, _ = state
    run = db.run(sid)
    config = run["config"]
    config["max_revisions"] = 0
    db.execute(
        "UPDATE runs SET phase='report',outcome='rejected',config_json=? WHERE id=?", (canonical(config), sid)
    )

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)):
            text = app.query_one("#task-view", TextArea).text.lower()
            assert "check failed" in text
            assert "review incomplete" in text
            assert "revision limit reached" in text

    asyncio.run(scenario())


def test_resize_and_viewer_focus_preserve_keyboard_access(state):
    db, _, _, _ = state

    async def scenario():
        app = crew(db)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#task-view", TextArea).focus()
            await pilot.press("question_mark", "escape")
            assert app.screen.__class__.__name__ != "HelpScreen"
            app.query_one("#scopes", DataTable).focus()
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            assert app.query_one("#cards", OptionList).display
            assert app.query_one("#cards", OptionList).has_focus
            await pilot.press("down", "enter")

    asyncio.run(scenario())
