"""Tests for browser_click link navigation behavior and navigate auto-snapshot."""

import json
import os
import sys
from unittest.mock import call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestBrowserClickLinkNavigation:
    def test_relative_href_uses_browser_navigate(self):
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
            patch("tools.browser_tool.browser_navigate", return_value=navigate_response) as mock_nav,
        ):
            mock_cmd.side_effect = [
                {"success": True, "data": {"attribute": "href", "value": "reportes.php"}},
                {"success": True, "data": {"url": "https://app.dentidesk.cl/home.php"}},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_called_once_with(
            "https://app.dentidesk.cl/reportes.php",
            task_id="dentidesk",
        )
        assert mock_cmd.call_args_list == [
            call("dentidesk", "getattribute", ["@e7", "href"]),
            call("dentidesk", "url", []),
        ]
        assert result["success"] is True
        assert result["clicked"] == "@e7"
        assert result["navigated"] is True
        assert result["snapshot"] == '- heading "Reportes"'

    def test_absolute_href_uses_browser_navigate_without_url_lookup(self):
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
                    "attribute": "href",
                    "value": "https://app.dentidesk.cl/reportes.php",
                },
            }

            result = json.loads(browser_click("e7", task_id="dentidesk"))

        mock_nav.assert_called_once_with(
            "https://app.dentidesk.cl/reportes.php",
            task_id="dentidesk",
        )
        mock_cmd.assert_called_once_with("dentidesk", "getattribute", ["@e7", "href"])
        assert result["clicked"] == "@e7"
        assert result["navigated"] is True

    def test_anchor_href_uses_normal_click(self):
        from tools.browser_tool import browser_click

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.browser_navigate") as mock_nav,
        ):
            mock_cmd.side_effect = [
                {"success": True, "data": {"attribute": "href", "value": "#reports"}},
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_not_called()
        assert mock_cmd.call_args_list == [
            call("dentidesk", "getattribute", ["@e7", "href"]),
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
                {"success": True, "data": {"attribute": "href", "value": "javascript:void(0)"}},
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_not_called()
        assert mock_cmd.call_args_list == [
            call("dentidesk", "getattribute", ["@e7", "href"]),
            call("dentidesk", "click", ["@e7"]),
        ]
        assert result == {"success": True, "clicked": "@e7"}

    def test_getattribute_failure_falls_back_to_click(self):
        from tools.browser_tool import browser_click

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.browser_navigate") as mock_nav,
        ):
            mock_cmd.side_effect = [
                {"success": False, "error": "No ref map"},
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_not_called()
        assert mock_cmd.call_args_list == [
            call("dentidesk", "getattribute", ["@e7", "href"]),
            call("dentidesk", "click", ["@e7"]),
        ]
        assert result == {"success": True, "clicked": "@e7"}

    def test_relative_href_without_current_url_falls_back_to_click(self):
        from tools.browser_tool import browser_click

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.browser_navigate") as mock_nav,
        ):
            mock_cmd.side_effect = [
                {"success": True, "data": {"attribute": "href", "value": "reportes.php"}},
                {"success": False, "error": "No page"},
                {"success": True},
            ]

            result = json.loads(browser_click("@e7", task_id="dentidesk"))

        mock_nav.assert_not_called()
        assert mock_cmd.call_args_list == [
            call("dentidesk", "getattribute", ["@e7", "href"]),
            call("dentidesk", "url", []),
            call("dentidesk", "click", ["@e7"]),
        ]
        assert result == {"success": True, "clicked": "@e7"}


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
