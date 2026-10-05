"""A provider can remain alive after all its pipe streams reach EOF."""

import sys
import time

import pytest

from agentcrew.models import UserError
from agentcrew.providers.base import _execute


def run_closed_stdio(tmp_path, *, delay, timeout, heartbeat=None, canceled=None, started=None):
    script = tmp_path / "provider.py"
    script.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "for fd in (0, 1, 2): os.close(fd)\n"
        "time.sleep(0.15)\n"
        "Path('closed').touch()\n"
        f"time.sleep({delay})\n"
        "Path('completed').touch()\n"
    )
    result_path = tmp_path / "result.json"
    result_path.touch()
    with (tmp_path / "events.jsonl").open("wb") as events, (tmp_path / "provider.log").open("wb") as log:
        return _execute(
            [sys.executable, str(script)],
            tmp_path,
            "test prompt",
            timeout,
            events,
            log,
            result_path,
            heartbeat=heartbeat,
            canceled=canceled,
            started=started,
        )


def test_cancel_after_stdio_eof_stops_provider_before_its_next_write(tmp_path):
    processes = []
    started = time.monotonic()
    with pytest.raises(UserError) as error:
        run_closed_stdio(
            tmp_path,
            delay=1.5,
            timeout=5,
            canceled=lambda: (tmp_path / "closed").exists(),
            started=processes.append,
        )
    assert error.value.code == "provider_canceled"
    assert time.monotonic() - started < 1.3
    assert processes[0].poll() is not None
    assert not (tmp_path / "completed").exists()


def test_heartbeat_continues_after_stdio_eof_until_provider_exits(tmp_path):
    heartbeats_after_eof = []
    code = run_closed_stdio(
        tmp_path,
        delay=1.1,
        timeout=5,
        heartbeat=lambda: heartbeats_after_eof.append((tmp_path / "closed").exists()),
    )
    assert code == 0
    assert (tmp_path / "completed").exists()
    assert sum(heartbeats_after_eof) >= 2


def test_timeout_after_stdio_eof_keeps_heartbeats_and_stops_provider(tmp_path):
    heartbeats_after_eof = []
    processes = []
    started = time.monotonic()
    with pytest.raises(UserError) as error:
        run_closed_stdio(
            tmp_path,
            delay=2.5,
            timeout=1,
            heartbeat=lambda: heartbeats_after_eof.append((tmp_path / "closed").exists()),
            started=processes.append,
        )
    assert error.value.code == "provider_timeout"
    assert time.monotonic() - started < 2
    assert processes[0].poll() is not None
    assert any(heartbeats_after_eof)
    assert not (tmp_path / "completed").exists()


def test_leader_exit_stops_descendant_holding_pipes_and_preserves_buffered_output(tmp_path):
    script = tmp_path / "provider.py"
    script.write_text(
        "import json, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', "
        "\"import time; from pathlib import Path; time.sleep(1.5); Path('descendant-write').touch()\"])\n"
        "print(json.dumps({'type': 'turn.completed'}), flush=True)\n"
    )
    result_path = tmp_path / "result.json"
    result_path.touch()
    started = time.monotonic()
    with (tmp_path / "events.jsonl").open("wb") as events, (tmp_path / "provider.log").open("wb") as log:
        code = _execute([sys.executable, str(script)], tmp_path, "test prompt", 5, events, log, result_path)
    assert code == 0
    assert time.monotonic() - started < 1.3
    assert not (tmp_path / "descendant-write").exists()
    assert (tmp_path / "events.jsonl").read_text().strip() == '{"type": "turn.completed"}'
