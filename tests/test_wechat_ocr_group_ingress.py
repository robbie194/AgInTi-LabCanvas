import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import os
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "agentic_tools/wechat_gui_agent/scripts/wechat_ocr_group_ingress.py"
    scripts = str(path.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("wechat_ocr_group_ingress_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WeChatOcrGroupIngressTests(unittest.TestCase):
    def test_read_current_group_submits_multiple_messages_once_and_keeps_polling(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            module.PRIVATE = Path(tmp)
            module.GUI_LOCK = Path(tmp) / "gui.lock"
            config = {
                "chat_name": "OverTree-ABCD", "chatroom_id": "group",
                "personal_group_cloud": {"enabled": True},
            }
            args = types.SimpleNamespace(display=":97", require_unread_badge=False)
            window = types.SimpleNamespace(x=0, y=0, width=916, height=700)
            batch = {"method": "visible_batch", "messages": [
                {"text": "@OverTree 第一问", "method": "native_copy"},
                {"text": "@OverTree 第二问", "method": "native_copy"},
            ]}
            with mock.patch.object(module.gui, "require_tools"), \
                 mock.patch.object(module.gui, "wait_for_main_wechat_window", return_value=window), \
                 mock.patch.object(module, "has_unread_badge", return_value=False), \
                 mock.patch.object(module.gui, "open_target", return_value={"ok": True}) as open_target, \
                 mock.patch.object(module, "ocr_triggered_message", return_value=batch), \
                 mock.patch.object(module, "run_cloud_once", side_effect=lambda _c, s, _r, **_kw: {"state": s, "metrics": {}}) as cloud:
                first = module.scan_group(args, config, Path(tmp) / "config.json")
                second = module.scan_group(args, config, Path(tmp) / "config.json")
            self.assertEqual(first["queued_count"], 2)
            self.assertEqual(second["queued_count"], 0)
            self.assertEqual([r["content"] for r in cloud.call_args_list[0].args[2]],
                             ["@OverTree 第一问", "@OverTree 第二问"])
            self.assertEqual(cloud.call_args_list[1].args[2], [])
            self.assertTrue(open_target.call_args.kwargs["prefer_current"])
            self.assertFalse(open_target.call_args.kwargs["allow_search"])
            saved = module.load_state(Path(tmp) / "OverTree-ABCD-ocr.state.json")
            self.assertEqual(saved["ocr_last_scan_method"], "visible_batch")
            self.assertGreater(saved["ocr_last_scan_at"], 0)

    def test_seen_history_does_not_replay_after_cooldown_or_restart(self):
        module = load_module()
        state = {"ocr_seen_fingerprints": ["seen"], "ocr_fingerprint_times": {"seen": 1}}
        with mock.patch.object(module.time, "time", return_value=999999):
            self.assertTrue(module.ocr_fingerprint_is_recent(state, "seen", "old message"))
            self.assertFalse(module.ocr_fingerprint_is_recent(state, "new", "new message"))

    def test_literal_quote_does_not_swallow_later_tesseract_rows(self):
        module = load_module()
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        tsv = header + '5\t1\t1\t1\t1\t10\t10\t20\t20\t95\t"Robbie\n'
        tsv += '5\t1\t2\t1\t1\t10\t50\t100\t20\t95\t@OverTree 新问题\n'
        self.assertEqual([r["text"] for r in module.parse_ocr_lines(tsv)],
                         ['"Robbie', '@OverTree 新问题'])

    def test_misread_code_requires_exact_native_copy_not_character_guessing(self):
        module = load_module()
        line = {"text": "2TTV44W", "top": 10, "left": 10, "right": 160, "bottom": 30}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module.gui, "screenshot"), \
             mock.patch.object(module, "run_command", return_value=types.SimpleNamespace(stdout="")), \
             mock.patch.object(module, "parse_ocr_lines", return_value=[line]), \
             mock.patch.object(module, "copy_text_bubble", return_value="2TJTV44W") as copy, \
             mock.patch.object(module, "copy_code_word", return_value=""):
            config = {"personal_group_cloud": {"trigger_prefixes": ["@OverTree"]}}
            window = types.SimpleNamespace(x=0, y=0, width=916, height=700)
            result = module.ocr_triggered_message({}, window, config, Path(tmp), all_messages=True)
            self.assertEqual(result["messages"], [{"text": "2TJTV44W", "method": "ocr_binding_code"}])
            copy.return_value = ""
            result = module.ocr_triggered_message({}, window, config, Path(tmp), all_messages=True)
            self.assertEqual(result["messages"], [])

    def test_native_word_copy_accepts_only_a_complete_code_and_never_escapes(self):
        module = load_module()
        window = types.SimpleNamespace(x=0, y=0, width=916, height=700)
        with mock.patch.object(module, "start_clipboard_probe"), \
             mock.patch.object(module, "stop_clipboard_probe"), \
             mock.patch.object(module.gui, "double_click"), \
             mock.patch.object(module.gui, "run") as run, \
             mock.patch.object(module.time, "sleep"), \
             mock.patch.object(module, "wait_for_clipboard_text", return_value="2TJTV44W") as copied:
            self.assertEqual(module.copy_code_word({}, window, 500, 400), "2TJTV44W")
            copied.return_value = "2TTV44W"
            self.assertEqual(module.copy_code_word({}, window, 500, 400), "")
            copied.return_value = "@OverTree 完整问题"
            self.assertEqual(module.copy_code_word({}, window, 500, 400), "")
        self.assertFalse(any("Escape" in call.args[0] for call in run.call_args_list))

    def test_batch_claims_native_code_before_each_complete_question(self):
        module = load_module()
        lines = [{"text": text, "top": top, "left": 10, "right": 200, "bottom": top + 20}
                 for text, top in [("@OverTree 旧会话", 10), ("ABCD 2345", 50),
                                   ("@OverTree 第一问", 90), ("@OverTree 第二问", 130)]]
        config = {"personal_group_cloud": {"trigger_prefixes": ["@OverTree"]}}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module.gui, "screenshot"), \
             mock.patch.object(module, "run_command", return_value=types.SimpleNamespace(stdout="")), \
             mock.patch.object(module, "parse_ocr_lines", return_value=lines), \
             mock.patch.object(module, "copy_text_bubble", side_effect=["ABCD2345", "@OverTree 第一问\n完整问题", "@OverTree 第二问"]):
            result = module.ocr_triggered_message(
                {}, types.SimpleNamespace(x=0, y=0, width=916, height=700), config, Path(tmp),
                prefer_binding_code=True, all_messages=True,
            )
        self.assertEqual([m["text"] for m in result["messages"]],
                         ["ABCD2345", "@OverTree 第一问\n完整问题", "@OverTree 第二问"])

    def test_discovery_accepts_read_titles_without_accepting_longer_codes_or_preview_text(self):
        module = load_module()
        self.assertEqual(module.registered_titles_from_lines([
            {"text": "OverTree - 2AAC-论文讨论"}, {"text": "OverTree-ABCD (3)"},
            {"text": "OverTree - WXYZ 14:32"},
            {"text": "OverTree-ABCDX"}, {"text": "Robbie: OverTree-WXYZ"},
            {"text": "OverTree-G-7K4Q2M"}, {"text": "OtherGroup"},
        ]), {"OverTree-2AAC", "OverTree-ABCD", "OverTree-WXYZ", "OverTree-G-7K4Q2M"})

    def test_ocr_debug_dump_is_rejected_as_message_text(self):
        module = load_module()
        debug_dump = """@OverTree 请继续处理
2 1 3 0 0 0 108 74 281 27 -1
3 1 3 1 0 0 108 74 281 27 -1
4 1 3 1 1 0 108 74 281 27 -1
5 1 3 1 1 1 108 76 218 25 91.971695 sigmoid_curve.png
"""
        self.assertTrue(module.looks_like_ocr_debug_text(debug_dump))
        self.assertFalse(module.looks_like_ocr_debug_text("@OverTree 请继续处理\n第二行问题"))

    def test_ocr_lines_are_sorted_and_grouped(self):
        module = load_module()
        tsv = """level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t2\t10\t50\t10\t10\t90\tquestion
5\t1\t1\t1\t1\t10\t20\t10\t10\t90\t@OverTree
5\t1\t1\t1\t2\t30\t50\t10\t10\t90\tnow
"""
        lines = module.parse_ocr_lines(tsv)
        self.assertEqual([item["text"] for item in lines], ["@OverTree", "question now"])
        self.assertEqual((lines[0]["left"], lines[0]["top"], lines[0]["right"], lines[0]["bottom"]), (10, 20, 20, 30))
        self.assertEqual((lines[1]["left"], lines[1]["right"]), (10, 40))

    def test_ocr_line_coordinates_map_zoomed_crop_to_screen(self):
        module = load_module()
        tsv = """level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t1\t100\t50\t80\t40\t95\t复制
"""

        lines = module.parse_ocr_lines(tsv, scale=2.5, offset=(700, 400))

        self.assertEqual((lines[0]["left"], lines[0]["top"], lines[0]["right"], lines[0]["bottom"]), (740, 420, 772, 436))

    def test_passive_signature_discards_small_pixel_changes(self):
        module = load_module()
        count = module.PASSIVE_SIGNATURE_SIZE[0] * module.PASSIVE_SIGNATURE_SIZE[1]
        first = bytes([0x20]) * count
        small_change = bytes([0x2F]) * count
        visible_change = bytes([0x30]) * count

        self.assertEqual(
            module.quantized_pixel_signature(first),
            module.quantized_pixel_signature(small_change),
        )
        self.assertNotEqual(
            module.quantized_pixel_signature(first),
            module.quantized_pixel_signature(visible_change),
        )

    def test_active_scan_runs_on_change_or_bounded_rescan(self):
        module = load_module()
        state = {"screen_signature": "stable", "group_key": "groups", "last_active_scan_at": 1000}

        self.assertEqual(
            module.active_scan_decision(state, "stable", "groups", now=1010, rescan_seconds=180),
            (False, "screen_unchanged"),
        )
        self.assertEqual(
            module.active_scan_decision(state, "changed", "groups", now=1010, rescan_seconds=180),
            (True, "screen_changed"),
        )
        self.assertEqual(
            module.active_scan_decision(state, "stable", "groups", now=1180, rescan_seconds=180),
            (True, "safety_rescan_due"),
        )

    def test_ocr_trigger_uses_native_copy_for_complete_multiline_bubble(self):
        module = load_module()
        tsv = """level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t1\t160\t32\t80\t32\t95\t@OverTree
"""
        module.gui.screenshot = mock.Mock()
        module.run_command = mock.Mock(return_value=type("Result", (), {"stdout": tsv})())
        module.copy_text_bubble = mock.Mock(return_value="@OverTree 请分析第一部分\n并完整处理第二部分内容")
        window = type("Window", (), {"x": 10, "y": 20, "width": 1000, "height": 800})()
        config = {
            "personal_group_cloud": {"trigger_prefixes": ["@OverTree"]},
            "chat_region": {"left": 100, "top": 50, "right": 0, "bottom": 0},
        }

        with tempfile.TemporaryDirectory() as tmp:
            result = module.ocr_triggered_message({"DISPLAY": ":97"}, window, config, Path(tmp))

        self.assertEqual(result["method"], "native_copy")
        self.assertEqual(result["text"], "@OverTree 请分析第一部分\n并完整处理第二部分内容")
        module.copy_text_bubble.assert_called_once_with(
            {"DISPLAY": ":97"}, window, 265, 111, evidence=Path(tmp)
        )

    def test_ocr_trigger_rejects_native_copy_tesseract_dump(self):
        module = load_module()
        tsv = """level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t1\t160\t32\t80\t32\t95\t@OverTree
"""
        debug_dump = """@OverTree 请继续处理
2 1 3 0 0 0 108 74 281 27 -1
3 1 3 1 0 0 108 74 281 27 -1
4 1 3 1 1 0 108 74 281 27 -1
5 1 3 1 1 1 108 76 218 25 91.971695 sigmoid_curve.png
"""
        module.gui.screenshot = mock.Mock()
        module.run_command = mock.Mock(return_value=type("Result", (), {"stdout": tsv})())
        module.copy_text_bubble = mock.Mock(return_value=debug_dump)
        window = type("Window", (), {"x": 10, "y": 20, "width": 1000, "height": 800})()
        config = {
            "personal_group_cloud": {"trigger_prefixes": ["@OverTree"]},
            "chat_region": {"left": 100, "top": 50, "right": 0, "bottom": 0},
        }

        with tempfile.TemporaryDirectory() as tmp:
            result = module.ocr_triggered_message({"DISPLAY": ":97"}, window, config, Path(tmp))

        self.assertEqual(result, {"text": "", "method": "invalid_native_copy"})

    def test_copy_menu_requires_exact_copy_action_near_bubble(self):
        module = load_module()
        lines = [
            {"text": "复制链接", "left": 510, "top": 330, "right": 570, "bottom": 350},
            {"text": "复制", "left": 520, "top": 360, "right": 550, "bottom": 380},
            {"text": "转发", "left": 520, "top": 392, "right": 550, "bottom": 412},
            {"text": "复制", "left": 1200, "top": 400, "right": 1230, "bottom": 420},
        ]

        self.assertEqual(module.find_copy_menu_action(lines, 500, 300), (535, 370))
        self.assertIsNone(module.find_copy_menu_action([lines[0]], 500, 300))
        self.assertIsNone(module.find_copy_menu_action([lines[1]], 500, 300))
        above_cursor_menu = [
            {"text": "复制", "left": 500, "top": 520, "right": 540, "bottom": 540},
            {"text": "转发", "left": 500, "top": 552, "right": 540, "bottom": 572},
        ]
        self.assertEqual(module.find_copy_menu_action(above_cursor_menu, 520, 700), (520, 530))

    def test_native_copy_uses_fresh_clipboard_and_clicks_detected_menu_action(self):
        module = load_module()
        owner = mock.Mock()
        action_lines = [
            {"text": "复制", "left": 500, "top": 330, "right": 540, "bottom": 350},
            {"text": "转发", "left": 500, "top": 370, "right": 540, "bottom": 390},
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module, "start_clipboard_probe", return_value=owner) as start_probe, \
             mock.patch.object(module, "read_x_clipboard", return_value="@OverTree 完整问题"), \
             mock.patch.object(module, "stop_clipboard_probe") as stop_probe, \
             mock.patch.object(module, "parse_ocr_lines", return_value=action_lines), \
             mock.patch.object(module, "run_command", return_value=type("Result", (), {"stdout": "tsv"})()), \
             mock.patch.object(module.gui, "run", return_value=type("Result", (), {"stdout": "1200 800"})()) as run, \
             mock.patch.object(module.gui, "screenshot") as screenshot, \
             mock.patch.object(module.gui, "click") as click, \
             mock.patch.object(module.time, "sleep"):
            result = module.copy_text_bubble(
                {"DISPLAY": ":97"},
                type("Window", (), {"x": 0, "y": 0, "width": 1200, "height": 800})(),
                480,
                300,
                evidence=Path(tmp),
            )

        self.assertEqual(result, "@OverTree 完整问题")
        self.assertTrue(start_probe.called)
        self.assertEqual(run.call_args_list[0].args[0], ["xdotool", "mousemove", "480", "300"])
        self.assertEqual(run.call_args_list[1].args[0], ["xdotool", "click", "3"])
        self.assertEqual(run.call_args_list[2].args[0], ["xdotool", "getdisplaygeometry"])
        self.assertEqual(click.call_args.args[1:], (520, 340))
        self.assertEqual(screenshot.call_count, 1)
        stop_probe.assert_called_once_with(owner)

    def test_native_copy_refuses_stale_clipboard_value(self):
        module = load_module()
        owner = mock.Mock()
        action_lines = [
            {"text": "Copy", "left": 500, "top": 330, "right": 540, "bottom": 350},
            {"text": "Forward", "left": 500, "top": 370, "right": 540, "bottom": 390},
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module, "start_clipboard_probe", return_value=owner) as start_probe, \
             mock.patch.object(module, "read_x_clipboard") as read_clipboard, \
             mock.patch.object(module, "stop_clipboard_probe"), \
             mock.patch.object(module, "parse_ocr_lines", return_value=action_lines), \
             mock.patch.object(module, "run_command", return_value=type("Result", (), {"stdout": "tsv"})()), \
             mock.patch.object(module.gui, "run"), \
             mock.patch.object(module.gui, "screenshot"), \
             mock.patch.object(module.gui, "click"), \
             mock.patch.object(module.time, "sleep"):
            read_clipboard.side_effect = lambda _env: start_probe.call_args.args[1]
            result = module.copy_text_bubble(
                {"DISPLAY": ":97"},
                type("Window", (), {"x": 0, "y": 0, "width": 1200, "height": 800})(),
                480,
                300,
                evidence=Path(tmp),
            )

        self.assertEqual(result, "")

    def test_clipboard_copy_waits_for_fresh_value_and_rejects_sentinel(self):
        module = load_module()
        values = iter(["__marker__", "__marker__", "full copied message"])
        with mock.patch.object(module, "read_x_clipboard", side_effect=lambda _env: next(values)), \
             mock.patch.object(module.time, "sleep"):
            result = module.wait_for_clipboard_text({}, "__marker__", timeout=1.0)

        self.assertEqual(result, "full copied message")

    def test_build_row_uses_private_stable_identity_and_context_fingerprint(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            module.PRIVATE = Path(tmp)
            state = {}
            row, fingerprint = module.build_ocr_row({"chat_name": "客户群"}, state, "@OverTree  请分析")
            self.assertTrue(fingerprint)
            self.assertEqual(row["_message_db"], "ocr://wechat-group")
            self.assertEqual(row["server_id"], "ocr-" + fingerprint)
            self.assertEqual(row["content"], "@OverTree 请分析")
            self.assertEqual(state["ocr_next_local_id"], 1)

    def test_normalization_preserves_newlines_but_fingerprint_normalizes_spacing(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            module.PRIVATE = Path(tmp)
            state = {}
            first, first_fingerprint = module.build_ocr_row(
                {"chat_name": "客户群"}, state, "@OverTree 第一行\n第二行"
            )
            second, second_fingerprint = module.build_ocr_row(
                {"chat_name": "客户群"}, state, "@OverTree   第一行 第二行"
            )

        self.assertEqual(first["content"], "@OverTree 第一行\n第二行")
        self.assertEqual(first_fingerprint, second_fingerprint)
        self.assertEqual(second["content"], "@OverTree 第一行 第二行")

    def test_same_visible_line_is_suppressed_during_cooldown(self):
        module = load_module()
        state = {"ocr_seen_fingerprints": ["abc"], "ocr_fingerprint_times": {"abc": 1000}}
        original_time = module.time.time
        module.time.time = lambda: 1100
        try:
            self.assertTrue(module.ocr_fingerprint_is_recent(state, "abc", "同一条"))
            self.assertFalse(module.ocr_fingerprint_is_recent(state, "def", "另一条"))
        finally:
            module.time.time = original_time

    def test_triggered_text_uses_latest_addressed_line_only(self):
        module = load_module()
        lines = [
            {"text": "@OverTree 旧问题", "top": 10},
            {"text": "普通历史消息", "top": 30},
            {"text": "@OverTree 新问题", "top": 50},
            {"text": "另一条历史消息", "top": 70},
        ]
        module.parse_ocr_lines = lambda _tsv: lines
        module.run_command = lambda *args, **kwargs: type("Result", (), {"stdout": ""})()
        module.gui.screenshot = lambda *_args, **_kwargs: None
        with tempfile.TemporaryDirectory() as tmp:
            result = module.ocr_triggered_text(
                {"DISPLAY": ":97"},
                type("Window", (), {"x": 0, "y": 0, "width": 1200, "height": 800})(),
                {"personal_group_cloud": {"trigger_prefixes": ["@OverTree"]}},
                Path(tmp),
            )
        self.assertEqual(result, "@OverTree 新问题")

    def test_ocr_header_crop_includes_titles_in_narrow_wechat_window(self):
        module = load_module()
        window = type("Window", (), {"x": 502, "y": 206, "width": 916, "height": 666})()
        regions = module.ocr_title_crop_regions(window)
        self.assertEqual(regions[0]["left"], 941)
        self.assertEqual(regions[0]["width"], 300)
        self.assertEqual(regions[0]["top"], 238)

    def test_unaddressed_text_is_ignored(self):
        module = load_module()
        module.parse_ocr_lines = lambda _tsv: [
            {"text": "普通消息", "top": 10},
            {"text": "此群已连接到对应的 OverTree 项目和会话。", "top": 30},
            {"text": "日志 ABCD2345 仅供参考", "top": 50},
        ]
        module.run_command = lambda *args, **kwargs: type("Result", (), {"stdout": ""})()
        module.gui.screenshot = lambda *_args, **_kwargs: None
        with tempfile.TemporaryDirectory() as tmp:
            result = module.ocr_triggered_text(
                {"DISPLAY": ":97"},
                type("Window", (), {"x": 0, "y": 0, "width": 1200, "height": 800})(),
                {"personal_group_cloud": {"trigger_prefixes": ["@OverTree"]}},
                Path(tmp),
            )
        self.assertEqual(result, "")

    def test_newer_binding_code_is_not_masked_by_old_mention(self):
        module = load_module()
        lines = [
            {"text": "@OverTree 旧问题", "top": 10},
            {"text": "ABCD2345", "top": 50},
        ]
        module.parse_ocr_lines = lambda _tsv: lines
        module.run_command = lambda *args, **kwargs: type("Result", (), {"stdout": ""})()
        module.gui.screenshot = lambda *_args, **_kwargs: None
        with tempfile.TemporaryDirectory() as tmp:
            result = module.ocr_triggered_text(
                {"DISPLAY": ":97"},
                type("Window", (), {"x": 0, "y": 0, "width": 1200, "height": 800})(),
                {"personal_group_cloud": {"trigger_prefixes": ["@OverTree"]}},
                Path(tmp),
            )
        self.assertEqual(result, "ABCD2345")

    def test_unbound_group_claims_older_code_before_newer_question(self):
        module = load_module()
        lines = [
            {"text": "ABCD2345", "top": 10},
            {"text": "@OverTree 新问题", "top": 50},
        ]
        module.parse_ocr_lines = lambda _tsv: lines
        module.run_command = lambda *args, **kwargs: type("Result", (), {"stdout": ""})()
        module.gui.screenshot = lambda *_args, **_kwargs: None
        with tempfile.TemporaryDirectory() as tmp:
            result = module.ocr_triggered_message(
                {"DISPLAY": ":97"},
                type("Window", (), {"x": 0, "y": 0, "width": 1200, "height": 800})(),
                {"personal_group_cloud": {"trigger_prefixes": ["@OverTree"]}},
                Path(tmp),
                prefer_binding_code=True,
            )
        self.assertEqual(result, {"text": "ABCD2345", "method": "ocr_binding_code"})

    def test_config_generator_derives_stable_title_group_id(self):
        path = ROOT / "agentic_tools/wechat_gui_agent/scripts/wechat_ocr_group_config.py"
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            env_file = private / "wechat_supervisor.local.env"
            env_file.parent.mkdir()
            env_file.write_text(
                "WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID=agent-1\n"
                "WECHAT_PERSONAL_GROUP_CLOUD_URL=https://cloud.example\n"
                "WECHAT_PERSONAL_GROUP_SHARED_SECRET=" + "s" * 48 + "\n",
                encoding="utf-8",
            )
            result = __import__("subprocess").run(
            [sys.executable, str(path), "--group-name", "客户辅导群", "--env-file", str(env_file)],
            capture_output=True,
            text=True,
            env={**os.environ, "WECHAT_AGENT_PRIVATE_DIR": str(private), "WECHAT_AGENT_ENV_FILE": str(env_file)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config_path = next(private.glob("wechat-group-*-ocr-group.json"))
            self.assertTrue(config_path.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["chatroom_id"].startswith("ocr-title-"))
            self.assertIn(payload["chatroom_id"], env_file.read_text(encoding="utf-8"))

    def test_cloud_client_accepts_ocr_feature_flag_without_enabling_legacy_route(self):
        module = load_module()
        config = {
            "ingress": "ocr_gui",
            "chatroom_id": "ocr-title-demo",
            "personal_group_cloud": {"enabled": True},
        }
        client, group_id = module.run_cloud_once.__module__, None
        # Exercise the adapter's feature gate directly without making a request.
        bridge_path = ROOT / "agentic_tools/wechat_gui_agent/scripts/wechat_personal_group_cloud.py"
        spec = importlib.util.spec_from_file_location("personal_group_cloud_feature_test", bridge_path)
        assert spec and spec.loader
        bridge = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = bridge
        spec.loader.exec_module(bridge)
        fake_env = {
            "WECHAT_OCR_GROUP_ENABLED": "true",
            "WECHAT_PERSONAL_GROUP_ALLOWLIST": "ocr-title-demo",
            "WECHAT_PERSONAL_GROUP_CLOUD_URL": "https://cloud.example",
            "WECHAT_PERSONAL_GROUP_SHARED_SECRET": "s" * 48,
            "WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID": "agent-1",
        }
        client, group_id = bridge.build_client(config, environ=fake_env, opener=lambda *_a, **_k: None)
        self.assertEqual(group_id, "ocr-title-demo")
        self.assertEqual(client.agent_account_id, "agent-1")

    def test_registered_group_sync_builds_exact_title_config_with_stable_cloud_id(self):
        module = load_module()
        group_id = "7e3e4567-e89b-42d3-a456-426614174000"
        fake_client = type("FakeRegistryClient", (), {
            "list_registered_groups": lambda _self, _group_ids=None, **_kwargs: {
                "ok": True,
                "groups": [{"groupId": group_id, "title": "OverTree-G-7K4Q2M", "bindingStatus": "bound"}],
            },
        })()
        cloud = sys.modules["wechat_personal_group_cloud"]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {
                 "WECHAT_OCR_GROUP_REMOTE_SYNC": "true",
                 "WECHAT_OCR_GROUP_REGISTRY_REFRESH_SECONDS": "10",
                 "WECHAT_PERSONAL_GROUP_ALLOWLIST": "legacy-group",
             }), \
             mock.patch.object(module, "PRIVATE", Path(tmp)), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            module._REMOTE_GROUP_CACHE.update({"fetched_at": 0.0, "configs": [], "error": ""})
            configs = module.registered_group_configs(force_refresh=True, visible_titles=[])
            allowlist = os.environ["WECHAT_PERSONAL_GROUP_ALLOWLIST"]

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["chatroom_id"], group_id)
        self.assertEqual(configs[0]["send_target"]["expected_title"], "OverTree-G-7K4Q2M")
        self.assertTrue(configs[0]["send_target"]["allow_search"])
        self.assertIn(group_id, allowlist)
        self.assertIn("legacy-group", allowlist)

    def test_discovery_failure_preserves_bound_registry_reconciliation(self):
        module = load_module()
        group_id = "7e3e4567-e89b-42d3-a456-426614174000"
        client = mock.Mock()
        client.list_registered_groups.return_value = {"ok": True, "groups": [
            {"groupId": group_id, "title": "OverTree-ABCD", "bindingStatus": "bound"},
        ]}
        cloud = sys.modules["wechat_personal_group_cloud"]
        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(module, "visible_registered_group_titles", side_effect=RuntimeError("capture failed")), \
             mock.patch.object(cloud, "build_registry_client", return_value=client):
            configs = module.registered_group_configs(force_refresh=True)
        self.assertEqual(configs[0]["chatroom_id"], group_id)
        self.assertIn("capture failed", module._REMOTE_GROUP_CACHE["discovery_error"])
        client.list_registered_groups.assert_called_once_with([], pending_titles=[])

    def test_registered_compact_group_allows_customer_title_suffix(self):
        module = load_module()
        group_id = "7e3e4567-e89b-42d3-a456-426614174000"
        fake_client = type("FakeRegistryClient", (), {
            "list_registered_groups": lambda _self, _group_ids=None, **_kwargs: {
                "ok": True,
                "groups": [{"groupId": group_id, "title": "OverTree-ABCD", "bindingStatus": "bound"}],
            },
        })()
        cloud = sys.modules["wechat_personal_group_cloud"]
        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            module._REMOTE_GROUP_CACHE.update({"fetched_at": 0.0, "configs": [], "error": ""})
            configs = module.registered_group_configs(force_refresh=True, visible_titles=[])

        target = configs[0]["send_target"]
        self.assertEqual(target["expected_title"], "OverTree-ABCD")
        self.assertEqual(target["expected_title_aliases"], ["OverTree-ABCD-"])
        self.assertTrue(target["allow_search"])

    def test_pending_group_requires_local_id_or_observed_unread_title(self):
        module = load_module()
        group_id = "7e3e4567-e89b-42d3-a456-426614174000"
        fake_client = type("FakeRegistryClient", (), {
            "list_registered_groups": lambda _self, _group_ids=None, **_kwargs: {
                "ok": True,
                "groups": [{"groupId": group_id, "title": "OverTree-ABCD", "bindingStatus": "pending"}],
            },
        })()
        cloud = sys.modules["wechat_personal_group_cloud"]
        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            module._REMOTE_GROUP_CACHE.update({"fetched_at": 0.0, "configs": [], "error": ""})
            configs = module.registered_group_configs(force_refresh=True, visible_titles=[])

        self.assertEqual(configs, [])

        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            configs = module.registered_group_configs(force_refresh=True, visible_titles=["OverTree-ABCD"])
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["chatroom_id"], group_id)
        self.assertEqual(configs[0]["binding_status"], "pending")
        self.assertFalse(configs[0]["send_target"]["allow_search"])
        self.assertEqual(Path(configs[0]["state_path"]).name, f"{group_id}-ocr.state.json")

        local = {group_id: ({"chat_name": "OverTree-ABCD"}, Path("/private/group.json"))}
        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            module._REMOTE_GROUP_CACHE.update({"fetched_at": 0.0, "configs": [], "error": "", "local_group_key": ""})
            configs = module.registered_group_configs(force_refresh=True, local_configs=local, visible_titles=[])

        self.assertEqual(configs[0]["binding_status"], "pending")
        self.assertFalse(configs[0]["send_target"]["allow_search"])

    def test_legacy_bound_group_uses_its_exact_existing_local_title(self):
        module = load_module()
        group_id = "ocr-title-000000000000000000000000"
        local_path = Path("/private/current-group.json")
        fake_client = type("FakeRegistryClient", (), {
            "list_registered_groups": lambda _self, group_ids=None, **_kwargs: {
                "ok": True,
                "requested": group_ids,
                "groups": [{"groupId": group_id, "title": "", "bindingStatus": "bound"}],
            },
        })()
        cloud = sys.modules["wechat_personal_group_cloud"]
        local = {group_id: ({"chat_name": "OvertreeAI 测试", "state_path": "/private/current.state.json"}, local_path)}
        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            module._REMOTE_GROUP_CACHE.update({"fetched_at": 0.0, "configs": [], "error": "", "local_group_key": ""})
            configs = module.registered_group_configs(force_refresh=True, local_configs=local, visible_titles=[])

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["chat_name"], "OvertreeAI 测试")
        self.assertEqual(configs[0]["binding_status"], "bound")
        self.assertTrue(configs[0]["send_target"]["allow_search"])
        self.assertTrue(module.group_id_is_supported(group_id))
        self.assertFalse(module.group_id_is_supported("ocr-title-not-a-stable-hash"))

    def test_cloud_registry_is_the_only_admission_source_for_remote_sync(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module.PRIVATE = root / "private"
            module.PRIVATE.mkdir()
            stale_path = root / "stale.json"
            stale_path.write_text(json.dumps({
                "chatroom_id": "7e3e4567-e89b-42d3-a456-426614174000",
                "chat_name": "OverTree-OLD1",
                "state_path": str(root / "kept-state.json"),
            }), encoding="utf-8")
            live = {
                "chatroom_id": "7e3e4567-e89b-42d3-a456-426614174000",
                "chat_name": "OverTree-LIVE",
                "state_path": str(module.PRIVATE / "live-state.json"),
            }
            args = type("Args", (), {"display": ":97"})()
            with mock.patch.object(module, "remote_group_sync_enabled", return_value=True), \
                 mock.patch.object(module, "registered_group_configs", return_value=[live]), \
                 mock.patch.object(module, "scan_display_groups", return_value=[]) as scan:
                module.scan_once(args, [stale_path])

        prepared = scan.call_args.args[2]
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0][0]["chatroom_id"], live["chatroom_id"])
        self.assertEqual(prepared[0][0]["state_path"], str(root / "kept-state.json"))

    def test_cloud_registry_failure_does_not_fall_back_to_stale_local_groups(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            stale_path = Path(tmp) / "stale.json"
            stale_path.write_text(json.dumps({
                "chatroom_id": "7e3e4567-e89b-42d3-a456-426614174000",
                "chat_name": "OverTree-OLD1",
            }), encoding="utf-8")
            args = type("Args", (), {"display": ":97"})()
            with mock.patch.object(module, "remote_group_sync_enabled", return_value=True), \
                 mock.patch.object(module, "registered_group_configs", side_effect=RuntimeError("registry unavailable")), \
                 mock.patch.object(module, "scan_display_groups") as scan:
                result = module.scan_once(args, [stale_path])

        self.assertIn("registry unavailable", result[0]["error"])
        scan.assert_not_called()

    def test_registered_group_sync_ignores_titles_outside_generated_name_policy(self):
        module = load_module()
        cloud = sys.modules["wechat_personal_group_cloud"]
        fake_client = type("FakeRegistryClient", (), {
            "list_registered_groups": lambda _self, _group_ids=None, **_kwargs: {
                "ok": True,
                "groups": [{"groupId": "7e3e4567-e89b-42d3-a456-426614174000", "title": "Customer private group"}],
            },
        })()
        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            module._REMOTE_GROUP_CACHE.update({"fetched_at": 0.0, "configs": [], "error": ""})
            self.assertEqual(module.registered_group_configs(force_refresh=True, visible_titles=[]), [])

    def test_only_ocr_group_replies_select_visual_gui_delivery(self):
        module = load_module()
        captured = []
        direct = types.ModuleType("wechat_direct_chatops")
        direct.send_gui_message = lambda config, message: captured.append((config, message)) or "sent"
        with mock.patch.dict(sys.modules, {"wechat_direct_chatops": direct}):
            self.assertEqual(module.send_gui_message({"ingress": "ocr_gui"}, "回复"), "sent")
            self.assertEqual(module.send_gui_message({"ingress": "direct_chat"}, "回复"), "sent")
        self.assertTrue(captured[0][0]["_visual_only_send"])
        self.assertFalse(captured[1][0]["_visual_only_send"])

    def test_only_bound_registered_group_reply_gets_scoped_search_for_delivery(self):
        module = load_module()
        observed = []
        direct = types.ModuleType("wechat_direct_chatops")
        direct.send_gui_message = lambda config, _message: observed.append(
            (config["chat_name"], os.environ.get("WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH"))
        ) or "sent"
        registered = {
            "ingress": "ocr_gui",
            "binding_status": "bound",
            "chat_name": "OverTree-ABCD",
            "chatroom_id": "7e3e4567-e89b-42d3-a456-426614174000",
            "group_registration_id": "7e3e4567-e89b-42d3-a456-426614174000",
            "send_target": {"expected_title": "OverTree-ABCD", "allow_search": True},
        }
        pending = {**registered, "binding_status": "pending"}
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(sys.modules, {"wechat_direct_chatops": direct}):
            module.send_gui_message(registered, "reply")
            module.send_gui_message(pending, "reply")
            restored = os.environ.get("WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH")

        self.assertEqual(observed, [("OverTree-ABCD", "1"), ("OverTree-ABCD", None)])
        self.assertIsNone(restored)

    def test_unchanged_screen_path_polls_cloud_without_touching_wechat_gui(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            private.mkdir()
            module.PRIVATE = private
            config = {
                "enabled": True,
                "ingress": "ocr_gui",
                "chat_name": "OvertreeAI 测试",
                "chatroom_id": "ocr-title-test",
                "state_path": str(private / "group.state.json"),
                "send_target": {"name": "OvertreeAI 测试", "expected_title": "OvertreeAI 测试"},
                "personal_group_cloud": {"enabled": True, "trigger_prefixes": ["@OverTree"]},
            }
            args = type("Args", (), {"display": ":97", "require_unread_badge": False})()
            expected = {"state": {}, "metrics": {"idle": True}}

            with mock.patch.object(module, "run_cloud_once", return_value=expected) as poll_cloud, \
                 mock.patch.object(module.gui, "require_tools") as require_tools:
                result = module.scan_group(args, config, Path(tmp) / "group.json", scan_gui=False)

        self.assertEqual(result["extraction_method"], "not_scanned")
        poll_cloud.assert_called_once()
        self.assertEqual(poll_cloud.call_args.args[2], [])
        require_tools.assert_not_called()

    def test_exhausted_outbound_send_is_reported_as_monitor_error(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            private.mkdir()
            module.PRIVATE = private
            config = {
                "enabled": True,
                "ingress": "ocr_gui",
                "chat_name": "OvertreeAI 测试",
                "chatroom_id": "ocr-title-test",
                "state_path": str(private / "group.state.json"),
                "send_target": {"name": "OvertreeAI 测试", "expected_title": "OvertreeAI 测试"},
                "personal_group_cloud": {"enabled": True, "trigger_prefixes": ["@OverTree"]},
            }
            args = type("Args", (), {"display": ":97", "require_unread_badge": False})()
            failure = "Outbound ack item ack-1 abandoned after 4 attempt(s): GUI unavailable"

            with mock.patch.object(module, "run_cloud_once", return_value={
                "state": {"personal_group_cloud_delivery_failures": [{"id": "ack-1"}]},
                "metrics": {"send_error": failure, "outbox": 2},
            }):
                result = module.scan_group(args, config, private / "group.json", scan_gui=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], failure)
        self.assertEqual(result["metrics"]["outbox"], 2)

    def test_passive_screen_failure_still_polls_cloud_without_gui_scan(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            private.mkdir()
            module.PRIVATE = private
            module.GUI_LOCK = private / "wechat_gui_send.lock"
            args = type("Args", (), {"display": ":97", "active_rescan_seconds": 180})()
            prepared = [({"chat_name": "客户群"}, Path(tmp) / "group.json")]

            with mock.patch.object(module, "observe_passive_screen", side_effect=RuntimeError("capture failed")), \
                 mock.patch.object(module, "scan_group", return_value={"ok": True, "chat": "客户群"}) as scan:
                results = module.scan_display_groups(args, ":97", prepared)

        self.assertEqual(len(results), 2)
        scan.assert_called_once_with(
            args,
            prepared[0][0],
            prepared[0][1],
            scan_gui=False,
            seed_only=False,
        )

    def test_initial_scan_seeds_existing_mention_without_cloud_submission(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            private.mkdir()
            module.PRIVATE = private
            module.GUI_LOCK = private / "wechat_gui_send.lock"
            config = {
                "enabled": True,
                "ingress": "ocr_gui",
                "chat_name": "OvertreeAI 测试",
                "chatroom_id": "ocr-title-test",
                "state_path": str(private / "group.state.json"),
                "send_target": {"name": "OvertreeAI 测试", "expected_title": "OvertreeAI 测试"},
                "personal_group_cloud": {"enabled": True, "trigger_prefixes": ["@OverTree"]},
            }
            args = type("Args", (), {"display": ":97", "require_unread_badge": False})()
            cloud_result = {"state": {}, "metrics": {"idle": True}}
            window = type("Window", (), {"x": 0, "y": 0, "width": 916, "height": 700})()

            with mock.patch.object(module.gui, "require_tools"), \
                 mock.patch.object(module.gui, "wait_for_main_wechat_window", return_value=window), \
                 mock.patch.object(module, "has_unread_badge", return_value=False), \
                 mock.patch.object(module.gui, "open_target", return_value={"ok": True}), \
                 mock.patch.object(module, "ocr_triggered_message", return_value={
                     "text": "@OverTree 第一行\n第二行", "method": "native_copy",
                 }), \
                 mock.patch.object(module, "run_cloud_once", return_value=cloud_result) as poll_cloud:
                result = module.scan_group(
                    args, config, private / "group.json", scan_gui=True, seed_only=True
                )

            saved = json.loads((private / "group.state.json").read_text(encoding="utf-8"))

        self.assertTrue(result["seeded"])
        self.assertFalse(result["submitted"])
        self.assertEqual(poll_cloud.call_args.args[2], [])
        self.assertEqual(len(saved["ocr_seen_fingerprints"]), 1)

    def test_registered_group_red_badge_scan_never_uses_global_search(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            private.mkdir()
            module.PRIVATE = private
            module.GUI_LOCK = private / "wechat_gui_send.lock"
            config = {
                "enabled": True,
                "ingress": "ocr_gui",
                "chat_name": "OverTree-G-RMQG7F",
                "chatroom_id": "7e3e4567-e89b-42d3-a456-426614174000",
                "group_registration_id": "7e3e4567-e89b-42d3-a456-426614174000",
                "state_path": str(private / "group.state.json"),
                "send_target": {
                    "name": "OverTree-G-RMQG7F",
                    "query": "OverTree-G-RMQG7F",
                    "expected_title": "OverTree-G-RMQG7F",
                    "allow_search": False,
                },
                "personal_group_cloud": {"enabled": True},
            }
            args = type("Args", (), {"display": ":97", "require_unread_badge": False})()
            window = type("Window", (), {"x": 0, "y": 0, "width": 916, "height": 700})()
            global_search_at_open = []
            def reject_opened_target(*_args, **_kwargs):
                global_search_at_open.append((
                    os.environ.get("WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH"),
                    module.gui.global_search_allowed(True, module.gui.target_from_raw(config["send_target"])),
                ))
                return {
                    "ok": False,
                    "method": "search_disabled",
                    "search_disabled": True,
                }

            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(module.gui, "require_tools"), \
                 mock.patch.object(module.gui, "wait_for_main_wechat_window", return_value=window), \
                 mock.patch.object(module, "has_unread_badge", return_value=True), \
                 mock.patch.object(module.gui, "open_target", side_effect=reject_opened_target) as open_target, \
                 mock.patch.object(module, "ocr_triggered_message") as extract_message, \
                 mock.patch.object(module, "run_cloud_once", return_value={"state": {}, "metrics": {}}) as poll_cloud:
                result = module.scan_group(args, config, private / "group.json", scan_gui=True)
                search_setting_restored = os.environ.get("WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "title_guard_failed")
        self.assertFalse(open_target.call_args.kwargs["allow_search"])
        self.assertEqual(global_search_at_open, [(None, False)])
        self.assertFalse(open_target.call_args.kwargs["skip_title_guard"])
        self.assertTrue(open_target.call_args.kwargs["fail_closed_after_visible_match"])
        extract_message.assert_not_called()
        poll_cloud.assert_called_once()
        self.assertIsNone(search_setting_restored)

    def test_opt_in_badge_only_mode_skips_read_group(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            private.mkdir()
            module.PRIVATE = private
            config = {
                "enabled": True,
                "ingress": "ocr_gui",
                "chat_name": "OverTree-G-ABCD",
                "chatroom_id": "7e3e4567-e89b-42d3-a456-426614174000",
                "group_registration_id": "7e3e4567-e89b-42d3-a456-426614174000",
                "binding_status": "bound",
                "state_path": str(private / "group.state.json"),
                "send_target": {"name": "OverTree-G-ABCD", "expected_title": "OverTree-G-ABCD", "allow_search": True},
                "personal_group_cloud": {"enabled": True},
            }
            args = type("Args", (), {"display": ":97", "require_unread_badge": True})()
            window = type("Window", (), {"x": 0, "y": 0, "width": 916, "height": 700})()
            with mock.patch.object(module.gui, "require_tools"), \
                 mock.patch.object(module.gui, "wait_for_main_wechat_window", return_value=window), \
                 mock.patch.object(module, "has_unread_badge", return_value=False), \
                 mock.patch.object(module.gui, "open_target") as open_target, \
                 mock.patch.object(module, "ocr_triggered_message") as extract, \
                 mock.patch.object(module, "run_cloud_once", return_value={"state": {}, "metrics": {}}):
                result = module.scan_group(args, config, private / "group.json", scan_gui=True)

        self.assertEqual(result["extraction_method"], "no_unread_badge")
        open_target.assert_not_called()
        extract.assert_not_called()

    def test_pending_discovery_reads_only_generated_titles_with_their_own_badge(self):
        module = load_module()
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (300, 420), "white")
        draw = ImageDraw.Draw(image)
        # Unrelated/quiet rows do not inherit this row's unread marker.
        draw.ellipse((25, 94, 34, 103), fill=(230, 30, 45))
        tsv = "level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        for index, (title, top) in enumerate([
            ("OverTree-2AAC-论文讨论", 90),
            ("OverTree-ABCD", 160),
            ("OtherChat", 230),
            ("OverTree-ABCDX", 300),
        ], 1):
            tsv += f"5\t1\t{index}\t1\t1\t65\t{top}\t160\t20\t95\t{title}\n"
        self.assertEqual(module.unread_registered_titles_from_snapshot(tsv, image), ["OverTree-2AAC"])
        draw.ellipse((25, 304, 34, 313), fill=(230, 30, 45))
        self.assertEqual(module.unread_registered_titles_from_snapshot(tsv, image), ["OverTree-2AAC"])
        # Support the previous group format, including OCR whitespace splits.
        tsv += "5\t1\t5\t1\t1\t65\t370\t160\t20\t95\tOverTree - G - 7K4Q2M\n"
        draw.ellipse((25, 374, 34, 383), fill=(230, 30, 45))
        self.assertEqual(module.unread_registered_titles_from_snapshot(tsv, image), ["OverTree-2AAC", "OverTree-G-7K4Q2M"])

    def test_visible_discovery_never_navigates_or_searches(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module, "GUI_LOCK", Path(tmp) / "lock"), \
             mock.patch.object(module.gui, "wait_for_main_wechat_window", return_value=types.SimpleNamespace(width=916, height=700)), \
             mock.patch.object(module, "unread_chat_list_snapshot", return_value=("tsv", "image")), \
             mock.patch.object(module, "unread_registered_titles_from_snapshot", return_value=["OverTree-2AAC"]), \
             mock.patch.object(module.gui, "open_target") as navigate:
            self.assertEqual(module.visible_unread_group_titles(":97"), ["OverTree-2AAC"])
        navigate.assert_not_called()

    def test_red_badge_detection_requires_a_compact_connected_marker(self):
        module = load_module()
        from PIL import Image, ImageDraw

        badge = Image.new("RGB", (120, 44), "white")
        ImageDraw.Draw(badge).ellipse((82, 8, 89, 15), fill=(230, 30, 45))
        text_pixels = Image.new("RGB", (120, 44), "white")
        draw = ImageDraw.Draw(text_pixels)
        for x in range(10, 100, 8):
            draw.point((x, 11), fill=(230, 30, 45))

        self.assertTrue(module.red_badge_component_present(badge, min_pixels=12))
        self.assertFalse(module.red_badge_component_present(text_pixels, min_pixels=12))

    def test_group_search_is_disabled_even_when_a_legacy_target_requests_it(self):
        module = load_module()
        target = module.gui.target_from_raw({
            "name": "OverTree-G-RMQG7F",
            "query": "OverTree-G-RMQG7F",
            "expected_title": "OverTree-G-RMQG7F",
            "allow_search": True,
        })
        config = {
            "ingress": "ocr_gui",
            "chat_name": "OverTree-G-RMQG7F",
            "chatroom_id": "7e3e4567-e89b-42d3-a456-426614174000",
        }

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(module.gui.global_search_allowed(target.allow_search, target))
            self.assertNotIn("WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH", os.environ)


if __name__ == "__main__":
    unittest.main()
