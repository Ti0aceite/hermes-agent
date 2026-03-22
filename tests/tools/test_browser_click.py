"""Tests for browser_click link navigation behavior and navigate auto-snapshot."""

import json
import os
import sys
from unittest.mock import ANY, call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def test_browser_select_schema_is_openai_compatible():
    from tools.browser_tool import _BROWSER_SCHEMA_MAP

    params = _BROWSER_SCHEMA_MAP["browser_select"]["parameters"]

    assert params["type"] == "object"
    assert "oneOf" not in params
    assert "anyOf" not in params
    assert "allOf" not in params
    assert params["required"] == ["ref"]


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
                {"success": True, "data": {"value": None}},
                {"success": True},
            ]

            result = json.loads(browser_select("@e1", value="Blue", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e9", "value"]),
            call("dentidesk", "select", ["@e1", "Blue"]),
        ]
        assert result == {"success": True, "selected": "Blue", "element": "@e1"}

    def test_select_with_value_resolves_real_option_value_from_snapshot_label(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e22]:\n'
                            '    - option "Reporte por estado de citas" [ref=e30]'
                        ),
                    },
                },
                {"success": True, "data": {"value": "estado_horas"}},
                {"success": True},
            ]

            result = json.loads(
                browser_select("@e22", value="Reporte por estado de citas", task_id="dentidesk")
            )

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e30", "value"]),
            call("dentidesk", "select", ["@e22", "estado_horas"]),
        ]
        assert result == {"success": True, "selected": "estado_horas", "element": "@e22"}

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
                {"success": True, "data": {"value": None}},
                {"success": True},
            ]

            result = json.loads(browser_select("@e1", option_ref="@e9", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e9", "value"]),
            call("dentidesk", "getattribute", ["@e9", "value"]),
            call("dentidesk", "select", ["@e1", "Blue"]),
        ]
        assert result == {"success": True, "selected": "Blue", "element": "@e1"}

    def test_select_with_option_ref_resolves_label_to_real_value_from_parent_select(self):
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
                {"success": True, "data": {"value": None}},
                {"success": True, "data": {"value": "estado_horas"}},
                {"success": True},
            ]

            result = json.loads(browser_select("@e1", option_ref="@e9", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e9", "value"]),
            call("dentidesk", "getattribute", ["@e9", "value"]),
            call("dentidesk", "select", ["@e1", "estado_horas"]),
        ]
        assert result == {"success": True, "selected": "estado_horas", "element": "@e1"}

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

    def test_select_retries_with_nth_selector_for_ambiguous_combobox(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e17]\n'
                            '  - combobox [ref=e22]\n'
                            '    - option "Reporte por estado de citas" [ref=e30]'
                        ),
                        "refs": {
                            "e17": {"name": "Tipo de reporte:", "role": "combobox"},
                            "e22": {"role": "combobox"},
                        },
                    },
                },
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "ready": true, "matched_value": "estado_horas", "matched_text": "Reporte por estado de citas"}',
                    },
                },
                {
                    "success": False,
                    "error": 'Selector "@e22" matched 2 elements. Run \'snapshot\' to get updated refs, or use a more specific CSS selector.',
                },
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e17]\n'
                            '  - combobox [ref=e22]\n'
                            '    - option "Reporte por estado de citas" [ref=e30]'
                        ),
                        "refs": {
                            "e17": {"name": "Tipo de reporte:", "role": "combobox"},
                            "e22": {"role": "combobox"},
                        },
                    },
                },
                {"success": True},
            ]

            result = json.loads(browser_select("@e22", value="estado_horas", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "eval", [ANY]),
            call("dentidesk", "select", ["@e22", "estado_horas"]),
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "select", ["select >> nth=1", "estado_horas"]),
        ]
        assert result == {"success": True, "selected": "estado_horas", "element": "@e22"}

    def test_select_falls_back_to_dom_eval_after_timeout(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e17]\n'
                            '  - combobox [ref=e22]\n'
                            '    - option "Reporte por estado de citas" [ref=e30]'
                        ),
                        "refs": {
                            "e17": {"name": "Tipo de reporte:", "role": "combobox"},
                            "e22": {"role": "combobox"},
                        },
                    },
                },
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "ready": true, "matched_value": "estado_horas", "matched_text": "Reporte por estado de citas"}',
                    },
                },
                {
                    "success": False,
                    "error": 'Selector "@e22" matched 2 elements. Run \'snapshot\' to get updated refs, or use a more specific CSS selector.',
                },
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e17]\n'
                            '  - combobox [ref=e22]\n'
                            '    - option "Reporte por estado de citas" [ref=e30]'
                        ),
                        "refs": {
                            "e17": {"name": "Tipo de reporte:", "role": "combobox"},
                            "e22": {"role": "combobox"},
                        },
                    },
                },
                {
                    "success": False,
                    "error": "Command timed out after 30 seconds",
                },
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "selected": "estado_horas", "matched_text": "Reporte por estado de citas"}',
                    },
                },
            ]

            result = json.loads(browser_select("@e22", value="estado_horas", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "eval", [ANY]),
            call("dentidesk", "select", ["@e22", "estado_horas"]),
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "select", ["select >> nth=1", "estado_horas"]),
            call("dentidesk", "eval", [ANY]),
        ]
        assert result == {"success": True, "selected": "estado_horas", "element": "@e22"}

    def test_select_with_option_ref_uses_dom_eval_first_for_dependent_dropdown(self):
        from tools.browser_tool import browser_select

        with patch("tools.browser_tool._run_browser_command") as mock_cmd:
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e17]\n'
                            '  - combobox [ref=e22]\n'
                            '    - option "Reporte por estado de citas" [ref=e30]'
                        ),
                    },
                },
                {"success": True, "data": {"value": None}},
                {"success": True, "data": {"value": "estado_horas"}},
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "ready": true, "matched_value": "estado_horas", "matched_text": "Reporte por estado de citas"}',
                    },
                },
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "selected": "estado_horas", "matched_text": "Reporte por estado de citas"}',
                    },
                },
            ]

            result = json.loads(browser_select("@e22", option_ref="@e30", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e30", "value"]),
            call("dentidesk", "getattribute", ["@e30", "value"]),
            call("dentidesk", "eval", [ANY]),
            call("dentidesk", "eval", [ANY]),
        ]
        assert result == {"success": True, "selected": "estado_horas", "element": "@e22"}

    def test_select_waits_for_dependent_dropdown_option_before_selecting(self):
        from tools.browser_tool import browser_select

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.time.sleep", return_value=None),
        ):
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e17]\n'
                            '  - combobox [ref=e22]\n'
                            '    - option "Reporte por estado de citas" [ref=e30]'
                        ),
                    },
                },
                {"success": True, "data": {"value": "estado_horas"}},
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "ready": false, "disabled": true, "matched_value": null}',
                    },
                },
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "ready": true, "disabled": false, "matched_value": "estado_horas"}',
                    },
                },
                {"success": True},
            ]

            result = json.loads(
                browser_select("@e22", value="Reporte por estado de citas", task_id="dentidesk")
            )

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "getattribute", ["@e30", "value"]),
            call("dentidesk", "eval", [ANY]),
            call("dentidesk", "eval", [ANY]),
            call("dentidesk", "select", ["@e22", "estado_horas"]),
        ]
        assert result == {"success": True, "selected": "estado_horas", "element": "@e22"}

    def test_select_fails_fast_when_dependent_dropdown_has_no_options_yet(self):
        from tools.browser_tool import browser_select

        with (
            patch("tools.browser_tool._run_browser_command") as mock_cmd,
            patch("tools.browser_tool.time.sleep", return_value=None),
            patch("tools.browser_tool.time.time", side_effect=[0.0, 10.0]),
        ):
            mock_cmd.side_effect = [
                {
                    "success": True,
                    "data": {
                        "snapshot": (
                            '  - combobox "Tipo de reporte" [ref=e17]\n'
                            '  - combobox [ref=e22]'
                        ),
                    },
                },
                {
                    "success": True,
                    "data": {
                        "result": '{"success": true, "ready": false, "disabled": true, "option_count": 0, "matched_value": null}',
                    },
                },
            ]

            result = json.loads(browser_select("@e22", value="estado_horas", task_id="dentidesk"))

        assert mock_cmd.call_args_list == [
            call("dentidesk", "snapshot", ["-c"]),
            call("dentidesk", "eval", [ANY]),
        ]
        assert result == {
            "success": False,
            "error": (
                "Option estado_horas is not available yet; "
                "the dropdown is still disabled or its options have not loaded."
            ),
        }


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
