"""Complete argparse interface; readers never start background agents."""

import argparse
import getpass
import json
import sqlite3
import sys
import time
import tomllib
from pathlib import Path

from . import __version__
from .db import Database
from .i18n import t
from .models import MAX_JSON, UserError, canonical, digest, now, redact, redact_data, sanitize, uid


def parser(language="en"):
    class LocalizedParser(argparse.ArgumentParser):
        def format_help(self):
            text = super().format_help()
            if language == "ru":
                for source, target in {
                    "usage:": "Использование:",
                    "options:": "Параметры:",
                    "positional arguments:": "Позиционные аргументы:",
                    "show this help message and exit": "показать справку и выйти",
                    "show program's version number and exit": "показать версию и выйти",
                }.items():
                    text = text.replace(source, target)
            return text

    root = LocalizedParser(prog="agentcrew", description=t("Local verifiable coding teams in tmux", language))
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--lang", choices=["en", "ru"], default=language)
    sub = root.add_subparsers(dest="command", required=True)

    def command(name, help_text, parent=sub, reading=False):
        p = parent.add_parser(name, help=t(help_text, language), description=t(help_text, language))
        p.add_argument("--lang", choices=["en", "ru"], default=argparse.SUPPRESS)
        if reading:
            p.add_argument("--json", action="store_true")
        return p

    p = command("doctor", "Check environment; model calls only with --smoke", reading=True)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--provider", choices=["codex", "claude"], default="codex")
    p.add_argument("--yes", action="store_true")
    p = command("init", "Create repository configuration without running it", reading=True)
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    for name in ("develop", "new", "review"):
        p = command(
            name,
            {
                "develop": "Implement a specification",
                "new": "Create and develop a project",
                "review": "Review a frozen Git change",
            }[name],
            reading=True,
        )
        p.add_argument("--provider", choices=["codex", "claude"])
        p.add_argument("--detach", action="store_true")
        p.add_argument("--yes", action="store_true")
        if name == "new":
            p.add_argument("--directory", type=Path, required=True)
            p.add_argument("--template", choices=["python-cli", "typescript-cli"], required=True)
        else:
            p.add_argument("--cwd", type=Path, default=Path.cwd())
        if name == "review":
            p.add_argument("--base", required=True)
            p.add_argument("--no-checks", action="store_true")
        else:
            p.add_argument("--spec", type=Path, required=True)
    p = command("fix", "Fix one finding in a separate child run", reading=True)
    p.add_argument("run_id")
    p.add_argument("--finding", required=True)
    p.add_argument("--provider", choices=["codex", "claude"])
    p.add_argument("--detach", action="store_true")
    p.add_argument("--yes", action="store_true")
    p = command("status", "Read current runs/teams", reading=True)
    p.add_argument("scope_id", nargs="?")
    for name in ("report", "plan", "diff"):
        p = command(name, "Read " + name, reading=True)
        p.add_argument("run_id")
        if name != "diff":
            p.add_argument("--format", choices=["json", "md"], default="md")
        if name == "report":
            p.add_argument("--include-logs", action="store_true")
    p = command("finding", "Inspect or audit a finding", reading=True)
    p.add_argument("run_id")
    p.add_argument("finding_id")
    p.add_argument("--status", choices=["reproduced", "dismissed"])
    p.add_argument("--reason")
    p.add_argument("--evidence")
    p = command("confirm", "Audit candidate-bound manual requirement evidence", reading=True)
    p.add_argument("run_id")
    p.add_argument("requirement")
    p.add_argument("--status", choices=["passed", "failed"], required=True)
    p.add_argument("--evidence", required=True)
    p = command("answer", "Answer a recorded planner question", reading=True)
    p.add_argument("run_id")
    p.add_argument("--body-file", type=Path, required=True)
    p = command("apply", "Apply a specific source-based patch without committing", reading=True)
    p.add_argument("run_id")
    p.add_argument("--candidate")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-unaccepted", action="store_true")
    p.add_argument("--yes", action="store_true")
    p = command("attach", "Attach a persistent worker pane")
    p.add_argument("scope_id")
    p.add_argument("role")
    for name in ("pause", "resume", "cancel"):
        p = command(name, name + " one workflow", reading=True)
        p.add_argument("scope_id")
    p = command("recover", "Inspect recovery before restarting verified processes", reading=True)
    p.add_argument("scope_id", nargs="?")
    p.add_argument("--plan", action="store_true")
    p = command("cleanup", "Preview/delete only owned clean completed worktrees", reading=True)
    p.add_argument("scope_id", nargs="?")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--older-than-days", type=int)
    p = command("export", "Export a report with explicit redaction", reading=True)
    p.add_argument("run_id")
    p.add_argument("--redacted", action="store_true")
    p.add_argument("--include-logs", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    p = command("send", "Write an informational durable inbox message", reading=True)
    p.add_argument("seat")
    p.add_argument("--body-file", type=Path, required=True)
    p.add_argument("--idempotency-key")
    p.add_argument("--verify", action="store_true")
    p = command("tui", "Keyboard dashboard; closing it preserves workers")
    team = command("team", "Persistent teams").add_subparsers(dest="team_command", required=True)
    p = command("up", "Start isolated persistent seats", team, True)
    p.add_argument("--template", default="pair")
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.add_argument("--provider", choices=["codex", "claude"])
    p.add_argument("--yes", action="store_true")
    command("ps", "Read teams", team, True)
    p = command("stop", "Stop only this team", team, True)
    p.add_argument("team_id")
    p.add_argument("--snapshot", action="store_true")
    p = command("resume", "Recover a stopped team", team, True)
    p.add_argument("team_id")
    queue = command("queue", "Durable owned work").add_subparsers(dest="queue_command", required=True)
    p = command("create", "Assign a task", queue, True)
    p.add_argument("--to", required=True)
    p.add_argument("--body-file", type=Path, required=True)
    p = command("list", "Read tasks", queue, True)
    p.add_argument("--team")
    p.add_argument("--run")
    p.add_argument("--to")
    p = command("show", "Read one task", queue, True)
    p.add_argument("task_id")
    p = command("handoff", "Transfer a SHA artifact", queue, True)
    p.add_argument("task_id")
    p.add_argument("--to", required=True)
    p.add_argument("--note", default="")
    p = command("complete", "Record artifact completion", queue, True)
    p.add_argument("task_id")
    p.add_argument("--artifact", type=Path)
    p.add_argument("--no-artifact")
    p = command("block", "Block with a remedy", queue, True)
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--remedy", required=True)
    p = command("retry", "Retry only after stopping and inspecting old attempt", queue, True)
    p.add_argument("task_id")
    p.add_argument("--inspect-writer", action="store_true")
    return root


def emit(value, args):
    value = redact_data(value)
    if getattr(args, "json", False):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    elif isinstance(value, str):
        print(sanitize(value))
    else:
        if args.lang == "ru":
            print(t(args.command, args.lang) + ":")
        print(json.dumps(value, ensure_ascii=False, indent=2))


def yes_or_prompt(args, explanation):
    print(redact(explanation), file=sys.stderr)
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise UserError("Explicit consent required; repeat this command with --yes", "consent_required")
    print(
        "Продолжить? [y/N] " if args.lang == "ru" else "Continue? [y/N] ", file=sys.stderr, end="", flush=True
    )
    if input().strip().lower() not in {"y", "yes", "д", "да"}:
        raise UserError("Canceled before launch", "consent_required")


def trust(db, repo, config, args, files):
    policy = {k: v for k, v in config.items() if not k.startswith("_")}
    key = digest(canonical(policy))
    repo = str(Path(repo).resolve())
    if db.row("SELECT 1 FROM approvals WHERE repo_root=? AND config_hash=?", (repo, key)):
        return
    providers = {v["provider"] for v in config.get("roles", {}).values()} or {
        config.get("default_provider", "codex")
    }
    access = canonical({role: options["access"] for role, options in config.get("roles", {}).items()})
    if args.lang == "ru":
        explanation = (
            f"Репозиторий: {repo}\nПровайдеры: {', '.join(sorted(providers))}; исходники могут уйти в их сервисы.\n"
            f"Файлы ({len(files)}): {canonical(files)}\nПрава по ролям: {access}\n"
            f"Проверки выполняют код репозитория на этой машине: {canonical(config.get('checks', []))}\n"
            f"Лимиты: review {config.get('max_review_seconds', 900)} с, develop {config.get('max_develop_seconds', 1800)} с.\n"
            "Рабочие копии и права CLI не заменяют системную изоляцию. Исходник меняет только явный apply."
        )
    else:
        explanation = f"Repository: {repo}\nProviders: {', '.join(sorted(providers))}; source may be sent to their services.\nFiles ({len(files)}): {canonical(files)}\nAccess by role: {access}\nChecks execute repository code on this machine: {canonical(config.get('checks', []))}\nLimits: review {config.get('max_review_seconds', 900)}s, develop {config.get('max_develop_seconds', 1800)}s.\nWorktrees/native permissions are not a security boundary. Source changes only by explicit apply."
    yes_or_prompt(args, explanation)
    with db.transaction():
        db.execute("INSERT INTO approvals VALUES(?,?,?)", (repo, key, now()))


def check_providers(config):
    from .providers.base import get_adapter

    seen = set()
    for options in config["roles"].values():
        key = (options["provider"], options["access"])
        if key in seen:
            continue
        seen.add(key)
        get_adapter(key[0]).require({"access": key[1]})


def wait_run(db, rid, args):
    print(t("run", args.lang) + " " + rid, file=sys.stderr)
    if args.detach:
        emit({"run_id": rid, "state": "queued"}, args)
        return 0
    last = None
    try:
        while True:
            run = db.run(rid)
            state = (run["phase"], run["state"], run["outcome"])
            if state != last:
                print(" / ".join(t(value, args.lang) for value in state), file=sys.stderr)
                last = state
            if run["state"] in {"completed", "failed", "canceled", "needs_attention"}:
                from .review.report import build_report, markdown

                report = build_report(db, rid)
                emit(report if args.json else markdown(report, args.lang), args)
                return 0 if run["outcome"] == "accepted" else 3 if run["state"] == "failed" else 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        message = (
            "Клиент отсоединён; работа продолжается."
            if args.lang == "ru"
            else "Detached; workflow continues."
        )
        print(f"{message} agentcrew status {rid}", file=sys.stderr)
        return 130


def finding_decision(db, rid, fid, status, reason):
    from .review.fix import resolve_finding

    if not reason or not reason.strip():
        raise UserError("Record who/how reproduced or a dismissal reason; use --reason/--evidence")
    finding = resolve_finding(db, rid, fid)
    with db.transaction():
        db.execute(
            "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
            (
                uid(),
                rid,
                finding["candidate_id"],
                "finding",
                finding["id"],
                canonical({"status": status, "actor": getpass.getuser(), "evidence": reason}),
                now(),
            ),
        )
        db.set_state(
            "findings", finding["id"], status, payload={"actor": getpass.getuser(), "evidence": reason}
        )
    return db.row("SELECT * FROM findings WHERE id=?", (finding["id"],))


def requirement_decision(db, rid, label, status, evidence):
    run = db.run(rid)
    req = db.row("SELECT * FROM requirements WHERE run_id=? AND (label=? OR id=?)", (rid, label, label))
    if not req or not run["current_candidate_id"] or not evidence.strip():
        raise UserError("Select an existing candidate and requirement with meaningful evidence")
    with db.transaction():
        db.execute(
            "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
            (
                uid(),
                rid,
                run["current_candidate_id"],
                "requirement",
                req["id"],
                canonical({"status": status, "actor": getpass.getuser(), "evidence": evidence}),
                now(),
            ),
        )
        db.event(
            rid,
            "requirement.confirmed",
            {
                "requirement_id": req["id"],
                "candidate_id": run["current_candidate_id"],
                "actor": getpass.getuser(),
                "status": status,
                "evidence": evidence,
            },
        )
        if run["state"] == "needs_attention" and run["phase"] == "evaluate":
            db.set_state("scopes", rid, "running")
    from .supervisor import ensure_supervisor

    ensure_supervisor(db)
    return {
        "run_id": rid,
        "requirement": label,
        "candidate_id": run["current_candidate_id"],
        "status": status,
    }


def dispatch(args):
    cmd = args.command
    if cmd == "doctor":
        from .doctor import doctor

        if args.smoke:
            yes_or_prompt(
                args,
                "Эта проверка расходует токены модели на одноразовом тестовом проекте."
                if args.lang == "ru"
                else "This explicit smoke test spends model tokens on a disposable local fixture.",
            )
            from .smoke import smoke

            emit(smoke(args.provider), args)
        else:
            emit(doctor(), args)
        return 0
    if cmd == "init":
        from .config import init_config, load_config

        path = init_config(args.cwd, args.lang)
        emit(
            {
                "config": str(path),
                "checks": load_config(args.cwd)["checks"],
                "next": "Проверьте настройки, затем закоммитьте .agentcrew.toml или явно добавьте его в .git/info/exclude. Проверки не запускались."
                if args.lang == "ru"
                else "Review config, then commit it or explicitly add .agentcrew.toml to .git/info/exclude. No check was executed.",
            },
            args,
        )
        return 0
    read_only = (
        cmd in {"status", "report", "plan", "diff", "export", "attach"}
        or (cmd == "recover" and args.plan)
        or (cmd == "team" and args.team_command == "ps")
        or (cmd == "queue" and args.queue_command in {"list", "show"})
    )
    if read_only:
        from .db import state_root

        if not (state_root() / "state.sqlite3").exists():
            if cmd == "recover" and not args.scope_id:
                emit(
                    {
                        name: []
                        for name in (
                            "scopes",
                            "sessions",
                            "blocked_tasks",
                            "worktrees",
                            "actions",
                            "provider_processes",
                        )
                    },
                    args,
                )
                return 0
            if cmd == "status" and not args.scope_id:
                emit({"scopes": []}, args)
                return 0
            if cmd == "team":
                emit({"teams": []}, args)
                return 0
            if cmd == "queue" and args.queue_command == "list":
                emit({"tasks": []}, args)
                return 0
            raise UserError("No recorded state; use status or start a workflow", "state_unavailable")
    db = Database(read_only=read_only)
    try:
        if cmd in {"review", "develop", "new"}:
            from .config import load_config
            from .git_snapshot import preflight

            if cmd == "new":
                from .develop.scaffold import scaffold
                from .develop.workflow import read_spec

                read_spec(args.spec)
                # Probe requested provider before creating the destination.
                from .config import validate_config

                check_providers(validate_config({"default_provider": args.provider or "codex"}))
                repo = scaffold(args.directory, args.template, args.spec)
            else:
                repo = args.cwd
            config = load_config(repo, no_checks=getattr(args, "no_checks", False), provider=args.provider)
            config["_command"] = "agentcrew " + cmd
            snap = preflight(repo, "review" if cmd == "review" else "develop", getattr(args, "base", None))
            check_providers(config)
            trust(db, repo, config, args, snap["changed_files"])
            if cmd == "review":
                from .review.workflow import start_review

                rid = start_review(db, repo, args.base, config)
            else:
                from .develop.workflow import start_develop

                rid = start_develop(db, repo, args.spec, config)
            return wait_run(db, rid, args)
        if cmd == "fix":
            from .review.fix import start_fix

            parent = db.run(args.run_id)
            config = {**parent["config"], "max_revisions": 0}
            if args.provider:
                config["default_provider"] = args.provider
                config["roles"] = {
                    role: {**options, "provider": args.provider} for role, options in config["roles"].items()
                }
            check_providers(config)
            trust(db, parent["repo_root"], config, args, [])
            return wait_run(db, start_fix(db, args.run_id, args.finding, provider=args.provider), args)
        if cmd == "status":
            scopes = (
                [db.scope(args.scope_id)]
                if args.scope_id
                else db.rows("SELECT * FROM scopes ORDER BY created_at DESC LIMIT 100")
            )
            for scope in scopes:
                scope["seats"] = db.rows(
                    "SELECT id,role,state,provider,worktree_path,heartbeat_at FROM seats WHERE scope_id=?",
                    (scope["id"],),
                )
                scope["tasks"] = db.rows(
                    "SELECT id,owner_seat,kind,state,candidate_id,attempt FROM tasks WHERE scope_id=? ORDER BY created_at DESC LIMIT 100",
                    (scope["id"],),
                )
                scope["run"] = db.row(
                    "SELECT workflow,phase,outcome,current_candidate_id FROM runs WHERE id=?", (scope["id"],)
                )
            emit({"scopes": scopes}, args)
            return 0
        if cmd in {"report", "export"}:
            from .review.report import build_report, markdown

            report = build_report(db, args.run_id, args.include_logs)
            if cmd == "export":
                if args.redacted:
                    report["config"] = {k: v for k, v in report["config"].items() if not k.startswith("_")}
                    report.pop("logs", None)
                with args.output.open("x", encoding="utf-8") as file:
                    file.write(redact(canonical(report)) if args.redacted else canonical(report))
                args.output.chmod(0o600)
                emit({"output": str(args.output)}, args)
            else:
                emit(report if args.json or args.format == "json" else markdown(report, args.lang), args)
            return 0
        if cmd == "plan":
            run = db.run(args.run_id)
            plan = json.loads(run["plan_json"]) if run["plan_json"] else None
            if args.json or args.format == "json":
                emit({"run_id": args.run_id, "plan": plan}, args)
            else:
                text = f"# {t('plan', args.lang)} {args.run_id}\n"
                if plan:
                    text += (
                        plan["summary"]
                        + "\n\n"
                        + "\n".join(
                            f"- {r['id']}: {r['description']} ({r['verification']['kind']} {r['verification']['test_id']})"
                            for r in plan["requirements"]
                        )
                    )
                    text += "\n\n" + t("questions", args.lang) + ": " + canonical(plan["questions"])
                else:
                    text += t("no_plan", args.lang)
                emit(text, args)
            return 0
        if cmd == "diff":
            from .git_snapshot import diff

            run = db.run(args.run_id)
            candidate = db.row("SELECT * FROM candidates WHERE id=?", (run["current_candidate_id"],))
            patch = (
                diff(Path(run["repo_root"]), run["base_sha"], candidate["commit_sha"]) if candidate else b""
            )
            emit(
                {
                    "run_id": run["id"],
                    "candidate_id": candidate["id"] if candidate else None,
                    "diff": sanitize(patch.decode("utf-8", "replace")),
                }
                if args.json
                else sanitize(patch.decode("utf-8", "replace")),
                args,
            )
            return 0
        if cmd == "finding":
            from .review.fix import resolve_finding

            emit(
                finding_decision(db, args.run_id, args.finding_id, args.status, args.evidence or args.reason)
                if args.status
                else resolve_finding(db, args.run_id, args.finding_id),
                args,
            )
            return 0
        if cmd == "confirm":
            emit(requirement_decision(db, args.run_id, args.requirement, args.status, args.evidence), args)
            return 0
        if cmd == "answer":
            from .develop.workflow import answer, read_spec

            emit(answer(db, args.run_id, read_spec(args.body_file)), args)
            return 0
        if cmd == "apply":
            from .git_snapshot import apply_candidate

            preview = apply_candidate(
                db,
                args.run_id,
                dry_run=True,
                allow_unaccepted=args.allow_unaccepted,
                candidate_id=args.candidate,
            )
            if args.dry_run:
                emit(preview if args.json else preview["patch"], args)
                return 0
            if args.allow_unaccepted:
                print(
                    (
                        "Перенос непринятого результата: "
                        + t(db.run(args.run_id)["outcome"], "ru")
                        + "; изучите ошибки в отчёте."
                    )
                    if args.lang == "ru"
                    else "Unaccepted override: "
                    + db.run(args.run_id)["outcome"]
                    + "; inspect report failures.",
                    file=sys.stderr,
                )
            yes_or_prompt(
                args,
                preview["patch"]
                + (
                    "\nПрименить к исходному рабочему дереву без коммита?"
                    if args.lang == "ru"
                    else "\nApply to source working tree without a commit?"
                ),
            )
            emit(
                apply_candidate(
                    db,
                    args.run_id,
                    dry_run=False,
                    allow_unaccepted=args.allow_unaccepted,
                    candidate_id=preview["candidate_id"],
                ),
                args,
            )
            return 0
        if cmd == "attach":
            from .tmux_backend import Tmux

            seat = db.row(
                "SELECT tmux_session FROM seats WHERE scope_id=? AND role=?", (args.scope_id, args.role)
            )
            if not seat:
                raise UserError("Unknown role; use status")
            return Tmux(db).attach(seat["tmux_session"])
        if cmd in {"pause", "resume", "cancel"}:
            from .supervisor import control

            control(db, args.scope_id, cmd)
            emit(db.scope(args.scope_id), args)
            return 0
        if cmd == "recover":
            from .supervisor import recover, recovery_plan

            emit(recovery_plan(db, args.scope_id) if args.plan else recover(db, args.scope_id), args)
            return 0
        if cmd == "cleanup":
            from .git_snapshot import cleanup_worktree

            condition = "s.state IN ('completed','failed','canceled')"
            values = []
            if args.scope_id:
                condition += " AND s.id=?"
                values.append(args.scope_id)
            if args.older_than_days is not None:
                if args.older_than_days < 1:
                    raise UserError("Retention must be >= 1 day")
                condition += " AND julianday('now')-julianday(s.created_at)>=?"
                values.append(args.older_than_days)
            rows = db.rows(
                "SELECT w.* FROM worktrees w JOIN scopes s ON s.id=w.scope_id WHERE "
                + condition
                + " AND w.state='ready'",
                values,
            )
            dry = args.dry_run or not args.yes
            result = []
            for row in rows:
                try:
                    result.append(cleanup_worktree(db, row["id"], dry_run=dry))
                except UserError as exc:
                    result.append({"worktree_id": row["id"], "blocked": str(exc)})
            emit({"dry_run": dry, "worktrees": result}, args)
            return 0
        if cmd == "send":
            from .develop.workflow import read_spec
            from .queue import Queue

            seat = db.row("SELECT * FROM seats WHERE id=?", (args.seat,))
            if not seat:
                raise UserError("Unknown seat address; use team ps/status")
            mid = Queue(db).send(
                seat["scope_id"], "user", args.seat, read_spec(args.body_file), args.idempotency_key or uid()
            )
            emit(
                {
                    "message_id": mid,
                    "state": db.row("SELECT state FROM messages WHERE id=?", (mid,))["state"],
                    "verified": "durable inbox only" if args.verify else None,
                },
                args,
            )
            return 0
        if cmd == "team":
            from . import teams

            if args.team_command == "up":
                from .config import load_config
                from .git_snapshot import preflight

                template = teams._template(args.template, args.provider)
                config = load_config(args.cwd, no_checks=True, provider=args.provider)
                config["roles"] = {role["address"]: role for role in template["roles"]}
                config["team_template"] = template
                snap = preflight(args.cwd, "develop")
                check_providers(config)
                trust(db, args.cwd, config, args, snap["changed_files"])
                emit({"team_id": teams.up(db, args.cwd, args.template, args.provider)}, args)
            elif args.team_command == "ps":
                emit(
                    {
                        "teams": db.rows(
                            "SELECT s.*,t.template_name FROM scopes s JOIN teams t ON s.id=t.id ORDER BY s.created_at DESC"
                        )
                    },
                    args,
                )
            elif args.team_command == "stop":
                emit(teams.stop(db, args.team_id, snapshot=args.snapshot), args)
            else:
                emit(teams.resume(db, args.team_id), args)
            return 0
        if cmd == "queue":
            from .queue import Queue

            q = Queue(db)
            action = args.queue_command
            if action == "create":
                from .develop.workflow import read_spec
                from .teams import create_task

                emit({"task_id": create_task(db, args.to, read_spec(args.body_file))}, args)
            elif action == "list":
                sid = args.team or args.run
                if sid:
                    rows = db.rows("SELECT * FROM tasks WHERE scope_id=? ORDER BY created_at", (sid,))
                elif args.to:
                    rows = db.rows("SELECT * FROM tasks WHERE owner_seat=? ORDER BY created_at", (args.to,))
                else:
                    rows = db.rows("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")
                for row in rows:
                    row.pop("body_json", None)
                emit({"tasks": rows}, args)
            elif action == "show":
                emit(db.row("SELECT * FROM tasks WHERE id=?", (args.task_id,)), args)
            elif action == "handoff":
                from .teams import handoff

                emit({"task_id": handoff(db, args.task_id, args.to, args.note)}, args)
            elif action == "block":
                q.block(args.task_id, args.reason, args.remedy)
                emit({"task_id": args.task_id, "state": "blocked"}, args)
            elif action == "complete":
                result = {"no_artifact": args.no_artifact}
                if args.artifact:
                    path = args.artifact.resolve(strict=True)
                    if not path.is_relative_to(db.root) or path.stat().st_size > 20 * 1024 * 1024:
                        raise UserError("Artifact must be an owned bounded state file")
                    result.update(artifact=str(path), sha256=digest(path.read_bytes()))
                q.complete(args.task_id, result)
                emit({"task_id": args.task_id, "state": "done"}, args)
            elif action == "retry":
                from .supervisor import ensure_supervisor
                from .tmux_backend import Tmux

                task = db.row("SELECT * FROM tasks WHERE id=?", (args.task_id,))
                if not task:
                    raise UserError("Unknown task")
                seat = db.row("SELECT * FROM seats WHERE id=?", (task["owner_seat"],))
                if Tmux(db).alive(seat["tmux_session"]):
                    raise UserError("Old worker is still alive; inspect/stop it before retry")
                from .processes import active_attempt

                live = active_attempt(db.root / "runs" / task["scope_id"] / "tasks" / task["id"])
                if live:
                    raise UserError(
                        f"Old provider processes are still alive: {canonical(live)}. Inspect and stop them before retry",
                        "process_unverified",
                    )
                q.retry(args.task_id, inspected=args.inspect_writer)
                db.set_state("seats", seat["id"], "stopped")
                ensure_supervisor(db)
                emit({"task_id": args.task_id, "state": "pending"}, args)
            return 0
        if cmd == "tui":
            from .tui import run_tui

            run_tui(db, args.lang)
            return 0
        raise UserError("Unknown command")
    finally:
        db.close()


def language_for(argv):
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--lang")
    probe.add_argument("--cwd", type=Path, default=Path.cwd())
    known, _ = probe.parse_known_args(argv)
    if known.lang:
        return known.lang
    path = known.cwd / ".agentcrew.toml"
    try:
        if path.is_file() and path.stat().st_size <= MAX_JSON:
            locale = tomllib.loads(path.read_text(encoding="utf-8")).get("language")
            if type(locale) is str and locale in {"en", "ru"}:
                return locale
    except (OSError, ValueError):
        pass  # A read-only command must not fail because optional locale configuration is broken.
    return "en"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser(language_for(argv)).parse_args(argv)
    try:
        return dispatch(args)
    except UserError as exc:
        detail = f"{exc.code}: {redact(str(exc))}"
        if args.lang == "ru":
            detail = f"Ошибка: {detail}. Проверьте agentcrew doctor и agentcrew recover --plan."
        print(detail, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(
            f"process_error: {redact(str(exc))}; проверьте doctor/recover --plan; состояние сохранено"
            if args.lang == "ru"
            else f"process_error: {redact(str(exc))}; inspect doctor/recover --plan; state was preserved",
            file=sys.stderr,
        )
        return 3
