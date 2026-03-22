"""Security-focused tests for browser_type."""

import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_browser_type_schema_supports_secret_env_var_without_combinators():
    from tools.browser_tool import _BROWSER_SCHEMA_MAP

    params = _BROWSER_SCHEMA_MAP["browser_type"]["parameters"]

    assert params["type"] == "object"
    assert "oneOf" not in params
    assert "anyOf" not in params
    assert "allOf" not in params
    assert params["required"] == ["ref"]
    assert "secret_env_var" in params["properties"]


def test_browser_type_with_plain_text_returns_safe_result():
    from tools.browser_tool import browser_type

    with patch("tools.browser_tool._run_browser_command", return_value={"success": True}) as mock_cmd:
        result = json.loads(browser_type("@e5", text="20/03/2026", task_id="dentidesk"))

    mock_cmd.assert_called_once_with("dentidesk", "fill", ["@e5", "20/03/2026"])
    assert result == {
        "success": True,
        "typed": True,
        "typed_chars": 10,
        "element": "@e5",
    }


def test_browser_type_resolves_secret_env_var_without_returning_secret(monkeypatch):
    from tools.browser_tool import browser_type

    monkeypatch.setenv("DENTIDESK_PASS", "Mikaela4905#")

    with patch("tools.browser_tool._run_browser_command", return_value={"success": True}) as mock_cmd:
        result = json.loads(
            browser_type("@e5", secret_env_var="DENTIDESK_PASS", task_id="dentidesk")
        )

    mock_cmd.assert_called_once_with("dentidesk", "fill", ["@e5", "Mikaela4905#"])
    assert result == {
        "success": True,
        "typed": True,
        "typed_chars": 12,
        "element": "@e5",
        "typed_from_env": "DENTIDESK_PASS",
    }
    assert "Mikaela4905#" not in json.dumps(result)


def test_browser_type_rejects_invalid_input_combinations():
    from tools.browser_tool import browser_type

    missing = json.loads(browser_type("@e5"))
    both = json.loads(browser_type("@e5", text="user", secret_env_var="DENTIDESK_USER"))

    assert missing["success"] is False
    assert "exactly one" in missing["error"]
    assert both["success"] is False
    assert "exactly one" in both["error"]


def test_browser_type_errors_when_secret_env_var_missing():
    from tools.browser_tool import browser_type

    result = json.loads(browser_type("@e5", secret_env_var="DENTIDESK_PASS"))

    assert result["success"] is False
    assert "not set" in result["error"]
