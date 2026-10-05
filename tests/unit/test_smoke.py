import pytest

from agentcrew.models import UserError
from agentcrew.smoke import smoke


def test_missing_provider_does_not_create_attestation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(UserError):
        smoke("not-a-provider")
    assert not list(tmp_path.rglob("claude-read-only.json"))
