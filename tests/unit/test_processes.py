import json
import subprocess
import sys

from agentcrew.processes import active_attempt, identity


def test_identity_detects_live_and_dead_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"])
    try:
        process = identity(child.pid)
        assert process and process["pid"] == child.pid
        (tmp_path / "process.json").write_text(json.dumps(process))
        assert active_attempt(tmp_path)
    finally:
        child.terminate()
        child.wait()
    assert not active_attempt(tmp_path)


def test_normal_provider_terminates_detached_stdio_descendants(tmp_path):
    from agentcrew.providers.base import _execute

    code = """import subprocess,sys,json,time
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
open(sys.argv[1],'w').write(str(child.pid))
time.sleep(.15)
print(json.dumps({'type':'turn.completed'}))
"""
    child_path = tmp_path / "child"
    result = tmp_path / "result.json"
    result.write_text("{}")
    with (tmp_path / "events").open("wb") as events, (tmp_path / "log").open("wb") as log:
        assert (
            _execute(
                [sys.executable, "-c", code, str(child_path)], tmp_path, "prompt", 5, events, log, result
            )
            == 0
        )
    child_pid = int(child_path.read_text())
    assert identity(child_pid) is None


def test_recovery_sees_group_after_leader_exits(tmp_path):
    from agentcrew.providers.base import _stop

    code = """import subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
time.sleep(.3)
"""
    leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    record = identity(leader.pid)
    assert record
    (tmp_path / "process.json").write_text(json.dumps(record))
    leader.wait()
    try:
        assert active_attempt(tmp_path)
    finally:
        _stop(leader)
    assert not active_attempt(tmp_path)
