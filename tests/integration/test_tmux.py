import sys
import time

from agentcrew.db import Database
from agentcrew.tmux_backend import Tmux


def test_private_tmux_idempotent_and_survives_client(tmp_path):
    db = Database(tmp_path / "state")
    tmux = Tmux(db)
    name = "test-worker"
    try:
        pane = tmux.ensure(
            name,
            [sys.executable, "-c", 'import time; print("worker alive",flush=True); time.sleep(30)'],
            tmp_path,
        )
        assert pane.startswith("%")
        assert tmux.ensure(name, [sys.executable, "-c", "raise SystemExit(5)"], tmp_path) == pane
        assert tmux.has_session(name)
        assert tmux.socket.startswith("agentcrew-")
        assert Tmux(Database(db.root)).socket == tmux.socket
        time.sleep(0.2)
        assert "worker alive" in tmux.capture(name)
        tmux.stop(name)
        assert not tmux.has_session(name)
    finally:
        tmux.stop(name)
