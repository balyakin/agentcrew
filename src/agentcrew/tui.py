"""Keyboard inspection and explicit audited actions over durable saved state."""

import getpass
import json
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.option_list import Option

from .db import Database
from .i18n import t
from .models import MAX_JSON, UserError, canonical, now, redact, uid


def _text(value) -> str:
    return redact(str(value if value is not None else ""))


def _json(raw):
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"error": "Invalid saved JSON"}


class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "close", "Close"), ("q", "close", "Close")]

    def __init__(self, language):
        super().__init__()
        self.language = language

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(t("help", self.language), markup=False)
            yield TextArea(t("help_text", self.language), read_only=True, id="help-body")
            yield Button(t("close", self.language), id="close-help")

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self):
        self.dismiss()


class EvidenceScreen(ModalScreen[dict | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "save", "Save", priority=True),
        Binding("ctrl+d", "failed", "Failed", priority=True),
    ]

    def __init__(self, title, language, *, reason=False, minimum=10, manual=False, prompt=None):
        super().__init__()
        self.caption, self.language = title, language
        self.reason, self.minimum, self.manual = reason, minimum, manual
        self.prompt = prompt

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.caption, markup=False)
            yield Static(
                self.prompt or t("reason_prompt" if self.reason else "evidence_prompt", self.language),
                markup=False,
            )
            yield TextArea(id="decision-body")
            yield Static("", id="decision-error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button(
                    t("passed" if self.manual else "save", self.language),
                    id="save-decision",
                    variant="primary",
                )
                if self.manual:
                    yield Button(t("failed", self.language), id="fail-decision")
                yield Button(t("cancel", self.language), id="cancel-decision")

    def on_mount(self):
        self.query_one("#decision-body", TextArea).focus()

    def save(self, status="passed"):
        body = self.query_one("#decision-body", TextArea).text.strip()
        if len(body) < self.minimum or (self.minimum >= 20 and len(body.split()) < 3) or len(body) > 8000:
            self.query_one("#decision-error", Static).update(t("evidence_required", self.language))
            return
        self.dismiss({"text": body, "status": status})

    def action_save(self):
        self.save()

    def action_failed(self):
        if self.manual:
            self.save("failed")

    def action_cancel(self):
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel-decision":
            self.dismiss(None)
        else:
            self.save("failed" if event.button.id == "fail-decision" else "passed")


class CrewApp(App):
    TITLE = "AgentCrew"
    CSS = """
    Screen { layout: vertical; }
    Header { height: 1; }
    #overview { height: 6; }
    #work { height: 6; }
    DataTable { border: round $primary; height: 1fr; }
    #scopes { width: 34%; }
    #seats { width: 66%; }
    #tasks { width: 29%; }
    #checks { width: 26%; }
    #findings { width: 45%; }
    #details { height: 1fr; min-height: 4; }
    TabPane { padding: 0; }
    TextArea { height: 1fr; border: none; }
    #requirements { height: 4; }
    #status-line { height: 1; }
    #cards { display: none; height: 12; border: round $primary; }
    .narrow #overview, .narrow #work { display: none; }
    .narrow #cards { display: block; }
    ModalScreen { align: center middle; background: $background 80%; }
    .dialog { width: 70; max-width: 95%; height: 18; max-height: 90%; padding: 1 2; border: round $primary; background: $surface; }
    .dialog Static { height: auto; }
    #decision-error { height: 2; color: $error; }
    .dialog-buttons { height: 3; }
    .dialog-buttons Button { min-width: 12; margin-right: 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "report", "Report"),
        Binding("a", "attach", "Attach"),
        Binding("f", "filter", "Filter"),
        Binding("question_mark", "help", "Help"),
        Binding("?", "help", "Help", show=False),
        Binding("p", "pause", "Pause", show=False),
        Binding("s", "resume", "Resume", show=False),
        Binding("c", "cancel", "Cancel", show=False),
        Binding("v", "reproduce", "Reproduce", show=False),
        Binding("x", "dismiss_finding", "Dismiss", show=False),
        Binding("m", "manual", "Manual", show=False),
        Binding("b", "answer", "Answer", show=False),
    ]

    def __init__(self, db: Database, language="en"):
        super().__init__()
        self.db, self.language = db, language
        self.selected_scope = self.selected_seat = self.selected_finding = self.selected_requirement = None
        self.selected_task = self.selected_check = None
        self.filter_mode = 0
        self._tables, self._contents, self._rows = {}, {}, {}
        self._detail_kind = "task"
        self._updating = False

    def tr(self, key):
        return t(key, self.language)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="overview"):
            yield DataTable(id="scopes", cursor_type="row", show_row_labels=False)
            yield DataTable(id="seats", cursor_type="row", show_row_labels=False)
        with Horizontal(id="work"):
            for identifier in ("tasks", "checks", "findings"):
                yield DataTable(id=identifier, cursor_type="row", show_row_labels=False)
        yield OptionList(id="cards", markup=False)
        with TabbedContent(id="details"):
            for identifier in ("task", "plan", "requirements", "diff", "report", "events"):
                with TabPane(self.tr(identifier), id=f"{identifier}-tab"):
                    if identifier == "requirements":
                        yield DataTable(id="requirements", cursor_type="row", show_row_labels=False)
                    yield TextArea(read_only=True, id=f"{identifier}-view", show_cursor=False)
        yield Static("", id="status-line", markup=False)
        yield Footer()

    def on_mount(self):
        for key, action in (
            ("q", "quit"),
            ("r", "report"),
            ("a", "attach"),
            ("f", "filter"),
            ("question_mark", "help"),
        ):
            self.bind(key, action, description=self.tr(action))
        columns = {
            "scopes": ["runs", "status"],
            "seats": ["role", "status"],
            "tasks": ["task", "status"],
            "checks": ["name", "status"],
            "findings": ["where", "status"],
            "requirements": ["name", "description", "status"],
        }
        for identifier, headings in columns.items():
            table = self.query_one(f"#{identifier}", DataTable)
            table.border_title = self.tr(identifier if identifier != "scopes" else "runs")
            table.add_columns(*(self.tr(key) for key in headings))
        self.set_class(self.size.width < 80, "narrow")
        self.refresh_state()
        self.query_one("#cards" if self.size.width < 80 else "#scopes").focus()
        self.set_interval(0.5, self.refresh_state)

    def on_resize(self, event):
        narrow = event.size.width < 80
        changing = narrow != self.has_class("narrow")
        self.set_class(narrow, "narrow")
        if changing and self.is_mounted:
            if narrow and (
                self.focused is None or self.focused.id in {"scopes", "seats", "tasks", "checks", "findings"}
            ):
                self.call_after_refresh(lambda: self.query_one("#cards", OptionList).focus())
            elif not narrow and self.focused and self.focused.id == "cards":
                self.call_after_refresh(lambda: self.query_one("#scopes", DataTable).focus())

    def status(self, message):
        self.query_one("#status-line", Static).update(_text(message))

    def _view(self, identifier, value):
        value = _text(value)[:200000]
        if self._contents.get(identifier) != value:
            self.query_one(f"#{identifier}-view", TextArea).load_text(value)
            self._contents[identifier] = value

    def _table(self, identifier, rows, cells):
        self._rows[identifier] = {row["id"]: row for row in rows}
        values = [(row["id"], tuple(_text(cell) for cell in cells(row))) for row in rows]
        if self._tables.get(identifier) == values:
            return
        table = self.query_one(f"#{identifier}", DataTable)
        cursor = table.cursor_row
        table.clear()
        for key, columns in values:
            table.add_row(*(Text(column) for column in columns), key=key)
        if values:
            table.move_cursor(row=min(cursor, len(values) - 1))
        self._tables[identifier] = values

    def refresh_state(self):
        if not self.is_mounted or self._updating:
            return
        self._updating = True
        try:
            scopes = self.db.rows("SELECT * FROM scopes ORDER BY kind,created_at DESC LIMIT 200")
            modes = (None, {"running", "paused"}, {"failed", "needs_attention"})
            if modes[self.filter_mode] is not None:
                scopes = [scope for scope in scopes if scope["state"] in modes[self.filter_mode]]
            if self.selected_scope not in {scope["id"] for scope in scopes}:
                self.selected_scope = scopes[0]["id"] if scopes else None
                self.selected_seat = self.selected_finding = self.selected_requirement = None
            self._table(
                "scopes", scopes, lambda row: [row["kind"] + " " + row["id"][:12], self.tr(row["state"])]
            )
            sid = self.selected_scope
            seats = self.db.rows("SELECT * FROM seats WHERE scope_id=? ORDER BY role", (sid,))
            tasks = self.db.rows(
                "SELECT * FROM tasks WHERE scope_id=? ORDER BY created_at DESC LIMIT 200", (sid,)
            )
            run = self.db.row("SELECT * FROM runs WHERE id=?", (sid,))
            candidate = (
                self.db.row("SELECT * FROM candidates WHERE id=?", (run["current_candidate_id"],))
                if run
                else None
            )
            cid = candidate["id"] if candidate else None
            checks = self.db.rows(
                "SELECT * FROM check_runs WHERE candidate_id=? ORDER BY created_at DESC", (cid,)
            )
            findings = self.db.rows(
                "SELECT f.*,s.role,s.provider FROM findings f JOIN seats s ON s.id=f.origin_seat "
                "WHERE f.run_id=? AND f.candidate_id=? ORDER BY f.severity,f.file_path",
                (sid, cid),
            )
            requirements = self.db.rows(
                "SELECT r.*,e.status,e.evidence_json FROM requirements r "
                "LEFT JOIN requirement_results e ON e.requirement_id=r.id AND e.candidate_id=? "
                "WHERE r.run_id=? ORDER BY r.label",
                (cid, sid),
            )
            self._table(
                "seats", seats, lambda row: [row["role"] + "/" + row["provider"], self.tr(row["state"])]
            )
            self._table("tasks", tasks, lambda row: [row["kind"], self.tr(row["state"])])
            self._table("checks", checks, lambda row: [row["name"], self.tr(row["state"])])
            self._table(
                "findings",
                findings,
                lambda row: [
                    row["severity"] + " " + row["file_path"] + ":" + str(row["line"]),
                    self.tr(row["status"]),
                ],
            )
            self._table(
                "requirements",
                requirements,
                lambda row: [row["label"], row["description"], self.tr(row["status"] or "unknown")],
            )
            for attribute, rows in (
                ("selected_seat", seats),
                ("selected_task", tasks),
                ("selected_check", checks),
                ("selected_finding", findings),
                ("selected_requirement", requirements),
            ):
                if getattr(self, attribute) not in {row["id"] for row in rows}:
                    setattr(self, attribute, rows[0]["id"] if rows else None)
            cards = [
                (f"{kind}:{key}", self.tr(kind if kind != "scopes" else "runs") + "\n" + " | ".join(columns))
                for kind in ("scopes", "seats", "tasks", "checks", "findings")
                for key, columns in self._tables[kind]
            ]
            if self._tables.get("cards") != cards:
                widget = self.query_one("#cards", OptionList)
                widget.clear_options().add_options(Option(Text(body), id=key) for key, body in cards)
                self._tables["cards"] = cards
            self._details(run, candidate)
        except Exception as exc:
            self.status(self.tr("read_error") + ": " + _text(exc))
        finally:
            self._updating = False

    def _details(self, run, candidate):
        if not self.selected_scope:
            for identifier in ("task", "plan", "requirements", "diff", "report", "events"):
                self._view(identifier, self.tr("empty"))
            self._contents["identity"] = None
            return
        scope = self._rows["scopes"][self.selected_scope]
        identity = (scope["id"], candidate["id"] if candidate else None)
        if self._contents.get("identity") != identity:
            self._view("report", self.tr("report_hint"))
            self._view("diff", self.tr("diff_hint"))
            self._contents.pop("diff-candidate", None)
            self._contents["identity"] = identity
        self.sub_title = scope["id"] + " | " + self.tr(scope["state"])
        config = _json(run["config_json"]) if run else {}
        body = self.tr("task") + ": " + scope["id"] + "\n"
        if run:
            body += self.tr(run["phase"]) + " | " + self.tr(run["outcome"]) + "\n"
            if candidate:
                body += self.tr("revision") + f": {candidate['revision']} | {candidate['commit_sha']}\n"
            failed = [row["name"] for row in self._rows["checks"].values() if row["state"] == "failed"]
            if failed:
                body += self.tr("check_failed") + ": " + ", ".join(failed) + "\n"
            if candidate and run["phase"] in {"evaluate", "report", "reviewing"}:
                incomplete = []
                for role in ("correctness", "tests"):
                    seat = next((seat for seat in self._rows["seats"].values() if seat["role"] == role), None)
                    task = (
                        self.db.row(
                            "SELECT state FROM tasks WHERE owner_seat=? AND candidate_id=? "
                            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                            (seat["id"], candidate["id"]),
                        )
                        if seat
                        else None
                    )
                    if not task or task["state"] != "done":
                        incomplete.append(role + " (" + self.tr(task["state"] if task else "unknown") + ")")
                if incomplete:
                    body += self.tr("review_incomplete") + ": " + ", ".join(incomplete) + "\n"
                limit = config.get("max_revisions")
                if (
                    run["workflow"] == "develop"
                    and run["outcome"] == "rejected"
                    and type(limit) is int
                    and candidate["revision"] > limit
                ):
                    body += self.tr("revision_limit") + f": {limit}\n"
            spec = config.get("_spec_path")
            if spec:
                path = Path(spec)
                try:
                    body += "\n" + (
                        path.read_text(errors="replace")
                        if path.stat().st_size <= MAX_JSON
                        else self.tr("spec_too_large")
                    )
                except OSError as exc:
                    body += "\n" + _text(exc)
        task = self._rows["tasks"].get(self.selected_task)
        if task:
            payload = _json(task["body_json"])
            body += (
                "\n\n"
                + self.tr("tasks")
                + f": {task['id']} | {self.tr(task['state'])}\n"
                + str(payload.get("prompt", payload))
            )
        if self._detail_kind == "finding" and self.selected_finding:
            row = self._rows["findings"][self.selected_finding]
            detail = (
                f"{row['severity']} {row['file_path']}:{row['line']} {row['title']}\n{self.tr(row['status'])}\n"
                f"{row['detail']}\n{self.tr('evidence')}: {row['evidence']}\n"
                f"{self.tr('reproduction')}: {row['reproduction']}\n"
                f"{self.tr('source')}: {row['role']}/{row['provider']} | {row['origin_seat']}\n"
                f"v: {self.tr('reproduce')} | x: {self.tr('dismiss')}\n\n"
            )
            body = detail + body
        if self._detail_kind == "check" and self.selected_check:
            row = self._rows["checks"][self.selected_check]
            body = (
                f"{row['name']}: {self.tr(row['state'])}; exit={row['exit_code']}\n{row['argv_json']}\n\n"
                + body
            )
        self._view("task", body)
        self._view(
            "plan",
            json.dumps(_json(run["plan_json"]), ensure_ascii=False, indent=2) if run else self.tr("unknown"),
        )
        requirement = self._rows["requirements"].get(self.selected_requirement)
        self._view(
            "requirements",
            (
                f"{requirement['label']}: {requirement['description']}\n"
                f"{self.tr(requirement['status'] or 'unknown')}\n"
                f"{requirement['verification_json']}\n{requirement['evidence_json'] or '{}'}\n"
                f"m: {self.tr('manual')}"
                if requirement
                else self.tr("unknown")
            ),
        )
        active = self.query_one("#details", TabbedContent).active
        if active == "events-tab":
            rows = self.db.rows(
                "SELECT * FROM events WHERE scope_id=? ORDER BY seq DESC LIMIT 100", (scope["id"],)
            )
            self._view(
                "events",
                "\n".join(
                    f"{row['seq']} {row['kind']} {_text(canonical(_json(row['payload_json'])))}"
                    for row in reversed(rows)
                ),
            )
        if active == "diff-tab" and run:
            if not candidate:
                self._view("diff", self.tr("no_candidate"))
            elif self._contents.get("diff-candidate") != candidate["id"]:
                from .git_snapshot import diff

                try:
                    patch = diff(Path(scope["repo_root"]), run["base_sha"], candidate["commit_sha"])
                    self._view("diff", patch.decode("utf-8", errors="replace"))
                    self._contents["diff-candidate"] = candidate["id"]
                except UserError as exc:
                    self._view("diff", str(exc))
        if "report" not in self._contents:
            self._view("report", self.tr("report_hint"))

    def _choose(self, kind, key, *, open_detail=False):
        if self._updating or key not in self._rows.get(kind, {}):
            return
        attributes = {
            "scopes": "selected_scope",
            "seats": "selected_seat",
            "tasks": "selected_task",
            "checks": "selected_check",
            "findings": "selected_finding",
            "requirements": "selected_requirement",
        }
        setattr(self, attributes[kind], key)
        if kind != "scopes":
            self._detail_kind = "finding" if kind == "findings" else "check" if kind == "checks" else "task"
        self.refresh_state()
        if open_detail:
            self.query_one("#details", TabbedContent).active = (
                "requirements-tab" if kind == "requirements" else "task-tab"
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.data_table is self.focused:
            self._choose(event.data_table.id, event.row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        self._choose(event.data_table.id, event.row_key.value, open_detail=True)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option.id:
            kind, key = event.option.id.split(":", 1)
            self._choose(kind, key, open_detail=True)

    def on_tabbed_content_tab_activated(self):
        self.refresh_state()

    def action_filter(self):
        self.filter_mode = (self.filter_mode + 1) % 3
        self.refresh_state()
        self.status(self.tr("filter") + ": " + self.tr(("all", "active", "attention")[self.filter_mode]))

    def action_help(self):
        self.push_screen(HelpScreen(self.language))

    def _call(self, operation):
        try:
            operation()
            self.refresh_state()
        except Exception as exc:
            self.status(str(exc))

    def action_report(self):
        def report():
            if not self.selected_scope or not self.db.row(
                "SELECT id FROM runs WHERE id=?", (self.selected_scope,)
            ):
                raise UserError(self.tr("select_run"))
            from .review.report import build_report, markdown

            self._view("report", markdown(build_report(self.db, self.selected_scope), self.language))
            self.query_one("#details", TabbedContent).active = "report-tab"

        self._call(report)

    def action_attach(self):
        def attach():
            seat = self._rows.get("seats", {}).get(self.selected_seat)
            if not seat:
                raise UserError(self.tr("select_seat"))
            from .tmux_backend import Tmux

            with self.suspend():
                Tmux(self.db).attach(seat["tmux_session"])

        self._call(attach)

    def _control(self, action):
        def control():
            if not self.selected_scope:
                raise UserError(self.tr("select_run"))
            from .supervisor import control

            control(self.db, self.selected_scope, action)
            self.status(self.tr(action))

        self._call(control)

    def action_pause(self):
        self._control("pause")

    def action_resume(self):
        self._control("resume")

    def action_cancel(self):
        self._control("cancel")

    def _audit(self, kind, target, candidate_id, scope_id, body):
        if not candidate_id or self.db.run(scope_id)["current_candidate_id"] != candidate_id:
            raise UserError(self.tr("candidate_changed"))
        decision = uid()
        self.db.execute(
            "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
            (decision, scope_id, candidate_id, kind, target, canonical(body), now()),
        )
        return decision

    def _finding_action(self, action):
        finding = self._rows.get("findings", {}).get(self.selected_finding)
        if not finding:
            self.status(self.tr("select_finding"))
            return
        reason = action == "dismissed"

        def save(result):
            if not result:
                return

            def record():
                body = {
                    "actor": getpass.getuser(),
                    "action": action,
                    "reason" if reason else "evidence": result["text"],
                }
                with self.db.transaction():
                    decision = self._audit(
                        "finding", finding["id"], finding["candidate_id"], finding["run_id"], body
                    )
                    self.db.set_state(
                        "findings",
                        finding["id"],
                        action,
                        payload={**body, "decision_id": decision, "candidate_id": finding["candidate_id"]},
                    )
                self.status(self.tr("saved"))

            self._call(record)

        self.push_screen(
            EvidenceScreen(
                self.tr("dismiss" if reason else "reproduce"),
                self.language,
                reason=reason,
                minimum=20 if reason and finding["severity"] in {"P0", "P1"} else 10,
            ),
            save,
        )

    def action_reproduce(self):
        self._finding_action("reproduced")

    def action_dismiss_finding(self):
        self._finding_action("dismissed")

    def action_manual(self):
        requirement = self._rows.get("requirements", {}).get(self.selected_requirement)
        if not requirement:
            self.status(self.tr("select_requirement"))
            return
        if _json(requirement["verification_json"]).get("kind") != "manual":
            self.status(self.tr("not_manual"))
            return
        scope_id = self.selected_scope
        candidate_id = self.db.run(scope_id)["current_candidate_id"]
        if not candidate_id:
            self.status(self.tr("no_candidate"))
            return

        def save(result):
            if not result:
                return

            def record():
                body = {"status": result["status"], "actor": getpass.getuser(), "evidence": result["text"]}
                with self.db.transaction():
                    decision = self._audit("requirement", requirement["id"], candidate_id, scope_id, body)
                    self.db.execute(
                        "INSERT INTO requirement_results VALUES(?,?,?,?) ON CONFLICT(requirement_id,candidate_id) "
                        "DO UPDATE SET status=excluded.status,evidence_json=excluded.evidence_json",
                        (
                            requirement["id"],
                            candidate_id,
                            body["status"],
                            canonical({**body, "decision_id": decision}),
                        ),
                    )
                    self.db.event(
                        scope_id,
                        "requirement.confirmed",
                        {
                            **body,
                            "decision_id": decision,
                            "candidate_id": candidate_id,
                            "requirement_id": requirement["id"],
                        },
                    )
                self.status(self.tr("saved"))

            self._call(record)

        self.push_screen(
            EvidenceScreen(
                self.tr("manual") + ": " + _text(requirement["label"]), self.language, manual=True
            ),
            save,
        )

    def action_answer(self):
        scope_id = self.selected_scope
        if not scope_id or not self.db.row("SELECT id FROM runs WHERE id=?", (scope_id,)):
            self.status(self.tr("select_run"))
            return
        run = self.db.run(scope_id)
        questions = _json(run["plan_json"]).get("questions", [])

        def save(result):
            if not result:
                return

            def record():
                from .develop.workflow import answer

                saved = answer(self.db, scope_id, result["text"])
                if not saved.get("already_saved"):
                    self.db.event(
                        scope_id,
                        "tui.answer",
                        {"actor": getpass.getuser(), "body": result["text"], "answer_id": saved["answer_id"]},
                    )
                self.status(self.tr("saved"))

            self._call(record)

        caption = self.tr("answer") + "\n" + "\n".join(_text(question) for question in questions)
        self.push_screen(
            EvidenceScreen(caption, self.language, minimum=1, prompt=self.tr("answer_prompt")), save
        )


def run_tui(db: Database, language="en") -> None:
    CrewApp(db, language=language).run()
