import pytest

from agentcrew.models import UserError, load_json, redact, sanitize, validate_schema


def test_strict_json(tmp_path):
    path = tmp_path / "result.json"
    for value in ['{"x":1,"x":2}', '{"x":NaN}', "null garbage"]:
        path.write_text(value)
        with pytest.raises(UserError):
            load_json(path)
    with pytest.raises(UserError):
        validate_schema(True, {"type": "integer"})


def test_terminal_control_and_redaction():
    assert sanitize("\x1b[31mhello\x1b[0m\x00") == "hello"
    assert sanitize("\x1b]52;c;secret\x07hello") == "hello"
    assert "abcd" not in redact("token=abcd")


def test_redaction_covers_quoted_secrets_and_bearer_headers():
    from agentcrew.models import redact

    sample = 'api_key="quotedsecret"; Authorization: Bearer abcdefghijklmnop; {"password": "anothersecret"}'
    result = redact(sample)
    assert "quotedsecret" not in result and "abcdefghijklmnop" not in result and "anothersecret" not in result
