"""Only private-server tmux operations; no workflow policy."""

import os
import re
import shlex
import subprocess
from pathlib import Path

from .models import UserError, sanitize, uid


class Tmux:
    def __init__(self, db):
        with db.transaction():
            instance = db.row("SELECT value FROM meta WHERE key='instance'")
            if instance is None:
                if db.read_only:
                    self.socket = None
                    return
                db.execute("INSERT INTO meta VALUES('instance',?)", (uid(),))
                db.execute("INSERT INTO meta VALUES('uid',?)", (str(os.getuid()),))
                instance = db.row("SELECT value FROM meta WHERE key='instance'")
            if db.row("SELECT value FROM meta WHERE key='uid'")["value"] != str(os.getuid()):
                raise UserError("State belongs to a different UID", "state_owner_mismatch")
        self.socket = f"agentcrew-{os.getuid()}-{instance['value'][:8]}"
        self.validate(self.socket)

    @staticmethod
    def validate(name: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", name):
            raise UserError("Invalid tmux socket/session name")

    def call(self, *args, check=True):
        if self.socket is None:
            raise UserError("No AgentCrew tmux server was initialized", "tmux_unavailable")
        try:
            result = subprocess.run(
                ["tmux", "-L", self.socket, "-f", "/dev/null", *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UserError(f"tmux failed: {exc}; run doctor", "tmux_error", 3) from exc
        if check and result.returncode:
            raise UserError(f"tmux: {sanitize(result.stderr)[:1000]}", "tmux_error", 3)
        return result

    def has_session(self, name: str) -> bool:
        self.validate(name)
        if self.socket is None:
            return False
        return self.call("has-session", "-t", "=" + name, check=False).returncode == 0

    def ensure(self, name: str, argv: list[str], cwd: Path | str) -> str:
        self.validate(name)
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir() or not argv or any("\x00" in arg for arg in argv):
            raise UserError("Invalid tmux command or working directory")
        if not self.has_session(name):
            result = self.call("new-session", "-d", "-s", name, "-c", str(cwd), shlex.join(argv), check=False)
            if result.returncode and not self.has_session(name):
                raise UserError(f"Cannot start tmux session: {sanitize(result.stderr)}", "tmux_error", 3)
            self.call("set-option", "-w", "-t", "=" + name + ":", "remain-on-exit", "on")
        return self.call("display-message", "-p", "-t", "=" + name + ":", "#{pane_id}").stdout.strip()

    def alive(self, name: str) -> bool:
        if not self.has_session(name):
            return False
        return (
            self.call("display-message", "-p", "-t", "=" + name + ":", "#{pane_dead}").stdout.strip() == "0"
        )

    def stop(self, name: str):
        self.validate(name)
        if self.has_session(name):
            self.call("kill-session", "-t", "=" + name, check=False)

    def attach(self, name: str) -> int:
        self.validate(name)
        if not self.has_session(name):
            raise UserError("Session is not running; use recover --plan")
        return subprocess.call(["tmux", "-L", self.socket, "attach-session", "-t", "=" + name])

    def capture(self, name: str) -> str:
        self.validate(name)
        return sanitize(self.call("capture-pane", "-p", "-t", "=" + name + ":", "-S", "-200").stdout)
