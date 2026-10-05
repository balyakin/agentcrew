"""Read-only process identity checks. Saved PIDs never authorize a signal."""

import os
import signal
import subprocess
import time
from pathlib import Path

from .models import UserError, load_json


def identity(pid: int) -> dict | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise UserError("Process ownership uncertain; inspect manually", "process_unverified") from exc
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            raw = stat.read_text()
            fields = raw.rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            return {"pid": pid, "start": fields[19], "uid": stat.stat().st_uid}
        except (OSError, IndexError) as exc:
            raise UserError("Process identity unavailable; inspect manually", "process_unverified") from exc
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "uid="],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        return None
    return {"pid": pid, "start": result.stdout.strip(), "uid": os.getuid()}


def active_attempt(root: Path) -> list[dict]:
    active = []
    for path in root.rglob("process.json") if root.exists() else []:
        recorded = load_json(path)
        current = identity(recorded["pid"])
        same_leader = current == {k: recorded[k] for k in ("pid", "start", "uid")}
        survivors = members(recorded["pid"]) if current is None else []
        if same_leader or survivors:
            active.append({**recorded, "path": str(path), "members": survivors})
    return active


def members(pgid: int) -> list[int]:
    """Observe an owned process group, including children of a dead leader."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,uid=,stat="],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode:
        raise UserError("Process group unavailable; inspect manually", "process_unverified")
    found = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[1] == str(pgid) and fields[2] == str(os.getuid()):
            if not fields[3].startswith("Z"):
                found.append(int(fields[0]))
    return found


def stop_group(process: subprocess.Popen, grace: float = 0.3):
    """Signal only a group created by this runner's own Popen, never a saved PID."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # Leader exit does not prove its descendants have finished.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)
    deadline = time.monotonic() + 3
    while members(process.pid):
        if time.monotonic() >= deadline:
            raise UserError("Provider descendants are still active; inspect manually", "process_unverified")
        time.sleep(0.02)
