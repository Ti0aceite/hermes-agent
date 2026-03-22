"""Tests for browser_click link navigation behavior and navigate auto-snapshot."""

import json
import os
import sys
from unittest.mock import call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestBrowserClickLinkNavigation:
    def test_relative_href_from_snapshot_uses_browser_navigate(self):
        from tools.browser_tool import browser_click

        navigate_response = json.dumps(
            {
                "success": True,
                "url": "https://app.dentidesk.cl/reportes.php",
                "title": "Reportes",
                "snapshot": '- heading "Reportes"',
                "element_count": 6,
            }
        )

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool._get_session_info", return_value={"last_url": "https://app.dentidesk.cl/"}),
            patch("tools.browser_tool.browser_navigate", return_value=navigate_response) as mock_nav,
        ):
            mock_cmd.return_value = {
                "success": True,
                "data": {
                    "snapshot": '  - link "Reportes" [ref=e7]:\n    - /url: reportes.php',
                },
            }

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_called_once_with(
            "https://app.dentidesk.cl/reportes.php",
            task_id="dentidesk",
        )
        mock_cmd.assert_called_once_with("dentidesk", "snapshot", ["-c"])
        assert result["success"] is True
        assert result["clicked"] == "@e7"
        assert result["navigated"] is True
        assert result["snapshot"] == '- heading "Reportes"'

    def test_absolute_href_from_snapshot_uses_browser_navigate(self):
        from tools.browser_tool import browser_click

        navigate_response = json.dumps(
            {"success": True, "url": "https://app.dentidesk.cl/reportes.php", "title": "Reportes"}
        )

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.browser_navigate", return_value=navigate_response) as mock_nav,
        ):
            mock_cmd.return_value = {
                "success": True,
                "data": {
                    "snapshot": '  - link "Reportes" [ref=e7]:\n    - /url: https://app.dentidesk.cl/reportes.php',
                },
            }

            result = json.loads(browser_click("e7", task_id="dentidesk"))

        mock_nav.assert_called_once_with(
            "https://app.dentidesk.cl/reportes.php",
            task_id="dentidesk",
        )
        mock_cmd.assert_called_once_with("dentidesk", "snapshot", ["-c"])
        assert result["clicked"] == "@e7"
        assert result["navigated"] is True

    def test_anchor_href_uses_normal_click(self):
        from tools.browser_tool import browser_click

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.browser_navigate") as mock_nav,
        ):
            mock_cmd.side_effect = [
                {"success": True, "data": {"snapshot": '  - link "Reportes" [ref=e7]:\n    - /url: "#reports"'}},
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_not_called()
        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "click", ["@e7"]),
        ]
        assert result == {"success": True, "clicked": "@e7"}

    def test_javascript_href_uses_normal_click(self):
        from tools.browser_tool import browser_click

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.browser_navigate") as mock_nav,
        ):
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {"snapshot": '  - link "Reportes" [ref=e7]:\n    - /url: "javascript:void(0)"'},
                },
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_not_called()
        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "click", ["@e7"]),
        ]
        assert result == {"success": True, "clicked": "@e7"}

    def test_snapshot_failure_falls_back_to_getattribute_then_click(self):
        from tools.browser_tool import browser_click

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.browser_navigate") as mock_nav,
        ):
            mock_cmd.side_effect = [
                {"success": False, "error": "No ref map"},
                {"success": False, "error": "No ref map"},
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_not_called()
        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e7", "href"]),
            call("dentidesk", "click", ["@e7"]),
        ]
        assert result == {"success": True, "clicked": "@e7"}

    def test_relative_href_without_session_url_uses_runtime_url_lookup(self):
        from tools.browser_tool import browser_click

        navigate_response = json.dumps(
            {"success": True, "url": "https://app.dentidesk.cl/reportes.php", "title": "Reportes"}
        )

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool._get_session_info", return_value={}),
            patch("tools.browser_tool.browser_navigate", return_value=navigate_response) as mock_nav,
        ):
            mock_cmd.side_effect = [
                {"success": True, "data": {"snapshot": '  - link "Reportes" [ref=e7]:\n    - /url: reportes.php'}},
                {"success": True, "data": {"url": "https://app.dentidesk.cl/home.php"}},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_called_once_with(
            "https://app.dentidesk.cl/reportes.php",
            task_id="dentidesk",
        )
        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "url", []),
        ]
        assert result["success"] is True
        assert result["navigated"] is True

    def test_invalid_ref_selector_click_retries_after_refresh(self):
        from tools.browser_tool import browser_click

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool._get_browser_attribute", return_value=None),
        ):
            mock_cmd.side_effect = [
                {"success": True, "data": {"snapshot": '  - button "Guardar" [ref=e7]'}},
                {
                    "success": False,
                    "error": 'locator.click: Unsupported token "@e7" while parsing css selector "@e7"',
                },
                {"success": True, "data": {"snapshot": '  - button "Guardar" [ref=e7]'}},
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "click", ["@e7"]),
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "click", ["@e7"]),
        ]
        assert result == {"success": True, "clicked": "@e7"}


class TestBrowserSelect:
    def test_select_with_value_hydrates_snapshot_before_selecting(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": '  - combobox "Color" [ref=e1]:\n    - option "Blue" [ref=e9]',
                    },
                },
                {"success": True},
            ]

            result = json.loads(browser_select("@e1", value="Blue", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "select", ["@e1", "Blue"]),
        ]
        assert result == {"success": True, "selected": "Blue", "element": "@e1"}

    def test_select_with_option_ref_resolves_value_from_snapshot(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e1]:\n'
                            '    - option "Reporte por estado de citas" [ref=e9]'
                        ),
                    },
                },
                {"success": True, "data": {"value": "estado_horas"}},
                {"success": True},
            ]

            result = json.loads(browser_select("@e1", option_ref="@e9", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e9", "value"]),
            call("dentidesk", "select", ["@e1", "estado_horas"]),
        ]
        assert result == {"success": True, "selected": "estado_horas", "element": "@e1"}

    def test_select_with_option_ref_falls_back_to_snapshot_text(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": '  - combobox "Color" [ref=e1]:\n    - option "Blue" [ref=e9]',
                    },
                },
                {"success": True, "data": {"value": None}},
                {"success": True},
            ]

            result = json.loads(browser_select("@e1", option_ref="@e9", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e9", "value"]),
            call("dentidesk", "select", ["@e1", "Blue"]),
        ]
        assert result == {"success": True, "selected": "Blue", "element": "@e1"}

    def test_select_retries_after_invalid_ref_selector_error(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {"success": True, "data": {"snapshot": '  - combobox "Color" [ref=e1]'}},
                {
                    "success": False,
                    "error": 'locator.selectOption: Unsupported token "@e1" while parsing css selector "@e1"',
                },
                {"success": True, "data": {"snapshot": '  - combobox "Color" [ref=e1]'}},
                {"success": True},
            ]

            result = json.loads(browser_select("@e1", value="Blue", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "select", ["@e1", "Blue"]),
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "select", ["@e1", "Blue"]),
        ]
        assert result == {"success": True, "selected": "Blue", "element": "@e1"}


class TestBrowserNavigateAutoSnapshot:
    def test_navigation_includes_compact_snapshot(self):
        from tools.browser_tool import browser_navigate

        session_info = {"_first_nav": False, "features": {"proxies": True}}
        with (
            patch("tools.browser_tool._get_session_info", return_value=session_info),
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
        ):
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "title": "Reportes",
                        "url": "https://app.dentidesk.cl/reportes.php",
                    },
                },
                {
                    "success": True,
                    "data": {
                        "snapshot": '- heading "Reportes"',
                        "refs": {"e1": {"role": "heading"}},
                    },
                },
            ]

            result = json.loads(browser_navigate("https://app.dentidesk.cl/reportes.php", task_id="dentidesk"))

        assert result["success"] is True
        assert result["snapshot"] == '- heading "Reportes"'
        assert result["element_count"] == 1
        assert mock_cmd.call_args_list == [
            call("dentidesk", "open", ["https://app.dentidesk.cl/reportes.php"], timeout=60),
            call("dentidesk", "snapshot", ["-c"]),
        ]

    def test_navigation_still_succeeds_when_snapshot_fails(self):
        from tools.browser_tool import browser_navigate

        session_info = {"_first_nav": False}
        with (
            patch("tools.browser_tool._get_session_info", return_value=session_info),
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
        ):
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "title": "Reportes",
                        "url": "https://app.dentidesk.cl/reportes.php",
                    },
                },
                {"success": False, "error": "Snapshot failed"},
            ]

            result = json.loads(browser_navigate("https://app.dentidesk.cl/reportes.php", task_id="dentidesk"))

        assert result["success"] is True
        assert result["url"] == "https://app.dentidesk.cl/reportes.php"
        assert "snapshot" not in result
