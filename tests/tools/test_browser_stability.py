"""Tests for snapshot stabilization and dynamic browser_type recovery."""

import json
import os
import sys
from unittest.mock import ANY, call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_browser_snapshot_schema_supports_stabilize():
    from tools.browser_tool import _BROWSER_SCHEMA_MAP

    params = _BROWSER_SCHEMA_MAP["browser_snapshot"]["parameters"]

    assert params["type"] == "object"
    assert "stabilize" in params["properties"]
    assert params["properties"]["stabilize"]["default"] is False


def test_browser_snapshot_without_stabilize_preserves_legacy_shape():
    from tools.browser_tool import browser_snapshot

    with patch("tools.browser_tool._run_browser_command", return_value={
        "success": True,
        "data": {
            "snapshot": '- heading "Reportes"',
            "refs": {"e1": {"role": "heading"}},
        },
    }) as mock_cmd:
        result = json.loads(browser_snapshot(full=True, task_id="dentidesk"))

    mock_cmd.assert_called_once_with("dentidesk", "snapshot", [])
    assert result == {
        "success": True,
        "snapshot": '- heading "Reportes"',
        "element_count": 1,
    }


def test_browser_snapshot_with_stabilize_returns_richest_success():
    from tools.browser_tool import browser_snapshot

    with (
        patch("tools.browser_tool._run_browser_command") as mock_cmd,
        patch("tools.browser_tool.time.sleep") as mock_sleep,
    ):
        mock_cmd.side_effect = [
            {
                "success": True,
                "data": {"snapshot": '- heading "Cargando"', "refs": {"e1": {}}},
            },
            {
                "success": True,
                "data": {
                    "snapshot": '- form "Filtros"\n- textbox "Fecha Inicial"\n- textbox "Fecha Final"',
                    "refs": {"e1": {}, "e2": {}, "e3": {}},
                },
            },
            {
                "success": True,
                "data": {
                    "snapshot": '- tabla de resultados con mas texto pero menos refs',
                    "refs": {"e1": {}, "e2": {}},
                },
            },
        ]

        result = json.loads(browser_snapshot(full=True, task_id="dentidesk", stabilize=True))

    assert mock_cmd.call_args_list == [
        call("dentidesk", "snapshot", []),
        call("dentidesk", "snapshot", []),
        call("dentidesk", "snapshot", []),
    ]
    assert mock_sleep.call_args_list == [call(1.0), call(2.0)]
    assert result["success"] is True
    assert result["snapshot"] == '- form "Filtros"\n- textbox "Fecha Inicial"\n- textbox "Fecha Final"'
    assert result["element_count"] == 3
    assert result["stabilized"] is True
    assert result["attempt_count"] == 3
    assert result["selected_attempt"] == 2


def test_browser_snapshot_with_stabilize_returns_last_error_when_all_fail():
    from tools.browser_tool import browser_snapshot

    with (
        patch("tools.browser_tool._run_browser_command") as mock_cmd,
        patch("tools.browser_tool.time.sleep"),
    ):
        mock_cmd.side_effect = [
            {"success": False, "error": "Timed out waiting for page"},
            {"success": False, "error": "Snapshot failed"},
            {"success": False, "error": "No page available"},
        ]

        result = json.loads(browser_snapshot(task_id="dentidesk", stabilize=True))

    assert mock_cmd.call_args_list == [
        call("dentidesk", "snapshot", ["-c"]),
        call("dentidesk", "snapshot", ["-c"]),
        call("dentidesk", "snapshot", ["-c"]),
    ]
    assert result == {
        "success": False,
        "error": "No page available",
        "stabilized": True,
        "attempt_count": 3,
        "selected_attempt": None,
    }


def test_browser_type_retries_fill_after_invalid_ref():
    from tools.browser_tool import browser_type

    with patch("tools.browser_tool._run_browser_command") as mock_cmd:
        mock_cmd.side_effect = [
            {"success": False, "error": "Element @e37 not found or not visible"},
            {
                "success": True,
                "data": {
                    "snapshot": '  - textbox "Fecha Inicial" [ref=e37]',
                    "refs": {"e37": {"role": "textbox", "label": "Fecha Inicial"}},
                },
            },
            {"success": True},
        ]

        result = json.loads(browser_type("@e37", text="16/03/2026", task_id="dentidesk"))

    assert mock_cmd.call_args_list == [
        call("dentidesk", "fill", ["@e37", "16/03/2026"]),
        call("dentidesk", "snapshot", ["-c"]),
        call("dentidesk", "fill", ["@e37", "16/03/2026"]),
    ]
    assert result == {
        "success": True,
        "typed": True,
        "typed_chars": 10,
        "element": "@e37",
    }


def test_browser_type_falls_back_to_eval_after_second_retryable_failure():
    from tools.browser_tool import browser_type

    with patch("tools.browser_tool._run_browser_command") as mock_cmd:
        mock_cmd.side_effect = [
            {"success": False, "error": "Element @e37 not found or not visible"},
            {
                "success": True,
                "data": {
                    "snapshot": '  - textbox "Fecha Inicial" [ref=e37]',
                    "refs": {"e37": {"role": "textbox", "label": "Fecha Inicial"}},
                },
            },
            {"success": False, "error": "Timed out waiting to fill field"},
            {"success": True, "data": {"result": json.dumps({"success": True, "resolved_index": 0})}},
        ]

        result = json.loads(browser_type("@e37", text="16/03/2026", task_id="dentidesk"))

    assert mock_cmd.call_args_list == [
        call("dentidesk", "fill", ["@e37", "16/03/2026"]),
        call("dentidesk", "snapshot", ["-c"]),
        call("dentidesk", "fill", ["@e37", "16/03/2026"]),
        call("dentidesk", "eval", [ANY]),
    ]
    assert result == {
        "success": True,
        "typed": True,
        "typed_chars": 10,
        "element": "@e37",
    }


def test_browser_type_eval_fallback_preserves_secret_metadata(monkeypatch):
    from tools.browser_tool import browser_type

    monkeypatch.setenv("DENTIDESK_PASS", "Mikaela4905#")

    with patch("tools.browser_tool._run_browser_command") as mock_cmd:
        mock_cmd.side_effect = [
            {"success": False, "error": "Element @e5 not found or not visible"},
            {
                "success": True,
                "data": {
                    "snapshot": '  - textbox "Password" [ref=e5]',
                    "refs": {"e5": {"role": "textbox", "label": "Password"}},
                },
            },
            {"success": False, "error": "Timed out waiting to fill field"},
            {"success": True, "data": {"result": json.dumps({"success": True, "resolved_index": 0})}},
        ]

        result = json.loads(browser_type("@e5", secret_env_var="DENTIDESK_PASS", task_id="dentidesk"))

    assert mock_cmd.call_args_list[:3] == [
        call("dentidesk", "fill", ["@e5", "Mikaela4905#"]),
        call("dentidesk", "snapshot", ["-c"]),
        call("dentidesk", "fill", ["@e5", "Mikaela4905#"]),
    ]
    assert mock_cmd.call_args_list[3] == call("dentidesk", "eval", [ANY])
    assert result == {
        "success": True,
        "typed": True,
        "typed_chars": 12,
        "element": "@e5",
        "typed_from_env": "DENTIDESK_PASS",
    }
    assert "Mikaela4905#" not in json.dumps(result)
