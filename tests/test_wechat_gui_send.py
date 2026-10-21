import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_wechat_gui_send():
    path = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_gui_send.py"
    spec = importlib.util.spec_from_file_location("wechat_gui_send_for_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    scripts_dir = str(path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WeChatGuiSendTests(unittest.TestCase):
    def test_title_crop_adapts_to_narrow_main_wechat_window(self):
        module = load_wechat_gui_send()
        window = module.Window("main", 502, 206, 916, 666)

        regions = module.title_crop_regions(window)

        self.assertEqual(regions[0]["label"], "main_right_header_compact")
        self.assertEqual(regions[0]["left"], 941)
        self.assertEqual(regions[0]["width"], 300)
        self.assertLessEqual(regions[0]["height"], 50)
        self.assertEqual(regions[1]["label"], "main_right_header_adaptive")
        self.assertEqual(regions[2]["label"], "main_right_header_adaptive_high")

    def test_focus_synchronizes_x_focus_before_raise(self):
        module = load_wechat_gui_send()
        commands: list[list[str]] = []

        with (
            mock.patch.object(
                module,
                "run",
                side_effect=lambda command, **_kwargs: commands.append(command)
                or subprocess.CompletedProcess(command, 0, "", ""),
            ),
            mock.patch.object(module.time, "sleep"),
        ):
            module.focus({}, module.Window("123", 0, 0, 1000, 700))

        self.assertEqual(
            commands,
            [
                ["xdotool", "windowfocus", "--sync", "123"],
                ["xdotool", "windowraise", "123"],
            ],
        )

    def test_search_clicks_center_of_compact_search_field(self):
        module = load_wechat_gui_send()
        calls: list[tuple[str, object]] = []
        window = module.Window("main", 502, 206, 916, 666)

        with (
            mock.patch.object(module, "click", side_effect=lambda _env, x, y: calls.append(("click", (x, y)))),
            mock.patch.object(module, "hotkey", side_effect=lambda _env, name: calls.append(("hotkey", name))),
            mock.patch.object(module, "key", side_effect=lambda _env, name: calls.append(("key", name))),
            mock.patch.object(module, "paste_text", side_effect=lambda _env, text: calls.append(("paste", text))),
            mock.patch.object(module.time, "sleep"),
        ):
            module.search_for_target({}, window, "OverTree-5UMH", 0)

        self.assertEqual(calls[0], ("click", (620, 266)))
        self.assertIn(("hotkey", "ctrl+a"), calls)
        self.assertIn(("key", "BackSpace"), calls)
        self.assertIn(("paste", "OverTree-5UMH"), calls)

    def test_open_target_uses_ocr_search_result_before_fixed_rows(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="OverTree-5UMH",
            query="OverTree-5UMH",
            expected_title="OverTree-5UMH",
            expected_title_aliases=("OverTree-5UMH-",),
            allow_search=True,
        )
        window = module.Window("main", 502, 206, 916, 666)
        search_match = {"text": "OverTree-5UMH 自测", "click_x": 170, "click_y": 192}
        search_calls: list[str] = []
        visible_matches = iter([None, search_match])

        with (
            mock.patch.dict(module.os.environ, {"WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH": "1"}),
            mock.patch.object(module, "click_visible_chat_list_match", side_effect=lambda *_args, **_kwargs: next(visible_matches)),
            mock.patch.object(module, "search_for_target", side_effect=lambda _env, _window, query, _pause: search_calls.append(query)),
            mock.patch.object(module, "screenshot"),
            mock.patch.object(module, "title_window_candidates", return_value=[window]),
            mock.patch.object(module, "verify_opened_title", return_value={
                "ok": True,
                "method": "search_result_ocr",
                "ocr_text": "OverTree-5UMH 自测",
                "compose_window": module.window_to_dict(window),
            }),
            mock.patch.object(module, "click"),
            mock.patch.object(module.time, "sleep"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            result = module.open_target(
                {}, window, target, 0, Path(tmp), "search-result-test", False,
                False, True, fail_closed_after_visible_match=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "search_result_ocr")
        self.assertEqual(search_calls, ["OverTree-5UMH"])
        self.assertEqual(result["search_result_match"], search_match)

    def test_gui_send_lock_waits_for_current_sender_then_acquires(self):
        module = load_wechat_gui_send()
        lock = object()
        attempts = [BlockingIOError(), None]
        ticks = iter([0.0, 0.0, 0.1, 0.1])

        def fake_flock(_lock, _mode):
            outcome = attempts.pop(0)
            if outcome is not None:
                raise outcome

        with (
            mock.patch.object(module.fcntl, "flock", side_effect=fake_flock),
            mock.patch.object(module.time, "monotonic", side_effect=lambda: next(ticks)),
            mock.patch.object(module.time, "sleep") as sleep_mock,
        ):
            module.acquire_gui_send_lock(lock, timeout_seconds=1.0)

        sleep_mock.assert_called_once()

    def test_gui_send_lock_times_out_without_blocking_forever(self):
        module = load_wechat_gui_send()
        lock = object()
        ticks = iter([0.0, 1.0])
        with (
            mock.patch.object(module.fcntl, "flock", side_effect=BlockingIOError),
            mock.patch.object(module.time, "monotonic", side_effect=lambda: next(ticks)),
        ):
            with self.assertRaisesRegex(SystemExit, "WECHAT_SEND_BUSY"):
                module.acquire_gui_send_lock(lock, timeout_seconds=0.5)

    def test_main_window_wait_ignores_startup_splash(self):
        module = load_wechat_gui_send()
        windows = [
            module.Window("splash", 0, 0, 420, 320),
            module.Window("main", 0, 0, 1020, 739),
        ]
        with (
            mock.patch.object(module, "find_wechat_window", side_effect=windows),
            mock.patch.object(module.time, "sleep"),
        ):
            selected = module.wait_for_main_wechat_window({}, timeout=2)

        self.assertEqual(selected, windows[-1])

    def test_main_window_wait_returns_small_window_after_timeout(self):
        module = load_wechat_gui_send()
        splash = module.Window("splash", 0, 0, 420, 320)
        ticks = iter([0.0, 0.0, 1.0, 1.0])
        with (
            mock.patch.object(module, "find_wechat_window", return_value=splash),
            mock.patch.object(module.time, "monotonic", side_effect=lambda: next(ticks)),
            mock.patch.object(module.time, "sleep"),
        ):
            selected = module.wait_for_main_wechat_window({}, timeout=1)

        self.assertEqual(selected, splash)

    def test_file_send_requires_explicit_send_flag(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "report.pdf"
            file_path.write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(
                sys,
                "argv",
                [
                    "wechat_gui_send.py",
                    "--target",
                    "EchoMind",
                    "--file",
                    str(file_path),
                ],
            ):
                with self.assertRaisesRegex(SystemExit, "--file requires --send"):
                    module.main()

    def test_guarded_file_helper_checks_lock_and_visible_send_change(self):
        module = load_wechat_gui_send()
        calls: list[list[str]] = []
        lock_checks: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "report.pdf"
            file_path.write_bytes(b"%PDF-1.4\n")

            def fake_screenshot(_env, path):
                path = Path(path)
                calls.append(["screenshot", path.name])
                path.write_bytes(path.name.encode("utf-8"))

            def fake_lock(_env, _window, screenshot_path, _crop_path):
                lock_checks.append(Path(screenshot_path).name)
                return {"locked": False, "ocr_text": ""}

            with (
                mock.patch.object(module, "focus"),
                mock.patch.object(module, "screenshot", side_effect=fake_screenshot),
                mock.patch.object(module, "detect_wechat_locked", side_effect=fake_lock),
                mock.patch.object(module, "clear_composer"),
                mock.patch.object(module, "click") as click,
                mock.patch.object(
                    module,
                    "wait_for_verified_file_chooser",
                    return_value=module.WindowIdentity("chooser", "Open File", "GtkFileChooserDialog"),
                ),
                mock.patch.object(module, "paste_path_into_file_chooser"),
                mock.patch.object(module, "wait_for_wechat_focus_after_picker"),
                mock.patch.object(
                    module,
                    "verify_opened_title",
                    return_value={"ok": True, "method": "file_selected"},
                ),
                mock.patch.object(module.time, "sleep"),
            ):
                result = module.send_file_to_open_chat(
                    {},
                    module.Window("main", 0, 0, 1000, 700),
                    module.TargetSpec(
                        name="EchoMind",
                        query="EchoMind",
                        expected_title="EchoMind",
                    ),
                    file_path,
                    root,
                    "guarded",
                    pause=0.1,
                )

        self.assertEqual(result["status"], "sent-file-submitted")
        self.assertEqual(result["filename"], "report.pdf")
        self.assertEqual(click.call_args_list[0].args[1:], (456, 666))
        self.assertEqual(
            lock_checks,
            [
                "guarded-file-preflight.png",
                "guarded-file-selected.png",
                "guarded-file-sent.png",
            ],
        )

    def test_guarded_file_helper_rejects_chooser_closing_without_sent_bubble(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "report.pdf"
            file_path.write_bytes(b"%PDF-1.4\n")

            def fake_screenshot(_env, path):
                name = Path(path).name
                Path(path).write_bytes(b"selected" if "selected" in name else b"preflight")

            with (
                mock.patch.object(module, "focus"),
                mock.patch.object(module, "screenshot", side_effect=fake_screenshot),
                mock.patch.object(module, "detect_wechat_locked", return_value={"locked": False, "ocr_text": ""}),
                mock.patch.object(module, "clear_composer"),
                mock.patch.object(module, "click"),
                mock.patch.object(module, "wait_for_verified_file_chooser", return_value=module.WindowIdentity("chooser", "Select File", "wechat wechat")),
                mock.patch.object(module, "paste_path_into_file_chooser"),
                mock.patch.object(module, "wait_for_wechat_focus_after_picker"),
                mock.patch.object(module, "verify_opened_title", return_value={"ok": True, "method": "file_selected"}),
                mock.patch.object(module.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "WECHAT_FILE_SEND_VERIFY_FAILED"):
                    module.send_file_to_open_chat(
                        {}, module.Window("main", 0, 0, 1000, 700),
                        module.TargetSpec(name="EchoMind", query="EchoMind", expected_title="EchoMind"),
                        file_path, root, "chooser-stuck", pause=0.1,
                    )

    def test_guarded_file_helper_rejects_target_change_after_picker(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "report.pdf"
            file_path.write_bytes(b"%PDF-1.4\n")

            def fake_screenshot(_env, path):
                path = Path(path)
                path.write_bytes(path.name.encode("utf-8"))

            with (
                mock.patch.object(module, "focus"),
                mock.patch.object(module, "screenshot", side_effect=fake_screenshot),
                mock.patch.object(
                    module,
                    "detect_wechat_locked",
                    return_value={"locked": False, "ocr_text": ""},
                ),
                mock.patch.object(module, "clear_composer"),
                mock.patch.object(module, "click") as click,
                mock.patch.object(
                    module,
                    "wait_for_verified_file_chooser",
                    return_value=module.WindowIdentity("chooser", "Open File", "GtkFileChooserDialog"),
                ),
                mock.patch.object(module, "paste_path_into_file_chooser"),
                mock.patch.object(module, "wait_for_wechat_focus_after_picker"),
                mock.patch.object(
                    module,
                    "verify_opened_title",
                    return_value={"ok": False, "method": "file_selected"},
                ),
                mock.patch.object(module.time, "sleep"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "WECHAT_FILE_TARGET_CHANGED",
                ):
                    module.send_file_to_open_chat(
                        {},
                        module.Window("main", 0, 0, 1000, 700),
                        module.TargetSpec(
                            name="EchoMind",
                            query="EchoMind",
                            expected_title="EchoMind",
                        ),
                        file_path,
                        root,
                        "guarded",
                        pause=0.1,
                    )

        self.assertEqual(click.call_count, 1)

    def test_file_send_never_pastes_path_without_verified_chooser(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "private-report.pdf"
            file_path.write_bytes(b"%PDF-1.4\n")

            def fake_screenshot(_env, path):
                Path(path).write_bytes(Path(path).name.encode("utf-8"))

            with (
                mock.patch.object(module, "focus"),
                mock.patch.object(module, "screenshot", side_effect=fake_screenshot),
                mock.patch.object(
                    module,
                    "detect_wechat_locked",
                    return_value={"locked": False, "ocr_text": ""},
                ),
                mock.patch.object(module, "clear_composer"),
                mock.patch.object(module, "click"),
                mock.patch.object(
                    module,
                    "wait_for_verified_file_chooser",
                    side_effect=RuntimeError("WECHAT_FILE_CHOOSER_NOT_OPEN"),
                ),
                mock.patch.object(module, "paste_path_into_file_chooser") as paste_path,
                mock.patch.object(module.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "WECHAT_FILE_CHOOSER_NOT_OPEN"):
                    module.send_file_to_open_chat(
                        {},
                        module.Window("main", 0, 0, 1000, 700),
                        module.TargetSpec(
                            name="EchoMind",
                            query="EchoMind",
                            expected_title="EchoMind",
                        ),
                        file_path,
                        root,
                        "fail-closed",
                        pause=0.1,
                    )

        paste_path.assert_not_called()

    def test_file_chooser_identity_must_be_distinct_and_native(self):
        module = load_wechat_gui_send()
        main = module.Window("main", 0, 0, 1000, 700)

        self.assertFalse(
            module.is_verified_file_chooser(
                module.WindowIdentity("main", "WeChat", "wechat"),
                main,
            )
        )
        self.assertFalse(
            module.is_verified_file_chooser(
                module.WindowIdentity("other", "WeChat", "wechat"),
                main,
            )
        )
        self.assertTrue(
            module.is_verified_file_chooser(
                module.WindowIdentity("chooser", "选择文件", "GtkFileChooserDialog"),
                main,
            )
        )

    def test_file_chooser_wait_scans_visible_windows_without_window_manager(self):
        module = load_wechat_gui_send()
        main = module.Window("main", 0, 0, 1000, 700)
        chooser = module.WindowIdentity(
            "chooser",
            "Open File",
            "GtkFileChooserDialog",
        )

        with (
            mock.patch.object(module, "active_window_identity", return_value=None),
            mock.patch.object(module, "visible_window_identities", return_value=[chooser]),
        ):
            result = module.wait_for_verified_file_chooser({}, main, timeout=0.2)

        self.assertEqual(result, chooser)

    def test_qt_open_dialog_requires_exact_wechat_parent(self):
        module = load_wechat_gui_send()
        main = module.Window("18874384", 0, 0, 1000, 700)
        for identity, expected in (
            (module.WindowIdentity("18874419", "Open", "wechat wechat", "18874384"), True),
            (module.WindowIdentity("18874419", "Open", "wechat wechat", "123"), False),
            (module.WindowIdentity("18874419", "Open", "chrome", "18874384"), False),
            (module.WindowIdentity("18874419", "Open", "wechat wechat"), False),
        ):
            with self.subTest(identity=identity):
                self.assertEqual(module.is_verified_file_chooser(identity, main), expected)

    def test_window_identity_uses_xprop_not_unsupported_xdotool_command(self):
        module = load_wechat_gui_send()
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            output = ('WM_CLASS(STRING) = "wechat", "wechat"\n'
                      'WM_TRANSIENT_FOR(WINDOW): window id # 0x1200010\n') if command[0] == "xprop" else "Open"
            return subprocess.CompletedProcess(command, 0, output, "")

        with mock.patch.object(module, "run", side_effect=fake_run):
            identity = module.read_window_identity({}, "18874419")
        self.assertEqual(identity, module.WindowIdentity("18874419", "Open", "wechat wechat", "18874384"))
        self.assertFalse(any("getwindowclassname" in call for call in calls))

    def test_active_window_falls_back_to_input_focus_without_window_manager(self):
        module = load_wechat_gui_send()
        chooser = module.WindowIdentity("18874419", "Open", "wechat wechat", "18874384")

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "18874419" if "getwindowfocus" in command else "", "")

        with mock.patch.object(module, "run", side_effect=fake_run), \
             mock.patch.object(module, "read_window_identity", return_value=chooser):
            self.assertEqual(module.active_window_identity({}), chooser)

    def test_file_picker_return_refocuses_guarded_window_without_window_manager(self):
        module = load_wechat_gui_send()
        main = module.Window("main", 0, 0, 1000, 700)
        visible_main = module.WindowIdentity("main", "Weixin", "wechat")

        with (
            mock.patch.object(module, "active_window_identity", return_value=None),
            mock.patch.object(
                module,
                "visible_window_identities",
                return_value=[visible_main],
            ),
            mock.patch.object(module, "focus") as focus,
        ):
            module.wait_for_wechat_focus_after_picker({}, main, timeout=0.2)

        focus.assert_called_once_with({}, main)

    def test_download_file_card_reuses_exact_complete_native_cache_file(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached = root / "2026-07" / "bundle.rar"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"Rar!\x1a\x07\x00payload")

            result = module.download_visible_file_card(
                {},
                module.Window("main", 0, 0, 1000, 700),
                "bundle.rar",
                root,
                root / "evidence",
                "probe",
                pause=0.1,
                wait_seconds=1,
                expected_size=cached.stat().st_size,
                expected_md5=module.file_md5(cached),
            )

        self.assertEqual(result["status"], "already-downloaded")
        self.assertEqual(result["downloaded_path"], str(cached.resolve()))

    def test_existing_native_cache_file_must_match_declared_identity(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            cached = Path(tmp) / "bundle.rar"
            cached.write_bytes(b"wrong payload")
            matches = module.exact_download_matches(Path(tmp), "bundle.rar")

            result = module.newest_complete_download(
                matches,
                expected_size=123456,
                expected_md5="0" * 32,
            )

        self.assertIsNone(result)

    def test_file_card_accepts_direct_download_without_popup(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloaded = root / "bundle.rar"
            downloaded.write_bytes(b"Rar!\x1a\x07\x00payload")
            with mock.patch.object(module, "exact_download_matches", return_value={}), mock.patch.object(
                module, "screenshot"
            ), mock.patch.object(
                module,
                "run",
                return_value=subprocess.CompletedProcess(["tesseract"], 0, "", ""),
            ), mock.patch.object(
                module,
                "locate_file_card_from_tsv",
                return_value={"click_x": 800, "click_y": 400, "click_candidates": [[800, 400]]},
            ), mock.patch.object(module, "click"), mock.patch.object(
                module, "wait_for_new_wechat_popup", return_value=None
            ), mock.patch.object(module, "wait_for_exact_download", return_value=downloaded):
                result = module.download_visible_file_card(
                    {},
                    module.Window("main", 0, 0, 1000, 700),
                    "bundle.rar",
                    root,
                    root / "evidence",
                    "probe",
                    pause=0.1,
                    wait_seconds=1,
                )

        self.assertEqual(result["status"], "downloaded-directly")
        self.assertEqual(result["downloaded_path"], str(downloaded))

    def test_file_card_locator_matches_exact_rar_in_right_pane(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t1\t1\t1\t1\t937\t443\t29\t10\t90\t128",
                "5\t1\t1\t1\t1\t2\t987\t443\t31\t11\t90\t光谱",
                "5\t1\t1\t1\t2\t1\t939\t462\t26\t14\t90\t资料",
                "5\t1\t1\t1\t2\t2\t968\t456\t19\t28\t90\t.rar",
                "5\t1\t2\t1\t1\t1\t610\t350\t70\t15\t90\told.jpg",
            ]
        )

        match = module.locate_file_card_from_tsv(
            tsv,
            "c12880光谱仪带数据存配套资料.rar",
            module.Window("main", 489, 193, 1020, 739),
        )

        self.assertIsNotNone(match)
        self.assertGreater(match["identity_score"], 0.35)
        self.assertEqual(match["click_x"], 977)

    def test_file_card_locator_translates_focused_upscaled_coordinates(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t1\t1\t1\t1\t249\t1020\t36\t42\t90\t全彩",
                "5\t1\t1\t1\t1\t2\t291\t1020\t162\t42\t90\t_示例书.pdf",
                "5\t1\t2\t1\t1\t1\t249\t1090\t240\t42\t90\t161.2M",
            ]
        )

        match = module.locate_file_card_from_tsv(
            tsv,
            "全彩_示例书.pdf",
            module.Window("main", 489, 193, 1020, 739),
            offset_x=835,
            offset_y=263,
            coordinate_scale=3.0,
        )

        self.assertIsNotNone(match)
        self.assertGreater(match["identity_score"], 0.7)
        self.assertEqual(match["click_x"], 959)
        self.assertEqual(match["click_y"], 610)

    def test_load_targets_resolves_cli_target_from_registry_mapping(self):
        module = load_wechat_gui_send()
        with tempfile.NamedTemporaryFile("w+", suffix=".json", encoding="utf-8") as handle:
            json.dump(
                {
                    "鏈接": {
                        "name": "鏈接",
                        "query": "鏈接",
                        "expected_title": "鏈接",
                        "expected_title_aliases": ["链接"],
                        "result_click": [165, 100],
                    }
                },
                handle,
                ensure_ascii=False,
            )
            handle.flush()

            targets, message = module.load_targets(["鏈接"], Path(handle.name), "hello")

        self.assertEqual(message, "hello")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].name, "鏈接")
        self.assertEqual(targets[0].expected_title_aliases, ("链接",))
        self.assertEqual(targets[0].result_click, (165, 100))

    def test_target_fallback_clicks_preserve_order_without_duplicates(self):
        module = load_wechat_gui_send()

        target = module.target_from_raw(
            {
                "name": "EchoMind",
                "query": "EchoMind",
                "expected_title": "EchoMind",
                "expected_title_aliases": ["Echo Mind"],
                "allow_title_guard_fallback": True,
                "result_click": [165, 100],
                "fallback_clicks": [[165, 100], [240, 335], [165, 170]],
            }
        )

        self.assertEqual(target.expected_title_aliases, ("Echo Mind",))
        self.assertTrue(target.allow_title_guard_fallback)
        self.assertFalse(target.allow_search)
        self.assertEqual(target.fallback_clicks, ((165, 100), (240, 335), (165, 170)))
        candidates = module.target_click_candidates(target)
        self.assertEqual(candidates[:4], [
            ("result_click", (165, 100)),
            ("result_click_row_center", (165, 74)),
            ("result_click_title_offset", (200, 74)),
            ("result_click_preview_offset", (200, 100)),
        ])
        self.assertIn(("fallback_click_2", (240, 335)), candidates)
        self.assertIn(("fallback_click_3", (165, 170)), candidates)

    def test_explicit_click_candidates_try_fallback_before_derived_points(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="EchoMind",
            query="EchoMind",
            expected_title="EchoMind",
            result_click=(165, 100),
            fallback_clicks=((165, 170),),
        )

        candidates = module.target_explicit_click_candidates(target)

        self.assertEqual(candidates[0], ("result_click", (165, 100)))
        self.assertEqual(candidates[1], ("fallback_click_1", (165, 170)))
        self.assertIn(("result_click_row_center", (165, 74)), candidates[2:])

    def test_title_guard_does_not_accept_full_page_left_list_match(self):
        module = load_wechat_gui_send()
        calls = []
        original_run = module.run
        try:
            def fake_run(command, *, env, check=True):
                calls.append(command)
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, "blank right pane", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("1", 0, 0, 1000, 700),
                Path("/tmp/screen.png"),
                module.TargetSpec(name="EchoMind", query="EchoMind", expected_title="EchoMind"),
                Path("/tmp/title.png"),
                "current",
            )
        finally:
            module.run = original_run

        self.assertFalse(result["ok"])
        self.assertEqual(sum(1 for call in calls if call[0] == "tesseract"), 3)

    def test_title_guard_accepts_configured_ocr_alias(self):
        module = load_wechat_gui_send()
        original_run = module.run
        try:
            def fake_run(command, *, env, check=True):
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, "SR AEF (5)", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("1", 0, 0, 1000, 700),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="懒人科研",
                    query="懒人科研",
                    expected_title="懒人科研",
                    expected_title_aliases=("SR AEF", "SRAEF"),
                ),
                Path("/tmp/title.png"),
                "current",
            )
        finally:
            module.run = original_run

        self.assertTrue(result["ok"])
        self.assertEqual(result["compose_window"]["width"], 1000)

    def test_title_guard_accepts_chinese_dash_misread_as_one(self):
        module = load_wechat_gui_send()
        original_run = module.run
        try:
            def fake_run(command, *, env, check=True):
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "写作一外语一挣钱 (4) 二\n只 一 口\n+78: Metasurface 2 message",
                        "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("1", 0, 0, 1000, 700),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="写作 外语 挣钱",
                    query="写作",
                    expected_title="写作 外语 挣钱",
                    expected_title_aliases=("写作—外语—挣钱",),
                ),
                Path("/tmp/title.png"),
                "visible_chat_list_ocr",
            )
        finally:
            module.run = original_run

        self.assertTrue(result["ok"])

    def test_title_guard_accepts_popup_chat_window(self):
        module = load_wechat_gui_send()
        original_run = module.run
        crops = []
        try:
            def fake_run(command, *, env, check=True):
                if command[0] == "convert":
                    crops.append(command[3])
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, "🍓我的设备 (4)", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("popup", 649, 206, 623, 666),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                ),
                Path("/tmp/title.png"),
                "result_click_double",
            )
        finally:
            module.run = original_run

        self.assertTrue(result["ok"])
        self.assertEqual(result["compose_window"]["wid"], "popup")
        self.assertTrue(crops)
        self.assertIn("+667+241", crops[0])

    def test_title_guard_prefers_native_window_title(self):
        module = load_wechat_gui_send()
        original_run = module.run
        calls = []
        try:
            def fake_run(command, *, env, check=True):
                calls.append(command)
                if command[:2] == ["xdotool", "getwindowname"]:
                    return subprocess.CompletedProcess(command, 0, "🍓我的设备\n", "")
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, "bad ocr", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("popup", 649, 206, 623, 666),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                ),
                Path("/tmp/title.png"),
                "result_click_double",
            )
        finally:
            module.run = original_run

        self.assertTrue(result["ok"])
        self.assertEqual(result["window_title"], "🍓我的设备")
        self.assertTrue(any(call[0] == "tesseract" for call in calls))

    def test_title_guard_fast_rejects_specific_wrong_native_window_title(self):
        module = load_wechat_gui_send()
        original_run = module.run
        calls = []
        try:
            def fake_run(command, *, env, check=True):
                calls.append(command)
                if command[:2] == ["xdotool", "getwindowname"]:
                    return subprocess.CompletedProcess(command, 0, "EchoMind\n", "")
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, "我的设备", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("main", 489, 193, 1020, 739),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                ),
                Path("/tmp/title.png"),
                "open_click",
            )
        finally:
            module.run = original_run

        self.assertFalse(result["ok"])
        self.assertTrue(result["window_title_nonmatch"])
        self.assertEqual(result["ocr_text"], "EchoMind")
        self.assertFalse(any(call[0] == "tesseract" for call in calls))

    def test_title_guard_rejects_ai_search_native_window_title(self):
        module = load_wechat_gui_send()
        original_run = module.run
        calls = []
        try:
            def fake_run(command, *, env, check=True):
                calls.append(command)
                if command[:2] == ["xdotool", "getwindowname"]:
                    return subprocess.CompletedProcess(command, 0, "AI Search - 我的设备\n", "")
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, "AI Search - 我的设备", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("popup", 649, 206, 623, 666),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                ),
                Path("/tmp/title.png"),
                "result_click_double",
            )
        finally:
            module.run = original_run

        self.assertFalse(result["ok"])
        self.assertEqual(result["surface_reject_reason"], "ai-search")
        self.assertTrue(any(call[0] == "tesseract" for call in calls))

    def test_title_guard_rejects_ai_search_ocr_match(self):
        module = load_wechat_gui_send()
        original_run = module.run
        try:
            def fake_run(command, *, env, check=True):
                if command[:2] == ["xdotool", "getwindowname"]:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, "我的设备 - Search\nAsk a follow-up...\n问AI", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("popup", 649, 206, 623, 666),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                ),
                Path("/tmp/title.png"),
                "result_click_double",
            )
        finally:
            module.run = original_run

        self.assertFalse(result["ok"])
        self.assertEqual(result["surface_reject_reason"], "search-webview")

    def test_title_guard_rejects_matching_title_with_ai_search_surface(self):
        module = load_wechat_gui_send()
        original_run = module.run
        try:
            def fake_run(command, *, env, check=True):
                if command[:2] == ["xdotool", "getwindowname"]:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "tesseract":
                    path = str(command[1])
                    if "-surface-" in path:
                        return subprocess.CompletedProcess(command, 0, "AI Search - 我的设备\nAsk a follow-up...\n问AI", "")
                    return subprocess.CompletedProcess(command, 0, "🍓我的设备", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.verify_opened_title(
                {},
                module.Window("main", 489, 193, 1020, 739),
                Path("/tmp/screen.png"),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                ),
                Path("/tmp/title.png"),
                "result_click_double",
            )
        finally:
            module.run = original_run

        self.assertFalse(result["ok"])
        self.assertEqual(result["surface_reject_reason"], "ai-search")
        self.assertIn("AI Search", result["surface_ocr_text"])

    def test_detect_wechat_locked_from_visible_screen(self):
        module = load_wechat_gui_send()
        original_run = module.run
        try:
            def fake_run(command, *, env, check=True):
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        "Weixin for Linux is locked. Unlock on Phone",
                        "",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            result = module.detect_wechat_locked(
                {},
                module.Window("1", 0, 0, 1000, 700),
                Path("/tmp/screen.png"),
                Path("/tmp/locked.png"),
            )
        finally:
            module.run = original_run

        self.assertTrue(result["locked"])
        self.assertIn("Weixin for Linux is locked", result["ocr_text"])

    def test_relaxed_title_guard_does_not_allow_live_send_by_default(self):
        module = load_wechat_gui_send()
        original_focus = module.focus
        original_screenshot = module.screenshot
        original_open_target = module.open_target
        original_record_event = module.record_event
        try:
            module.focus = lambda *_args, **_kwargs: None
            module.screenshot = lambda _env, path: Path(path).write_bytes(b"screen")
            module.open_target = lambda *_args, **_kwargs: {"ok": False, "method": "current", "ocr_text": "鏈接"}
            module.record_event = lambda **_kwargs: None

            with self.assertRaisesRegex(RuntimeError, "Live sends do not allow relaxed title fallback"):
                module.send_one(
                    {},
                    module.Window("1", 0, 0, 1000, 700),
                    module.TargetSpec(
                        name="🍓我的设备",
                        query="我的设备",
                        expected_title="🍓我的设备",
                        expected_title_aliases=("我的设备",),
                        allow_title_guard_fallback=True,
                    ),
                    "reply",
                    True,
                    False,
                    0,
                    False,
                    True,
                    True,
                    Path("/tmp"),
                    Path("/tmp/wechat-mirror.sqlite"),
                    1,
                )
        finally:
            module.focus = original_focus
            module.screenshot = original_screenshot
            module.open_target = original_open_target
            module.record_event = original_record_event

    def test_run_check_false_handles_missing_gui_tool(self):
        module = load_wechat_gui_send()
        with mock.patch.object(module.subprocess, "run", side_effect=FileNotFoundError("missing")):
            result = module.run(["xdotool", "search", "--class", "wechat"], env={}, check=False)

        self.assertEqual(result.returncode, 127)
        self.assertIn("missing", result.stderr)

    def test_relaxed_title_guard_still_allows_dry_open_review(self):
        module = load_wechat_gui_send()
        original_focus = module.focus
        original_screenshot = module.screenshot
        original_open_target = module.open_target
        original_record_event = module.record_event
        try:
            module.focus = lambda *_args, **_kwargs: None
            module.screenshot = lambda _env, path: Path(path).write_bytes(b"screen")
            module.open_target = lambda *_args, **_kwargs: {"ok": False, "method": "current", "ocr_text": "鏈接"}
            module.record_event = lambda **_kwargs: None

            result = module.send_one(
                {},
                module.Window("1", 0, 0, 1000, 700),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                    allow_title_guard_fallback=True,
                ),
                "reply",
                False,
                False,
                0,
                False,
                True,
                True,
                Path("/tmp"),
                Path("/tmp/wechat-mirror.sqlite"),
                1,
            )
        finally:
            module.focus = original_focus
            module.screenshot = original_screenshot
            module.open_target = original_open_target
            module.record_event = original_record_event

        self.assertEqual(result["status"], "dry-run-opened")

    def test_clear_composer_removes_stale_draft_before_paste(self):
        module = load_wechat_gui_send()
        calls = []
        original_click = module.click
        original_key = module.key
        original_hotkey = module.hotkey
        original_sleep = module.time.sleep
        try:
            module.click = lambda _env, x, y: calls.append(("click", x, y))
            module.key = lambda _env, name: calls.append(("key", name))
            module.hotkey = lambda _env, name: calls.append(("hotkey", name))
            module.time.sleep = lambda _seconds: None

            module.clear_composer({}, module.Window("1", 10, 20, 1000, 700), 0)
        finally:
            module.click = original_click
            module.key = original_key
            module.hotkey = original_hotkey
            module.time.sleep = original_sleep

        self.assertEqual(calls[0], ("click", 670, 640))
        self.assertIn(("key", "Escape"), calls)
        self.assertIn(("hotkey", "ctrl+a"), calls)
        self.assertIn(("key", "BackSpace"), calls)
        self.assertIn(("key", "Delete"), calls)

    def test_open_target_falls_back_after_failed_open_click(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="🍓我的设备",
            query="我的设备",
            expected_title="🍓我的设备",
            expected_title_aliases=("我的设备",),
            open_click=(135, 166),
            result_click=(165, 125),
        )
        original_click = module.click
        original_double_click = module.double_click
        original_screenshot = module.screenshot
        original_verify = module.verify_opened_title
        original_title_candidates = module.title_window_candidates
        original_sleep = module.time.sleep
        original_monotonic = module.time.monotonic
        calls = []
        clock = {"value": 0.0}
        try:
            module.click = lambda _env, x, y: calls.append(("click", x, y))
            module.double_click = lambda _env, x, y: calls.append(("double", x, y))
            module.screenshot = lambda _env, _path: None
            module.title_window_candidates = lambda _env, window: [window]
            module.time.sleep = lambda _seconds: None

            def fake_monotonic():
                clock["value"] += 10.0
                return clock["value"]

            def fake_verify(_env, window, _screenshot, _target, _crop, method):
                calls.append(("verify", method))
                return {
                    "ok": method == "result_click_direct_double",
                    "method": method,
                    "ocr_text": "🍓我的设备" if method == "result_click_direct_double" else "File Transfer",
                    "compose_window": module.window_to_dict(window),
                }

            module.time.monotonic = fake_monotonic
            module.verify_opened_title = fake_verify
            result = module.open_target(
                {},
                module.Window("1", 100, 200, 1000, 700),
                target,
                0,
                Path("/tmp"),
                "wechat-open-target-test",
                False,
                False,
            )
        finally:
            module.click = original_click
            module.double_click = original_double_click
            module.screenshot = original_screenshot
            module.verify_opened_title = original_verify
            module.title_window_candidates = original_title_candidates
            module.time.sleep = original_sleep
            module.time.monotonic = original_monotonic

        self.assertTrue(result["ok"])
        self.assertIn(("verify", "open_click"), calls)
        self.assertIn(("verify", "open_click_double"), calls)
        self.assertIn(("verify", "result_click_direct"), calls)
        self.assertIn(("verify", "result_click_direct_double"), calls)

    def test_open_target_tries_single_click_for_explicit_candidate_before_double(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="EchoMind",
            query="EchoMind",
            expected_title="EchoMind",
            result_click=(165, 100),
        )
        original_click = module.click
        original_double_click = module.double_click
        original_screenshot = module.screenshot
        original_verify = module.verify_opened_title
        original_title_candidates = module.title_window_candidates
        original_sleep = module.time.sleep
        original_monotonic = module.time.monotonic
        calls = []
        clock = {"value": 0.0}
        try:
            module.click = lambda _env, x, y: calls.append(("click", x, y))
            module.double_click = lambda _env, x, y: calls.append(("double", x, y))
            module.screenshot = lambda _env, _path: None
            module.title_window_candidates = lambda _env, window: [window]
            module.time.sleep = lambda _seconds: None

            def fake_monotonic():
                clock["value"] += 10.0
                return clock["value"]

            def fake_verify(_env, window, _screenshot, _target, _crop, method):
                calls.append(("verify", method))
                return {
                    "ok": method == "result_click_direct",
                    "method": method,
                    "ocr_text": "EchoMind" if method == "result_click_direct" else "blank",
                    "compose_window": module.window_to_dict(window),
                }

            module.time.monotonic = fake_monotonic
            module.verify_opened_title = fake_verify
            result = module.open_target(
                {},
                module.Window("1", 100, 200, 1000, 700),
                target,
                0,
                Path("/tmp"),
                "wechat-open-target-single-click-test",
                False,
                False,
                False,
            )
        finally:
            module.click = original_click
            module.double_click = original_double_click
            module.screenshot = original_screenshot
            module.verify_opened_title = original_verify
            module.title_window_candidates = original_title_candidates
            module.time.sleep = original_sleep
            module.time.monotonic = original_monotonic

        self.assertTrue(result["ok"])
        self.assertIn(("verify", "result_click_direct"), calls)
        self.assertNotIn(("verify", "result_click_direct_double"), calls)

    def test_open_target_no_search_never_opens_search_box(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="🍓我的设备",
            query="我的设备",
            expected_title="🍓我的设备",
            expected_title_aliases=("我的设备",),
        )
        original_search = module.search_for_target
        original_key = module.key
        original_screenshot = module.screenshot
        original_verify = module.verify_opened_title
        original_title_candidates = module.title_window_candidates
        original_sleep = module.time.sleep
        original_monotonic = module.time.monotonic
        clock = {"value": 0.0}
        try:
            module.search_for_target = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("search should not open"))  # type: ignore[assignment]
            module.key = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Return should not select search result"))  # type: ignore[assignment]
            module.screenshot = lambda _env, _path: None
            module.title_window_candidates = lambda _env, window: [window]
            module.time.sleep = lambda _seconds: None

            def fake_monotonic():
                clock["value"] += 10.0
                return clock["value"]

            def fake_verify(_env, window, _screenshot, _target, _crop, method):
                return {
                    "ok": False,
                    "method": method,
                    "ocr_text": "鏈接",
                    "compose_window": module.window_to_dict(window),
                }

            module.time.monotonic = fake_monotonic
            module.verify_opened_title = fake_verify
            result = module.open_target(
                {},
                module.Window("1", 100, 200, 1000, 700),
                target,
                0,
                Path("/tmp"),
                "wechat-open-no-search-test",
                False,
                True,
                False,
            )
        finally:
            module.search_for_target = original_search
            module.key = original_key
            module.screenshot = original_screenshot
            module.verify_opened_title = original_verify
            module.title_window_candidates = original_title_candidates
            module.time.sleep = original_sleep
            module.time.monotonic = original_monotonic

        self.assertFalse(result["ok"])
        self.assertTrue(result["search_disabled"])
        self.assertEqual(result["method"], "current")

    def test_visible_chat_list_match_reads_target_row_from_tsv(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t64\t164\t58\t15\t24.3\t懒",
                "5\t1\t10\t1\t1\t2\t88\t160\t9\t28\t96.5\t人",
                "5\t1\t10\t1\t1\t3\t96\t164\t26\t15\t95.7\t科研",
                "5\t1\t11\t1\t1\t1\t267\t168\t25\t8\t86.0\t10:14",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="懒人科研",
                query="懒人科研",
                expected_title="懒人科研",
                expected_title_aliases=("SR AEF",),
            ),
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["text"], "懒人科研")
        self.assertGreater(match["center_y"], 160)

    def test_visible_chat_list_match_survives_unmatched_ocr_quote_in_previous_row(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                '5\t1\t6\t1\t1\t1\t100\t66\t28\t10\t24.9\t"sigmai',
                "5\t1\t7\t1\t1\t1\t65\t111\t139\t13\t92.0\tOverTree-G-4Q8U4K",
                "5\t1\t7\t1\t1\t2\t213\t112\t27\t8\t58.6\t00:47",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="OverTree-G-4Q8U4K",
                query="OverTree-G-4Q8U4K",
                expected_title="OverTree-G-4Q8U4K",
            ),
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["text"], "OverTree-G-4Q8U4K")

    def test_tesseract_words_preserves_unmatched_ocr_quotes(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                '5\t1\t6\t1\t1\t1\t100\t66\t28\t10\t24.9\t"sigmai',
                "5\t1\t7\t1\t1\t1\t65\t111\t139\t13\t92.0\tOverTree-G-4Q8U4K",
            ]
        )

        words = module.tesseract_words(tsv)

        self.assertEqual([item["text"] for item in words], ['"sigmai', "OverTree-G-4Q8U4K"])

    def test_visible_chat_list_match_ignores_avatar_ocr_on_title_line(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t18\t16\t44\t34\t35\tsae",
                "5\t1\t10\t1\t1\t2\t67\t30\t37\t17\t88\t‘@My",
                "5\t1\t10\t1\t1\t3\t108\t32\t49\t11\t92\tdevices",
                "5\t1\t10\t1\t1\t4\t269\t33\t23\t8\t90\t11:58",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="🍓My devices",
                query="My devices",
                expected_title="🍓My devices",
                expected_title_aliases=("My devices",),
                allow_title_guard_fallback=True,
            ),
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["text"], "‘@Mydevices")
        self.assertEqual(match["identity_mode"], "exact")
        self.assertGreaterEqual(match["left"], 45)

    def test_visible_chat_list_match_rejects_preview_that_mentions_target(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t66\t25\t30\t30\t90\t鏈接",
                "5\t1\t10\t1\t2\t1\t156\t120\t113\t24\t90\t753:发送者为陈苗，",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="lachlanchan",
                query="陈苗",
                expected_title="陈苗",
            ),
        )

        self.assertIsNone(match)

    def test_title_identity_matches_traditional_ocr_for_simplified_target(self):
        module = load_wechat_gui_send()

        self.assertTrue(module.title_identity_matches("陳苗", ["陈苗"]))

    def test_registered_group_title_prefix_accepts_customer_suffix(self):
        module = load_wechat_gui_send()

        self.assertTrue(module.title_identity_matches(
            "OverTree-ABCD-论文讨论",
            ["OverTree-ABCD", "OverTree-ABCD-"],
        ))
        self.assertTrue(module.title_identity_matches(
            "OverTree-AB23",
            ["OverTree-AB23", "OverTree-AB23-"],
        ))
        self.assertFalse(module.title_identity_matches(
            "OverTree-ABCE-论文讨论",
            ["OverTree-ABCD", "OverTree-ABCD-"],
        ))

    def test_visible_chat_list_match_accepts_separator_ocr_variant(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t66\t238\t115\t29\t90\t写作一外语一挣钱",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="写作 外语 挣钱",
                query="写作",
                expected_title="写作 外语 挣钱",
                expected_title_aliases=("写作—外语—挣钱",),
            ),
        )

        self.assertIsNotNone(match)

    def test_visible_chat_list_match_accepts_ellipsis_truncated_specific_query(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t15\t1\t1\t1\t66\t242\t48\t15\t92\tMEMO",
                "5\t1\t15\t1\t1\t2\t114\t238\t17\t28\t89\t写",
                "5\t1\t15\t1\t1\t3\t130\t238\t16\t28\t91\t作",
                "5\t1\t15\t1\t1\t4\t146\t238\t14\t28\t96\t一",
                "5\t1\t15\t1\t1\t5\t160\t238\t14\t28\t93\t外",
                "5\t1\t15\t1\t1\t6\t174\t238\t13\t28\t92\t语",
                "5\t1\t15\t1\t1\t7\t186\t238\t13\t28\t68\t…",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="MEMO写作—外语—挣钱",
                query="MEMO写作",
                expected_title="MEMO写作—外语—挣钱",
                expected_title_aliases=("写作 外语 挣钱",),
                allow_title_guard_fallback=True,
            ),
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["identity_mode"], "exact")

    def test_visible_chat_list_match_repairs_one_ocr_character_after_script_normalization(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t66\t238\t188\t29\t90\tMEMO守作一外語一掙錢",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="MEMO写作—外语—挣钱",
                query="MEMO写作",
                expected_title="MEMO写作—外语—挣钱",
                allow_title_guard_fallback=True,
            ),
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["identity_mode"], "ocr-single-substitution")

    def test_visible_chat_list_match_script_normalization_does_not_require_opencc(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t66\t238\t188\t29\t90\tMEMO守作一外語一掙錢",
            ]
        )

        with mock.patch.object(module, "TITLE_T2S", None):
            match = module.visible_chat_list_match_from_tsv(
                tsv,
                module.TargetSpec(
                    name="MEMO写作—外语—挣钱",
                    query="MEMO写作",
                    expected_title="MEMO写作—外语—挣钱",
                    allow_title_guard_fallback=True,
                ),
            )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["identity_mode"], "ocr-single-substitution")

    def test_visible_chat_list_match_rejects_multiple_ocr_substitutions(self):
        module = load_wechat_gui_send()
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t66\t238\t188\t29\t90\tMEMO守业一外語一掙錢",
            ]
        )

        match = module.visible_chat_list_match_from_tsv(
            tsv,
            module.TargetSpec(
                name="MEMO写作—外语—挣钱",
                query="MEMO写作",
                expected_title="MEMO写作—外语—挣钱",
                allow_title_guard_fallback=True,
            ),
        )

        self.assertIsNone(match)

    def test_open_target_clicks_visible_chat_list_match_before_static_rows(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="懒人科研",
            query="懒人科研",
            expected_title="懒人科研",
            result_click=(165, 100),
        )
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t64\t164\t58\t15\t80\t懒",
                "5\t1\t10\t1\t1\t2\t88\t160\t9\t28\t80\t人",
                "5\t1\t10\t1\t1\t3\t96\t164\t26\t15\t80\t科研",
            ]
        )
        original_click = module.click
        original_run = module.run
        original_screenshot = module.screenshot
        original_verify = module.verify_opened_title
        original_title_candidates = module.title_window_candidates
        original_sleep = module.time.sleep
        clicks = []
        try:
            module.click = lambda _env, x, y: clicks.append((x, y))
            module.screenshot = lambda _env, path: Path(path).write_bytes(b"fake screenshot")
            module.title_window_candidates = lambda _env, window: [window]
            module.time.sleep = lambda _seconds: None

            def fake_run(command, *, env, check=True):
                if command[0] == "tesseract":
                    return subprocess.CompletedProcess(command, 0, tsv, "")
                return subprocess.CompletedProcess(command, 0, "", "")

            def fake_verify(_env, window, _screenshot, _target, _crop, method):
                return {
                    "ok": method.startswith("visible_chat_list_ocr"),
                    "method": method,
                    "ocr_text": "懒人科研",
                    "compose_window": module.window_to_dict(window),
                }

            module.run = fake_run
            module.verify_opened_title = fake_verify
            with tempfile.TemporaryDirectory() as tmp:
                result = module.open_target(
                    {},
                    module.Window("1", 100, 200, 1000, 700),
                    target,
                    0,
                    Path(tmp),
                    "wechat-visible-row-test",
                    False,
                    False,
                    False,
                )
        finally:
            module.click = original_click
            module.run = original_run
            module.screenshot = original_screenshot
            module.verify_opened_title = original_verify
            module.title_window_candidates = original_title_candidates
            module.time.sleep = original_sleep

        self.assertTrue(result["ok"])
        self.assertEqual(clicks, [(265, 434)])
        self.assertEqual(result["visible_chat_list_match"]["text"], "懒人科研")

    def test_live_open_retries_exact_visible_row_then_fails_closed(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="写作 外语 挣钱",
            query="写作",
            expected_title="写作 外语 挣钱",
            expected_title_aliases=("写作—外语—挣钱",),
            result_click=(165, 125),
            allow_search=True,
        )
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t65\t238\t120\t29\t80\t写作—外语—挣钱",
            ]
        )
        clicks: list[tuple[int, int]] = []
        methods: list[str] = []

        def fake_run(command, *, env, check=True):
            if command[0] == "tesseract" and command[-1] == "tsv":
                return subprocess.CompletedProcess(command, 0, tsv, "")
            return subprocess.CompletedProcess(command, 0, "", "")

        def fake_verify(_env, window, _screenshot, _target, _crop, method):
            methods.append(method)
            return {
                "ok": False,
                "method": method,
                "ocr_text": "",
                "compose_window": module.window_to_dict(window),
            }

        with (
            mock.patch.object(module, "run", side_effect=fake_run),
            mock.patch.object(module, "screenshot", side_effect=lambda _env, path: Path(path).write_bytes(b"shot")),
            mock.patch.object(module, "title_window_candidates", side_effect=lambda _env, window: [window]),
            mock.patch.object(module, "verify_opened_title", side_effect=fake_verify),
            mock.patch.object(module, "click", side_effect=lambda _env, x, y: clicks.append((x, y))),
            mock.patch.object(module.time, "sleep"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            result = module.open_target(
                {},
                module.Window("1", 100, 200, 1000, 700),
                target,
                0,
                Path(tmp),
                "live-visible-fail-closed",
                False,
                False,
                True,
                fail_closed_after_visible_match=True,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["exact_visible_match_open_failed"])
        self.assertEqual(clicks, [(265, 512), (265, 512)])
        self.assertTrue(any(method.startswith("visible_chat_list_ocr_retry") for method in methods))

    def test_open_target_accepts_visible_row_when_header_ocr_is_noisy_and_relaxed_allowed(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="🍓我的设备",
            query="我的设备",
            expected_title="🍓我的设备",
            expected_title_aliases=("我的设备",),
            allow_title_guard_fallback=True,
        )
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t64\t164\t58\t15\t80\t🍓",
                "5\t1\t10\t1\t1\t2\t88\t164\t58\t15\t80\t我的设备",
            ]
        )
        original_run = module.run
        original_screenshot = module.screenshot
        original_title_candidates = module.title_window_candidates
        original_sleep = module.time.sleep
        original_click = module.click
        try:
            module.screenshot = lambda _env, path: Path(path).write_bytes(b"fake screenshot")
            module.title_window_candidates = lambda _env, window: [window]
            module.time.sleep = lambda _seconds: None
            module.click = lambda *_args, **_kwargs: None

            def fake_run(command, *, env, check=True):
                if command[0] == "tesseract":
                    if command[-1] == "tsv":
                        return subprocess.CompletedProcess(command, 0, tsv, "")
                    return subprocess.CompletedProcess(command, 0, "SRNR (4)", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            with tempfile.TemporaryDirectory() as tmp:
                result = module.open_target(
                    {},
                    module.Window("1", 100, 200, 1000, 700),
                    target,
                    0,
                    Path(tmp),
                    "wechat-visible-row-noisy-title-test",
                    False,
                    False,
                    False,
                    relaxed_visible_fallback_allowed=True,
                )
        finally:
            module.run = original_run
            module.screenshot = original_screenshot
            module.title_window_candidates = original_title_candidates
            module.time.sleep = original_sleep
            module.click = original_click

        self.assertTrue(result["ok"])
        self.assertTrue(result["visible_chat_list_title_guard"])
        self.assertEqual(result["title_guard_source"], "visible_chat_list_match")

    def test_open_target_does_not_accept_visible_row_fallback_by_default(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="🍓我的设备",
            query="我的设备",
            expected_title="🍓我的设备",
            expected_title_aliases=("我的设备",),
            allow_title_guard_fallback=True,
        )
        tsv = "\n".join(
            [
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "5\t1\t10\t1\t1\t1\t64\t164\t58\t15\t80\t🍓",
                "5\t1\t10\t1\t1\t2\t88\t164\t58\t15\t80\t我的设备",
            ]
        )
        original_run = module.run
        original_screenshot = module.screenshot
        original_title_candidates = module.title_window_candidates
        original_sleep = module.time.sleep
        original_click = module.click
        try:
            module.screenshot = lambda _env, path: Path(path).write_bytes(b"fake screenshot")
            module.title_window_candidates = lambda _env, window: [window]
            module.time.sleep = lambda _seconds: None
            module.click = lambda *_args, **_kwargs: None

            def fake_run(command, *, env, check=True):
                if command[0] == "tesseract":
                    if command[-1] == "tsv":
                        return subprocess.CompletedProcess(command, 0, tsv, "")
                    return subprocess.CompletedProcess(command, 0, "SRNR (4)", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            with tempfile.TemporaryDirectory() as tmp:
                result = module.open_target(
                    {},
                    module.Window("1", 100, 200, 1000, 700),
                    target,
                    0,
                    Path(tmp),
                    "wechat-visible-row-noisy-title-default-test",
                    False,
                    False,
                    False,
                )
        finally:
            module.run = original_run
            module.screenshot = original_screenshot
            module.title_window_candidates = original_title_candidates
            module.time.sleep = original_sleep
            module.click = original_click

        self.assertFalse(result["ok"])
        self.assertEqual(result["visible_chat_list_match"]["normalized"], "我的设备")

    def test_visible_row_fallback_requires_target_opt_in(self):
        module = load_wechat_gui_send()

        result = module.visible_chat_list_fallback_guard(
            {
                "ok": False,
                "method": "visible_chat_list_ocr_double",
                "ocr_text": "SRNR (4)",
                "compose_window": module.window_to_dict(module.Window("1", 0, 0, 1000, 700)),
            },
            module.TargetSpec(
                name="🍓我的设备",
                query="我的设备",
                expected_title="🍓我的设备",
                expected_title_aliases=("我的设备",),
                allow_title_guard_fallback=False,
            ),
            {"text": "🍓我的设备", "normalized": "我的设备"},
        )

        self.assertIsNone(result)

    def test_paste_text_uses_bounded_clipboard_owner(self):
        module = load_wechat_gui_send()
        original_popen = module.subprocess.Popen
        original_run = module.run
        original_sleep = module.time.sleep
        calls = []

        class FakePipe:
            def write(self, text):
                calls.append(("write", text))

            def close(self):
                calls.append(("close",))

        class FakeProcess:
            def __init__(self, command, **_kwargs):
                calls.append(("popen", command))
                self.stdin = FakePipe()
                self.stdout = None
                self.stderr = None
                self.returncode = None

            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                self.returncode = 0
                return 0

            def terminate(self):
                calls.append(("terminate",))

            def kill(self):
                calls.append(("kill",))

        try:
            module.subprocess.Popen = FakeProcess
            module.run = lambda command, *, env, check=True: calls.append(("run", command)) or subprocess.CompletedProcess(command, 0, "", "")
            module.time.sleep = lambda _seconds: None

            module.paste_text({}, "hello")
        finally:
            module.subprocess.Popen = original_popen
            module.run = original_run
            module.time.sleep = original_sleep

        self.assertIn(("popen", ["xclip", "-selection", "clipboard", "-loops", "1"]), calls)
        self.assertIn(("run", ["xdotool", "key", "--clearmodifiers", "ctrl+v"]), calls)
        self.assertIn(("wait", 6.0), calls)

    def test_paste_text_timeout_is_a_send_failure(self):
        module = load_wechat_gui_send()

        class FakePipe:
            def write(self, _text):
                return None

            def close(self):
                return None

        class FakeProcess:
            def __init__(self, _command, **_kwargs):
                self.stdin = FakePipe()
                self.stdout = None
                self.stderr = None
                self.returncode = None
                self.wait_count = 0
                self.terminated = False

            def wait(self, timeout=None):
                self.wait_count += 1
                if self.wait_count == 1:
                    raise subprocess.TimeoutExpired("xclip", timeout)
                self.returncode = -15
                return self.returncode

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.returncode = -9

        process = FakeProcess([])
        with mock.patch.object(module.subprocess, "Popen", return_value=process), mock.patch.object(
            module, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), mock.patch.object(module.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "WECHAT_CLIPBOARD_PASTE_TIMEOUT"):
                module.paste_text({}, "hello")

        self.assertTrue(process.terminated)

    def test_verify_composer_text_reads_exact_message_back(self):
        module = load_wechat_gui_send()
        expected = "第一行\n研究室（けんきゅうしつ）"

        with mock.patch.object(module, "read_composer_text", return_value="第一行\r\n研究室（けんきゅうしつ）"):
            module.verify_composer_text({}, expected)

    def test_verify_composer_text_rejects_empty_composer(self):
        module = load_wechat_gui_send()

        with mock.patch.object(module, "read_composer_text", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "WECHAT_COMPOSE_VERIFY_FAILED"):
                module.verify_composer_text({}, "expected message")

    def test_read_composer_requires_new_copy_not_old_paste_clipboard(self):
        module = load_wechat_gui_send()
        marker = "labcanvas-clipboard-probe-fixed"
        for copied, error in ((marker, "did not copy"), ("actual reply", None)):
            with self.subTest(copied=copied):
                reads = iter([marker, copied])
                process = mock.Mock()
                process.poll.return_value = None
                calls = []

                def fake_run(command, **kwargs):
                    calls.append(command)
                    return subprocess.CompletedProcess(command, 0, next(reads) if command[0] == "xclip" else "", "")

                with mock.patch.object(module.subprocess, "Popen", return_value=process), \
                     mock.patch.object(module.uuid, "uuid4", return_value=mock.Mock(hex="fixed")), \
                     mock.patch.object(module, "run", side_effect=fake_run), \
                     mock.patch.object(module.time, "sleep"):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            module.read_composer_text({})
                    else:
                        self.assertEqual(module.read_composer_text({}), "actual reply")
                process.stdin.write.assert_called_once_with(marker)
                process.terminate.assert_called_once()
                self.assertIn(["xdotool", "key", "--clearmodifiers", "ctrl+c"], calls)

    def test_read_composer_rejects_clipboard_owner_race(self):
        module = load_wechat_gui_send()
        process = mock.Mock()
        with mock.patch.object(module.subprocess, "Popen", return_value=process), \
             mock.patch.object(module, "run", return_value=subprocess.CompletedProcess([], 0, "old pasted message", "")), \
             mock.patch.object(module.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "probe unavailable"):
                module.read_composer_text({})

    def test_pending_uncertain_send_is_not_resubmitted(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            pending.write_text('{}')
            with mock.patch.object(module, "pending_receipt_path", return_value=pending), \
                 mock.patch.object(module, "wait_native_receipt", return_value=None), \
                 mock.patch.object(module, "reset_wechat_send_surface") as reset:
                with self.assertRaisesRegex(RuntimeError, "WECHAT_GUI_SEND_UNCERTAIN"):
                    module.send_one({}, module.Window("1", 0, 0, 1000, 700),
                                    module.TargetSpec("Shares", "Shares", "Shares"),
                                    "reply", True, False, 0, False, True, False,
                                    Path(tmp), Path(tmp) / "mirror.db", 1)
            reset.assert_not_called()
            self.assertTrue(pending.exists())

    def test_screen_redraw_after_enter_is_not_delivery_evidence(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            with mock.patch.object(module, "pending_receipt_path", return_value=pending), \
                 mock.patch.object(module, "reset_wechat_send_surface"), \
                 mock.patch.object(module, "screenshot"), \
                 mock.patch.object(module, "open_target", return_value={"ok": True}), \
                 mock.patch.object(module, "focus"), \
                 mock.patch.object(module, "clear_composer"), \
                 mock.patch.object(module, "paste_text"), \
                 mock.patch.object(module, "verify_composer_text"), \
                 mock.patch.object(module, "same_screenshot", return_value=False), \
                 mock.patch.object(module, "prepare_receipt", return_value={"after": {}}), \
                 mock.patch.object(module, "key") as key, \
                 mock.patch.object(module.time, "sleep"), \
                 mock.patch.object(module, "wait_native_receipt", return_value=None), \
                 mock.patch.object(module, "record_event") as record:
                with self.assertRaisesRegex(RuntimeError, "WECHAT_GUI_SEND_UNCERTAIN"):
                    module.send_one({}, module.Window("1", 0, 0, 1000, 700),
                                    module.TargetSpec("Shares", "Shares", "Shares"),
                                    "reply", True, False, 0, True, True, False,
                                    Path(tmp), Path(tmp) / "mirror.db", 1)
            key.assert_called_once_with({}, "Return")
            self.assertTrue(pending.exists())
            self.assertEqual(record.call_args.kwargs["status"], "uncertain")

    def test_visual_only_send_uses_visible_button_without_native_receipt(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(module, "reset_wechat_send_surface"),
                mock.patch.object(module, "screenshot"),
                mock.patch.object(module, "open_target", return_value={"ok": True}),
                mock.patch.object(module, "focus"),
                mock.patch.object(module, "clear_composer"),
                mock.patch.object(module, "paste_text"),
                mock.patch.object(module, "verify_composer_text"),
                mock.patch.object(module, "same_screenshot", return_value=False),
                mock.patch.object(module, "prepare_receipt", side_effect=AssertionError("native receipt must not be read")) as prepare,
                mock.patch.object(module, "click_visible_send_button") as send_button,
                mock.patch.object(module, "click"),
                mock.patch.object(module, "read_composer_text", return_value=""),
                mock.patch.object(module.time, "sleep"),
                mock.patch.object(module, "record_event") as record,
            ):
                result = module.send_one(
                    {}, module.Window("1", 0, 0, 1000, 700),
                    module.TargetSpec("OvertreeAI 测试", "OvertreeAI 测试", "OvertreeAI 测试"),
                    "绑定成功", True, False, 0, True, True, False,
                    Path(tmp), Path(tmp) / "mirror.db", 1,
                    visual_only_send=True,
                )

        self.assertEqual(result["status"], "sent-visible")
        send_button.assert_called_once()
        prepare.assert_not_called()
        self.assertEqual(record.call_args.kwargs["metadata"]["delivery_verification"]["method"],
                         "visible_send_button_composer_cleared")

    def test_visual_only_send_preserves_uncertain_draft_without_retrying(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(module, "reset_wechat_send_surface"),
                mock.patch.object(module, "screenshot"),
                mock.patch.object(module, "open_target", return_value={"ok": True}),
                mock.patch.object(module, "focus"),
                mock.patch.object(module, "clear_composer"),
                mock.patch.object(module, "paste_text"),
                mock.patch.object(module, "verify_composer_text"),
                mock.patch.object(module, "same_screenshot", return_value=False),
                mock.patch.object(module, "prepare_receipt", side_effect=AssertionError("native receipt must not be read")),
                mock.patch.object(module, "click_visible_send_button"),
                mock.patch.object(module, "click"),
                mock.patch.object(module, "read_composer_text", return_value="draft still present"),
                mock.patch.object(module.time, "sleep"),
                mock.patch.object(module, "record_event") as record,
            ):
                with self.assertRaisesRegex(RuntimeError, "WECHAT_GUI_SEND_UNCERTAIN"):
                    module.send_one(
                        {}, module.Window("1", 0, 0, 1000, 700),
                        module.TargetSpec("OvertreeAI 测试", "OvertreeAI 测试", "OvertreeAI 测试"),
                        "确认消息", True, False, 0, True, True, False,
                        Path(tmp), Path(tmp) / "mirror.db", 1,
                        visual_only_send=True,
                    )
        self.assertEqual(record.call_args.kwargs["status"], "uncertain")

    def test_pending_native_receipt_recovers_without_gui_send(self):
        module = load_wechat_gui_send()
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            pending.write_text('{}')
            evidence = {"verified": True, "method": "native_outbound_echo"}
            with mock.patch.object(module, "pending_receipt_path", return_value=pending), \
                 mock.patch.object(module, "wait_native_receipt", return_value=evidence), \
                 mock.patch.object(module, "record_event") as record, \
                 mock.patch.object(module, "reset_wechat_send_surface") as reset:
                result = module.send_one({}, module.Window("1", 0, 0, 1000, 700),
                                         module.TargetSpec("Shares", "Shares", "Shares"),
                                         "reply", True, False, 0, False, True, False,
                                         Path(tmp), Path(tmp) / "mirror.db", 1)
            self.assertEqual(result["status"], "sent")
            reset.assert_not_called()
            self.assertFalse(pending.exists())
            self.assertEqual(record.call_args.kwargs["metadata"]["delivery_verification"], evidence)

    def test_target_search_requires_explicit_opt_in(self):
        module = load_wechat_gui_send()

        default_target = module.target_from_raw({"name": "EchoMind", "query": "EchoMind"})
        allowed_target = module.target_from_raw({"name": "EchoMind", "query": "EchoMind", "allow_search": True})
        blocked_target = module.target_from_raw({"name": "EchoMind", "query": "EchoMind", "allow_search": True, "no_search": True})

        self.assertFalse(default_target.allow_search)
        self.assertTrue(allowed_target.allow_search)
        self.assertFalse(blocked_target.allow_search)

    def test_global_account_search_requires_environment_opt_in(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="EchoMind",
            query="EchoMind",
            expected_title="EchoMind",
            allow_search=True,
        )

        with mock.patch.dict(module.os.environ, {}, clear=True):
            self.assertFalse(module.global_search_allowed(True, target))
        with mock.patch.dict(
            module.os.environ,
            {"WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH": "1"},
            clear=True,
        ):
            self.assertTrue(module.global_search_allowed(True, target))

    def test_preferred_search_query_uses_visible_title_for_stable_route_alias(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="stable-session-key",
            query="stable-session-key",
            expected_title="Visible Contact",
        )

        self.assertEqual(module.preferred_search_query(target), "Visible Contact")

    def test_preferred_search_query_keeps_deliberate_title_prefix(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="memo-profile",
            query="MEMO写作",
            expected_title="MEMO写作—外语—挣钱",
        )

        self.assertEqual(module.preferred_search_query(target), "MEMO写作")

    def test_close_non_target_wechat_windows_keeps_target_popup(self):
        module = load_wechat_gui_send()
        original_run = module.run
        original_close = module.request_close
        closed = []
        try:
            def fake_run(command, *, env, check=True):
                if command[:3] == ["xdotool", "search", "--onlyvisible"]:
                    return subprocess.CompletedProcess(command, 0, "main\nfile\nmine\n", "")
                if command[:2] == ["xdotool", "getwindowgeometry"]:
                    wid = command[-1]
                    if wid == "main":
                        return subprocess.CompletedProcess(command, 0, "X=0\nY=0\nWIDTH=1000\nHEIGHT=700\n", "")
                    return subprocess.CompletedProcess(command, 0, "X=100\nY=100\nWIDTH=600\nHEIGHT=500\n", "")
                if command[:2] == ["xdotool", "getwindowname"]:
                    return subprocess.CompletedProcess(command, 0, "🍓我的设备\n" if command[-1] == "mine" else "File Transfer\n", "")
                self.assertNotIn("windowclose", command)
                return subprocess.CompletedProcess(command, 0, "", "")

            module.run = fake_run
            module.request_close = lambda wid, **kwargs: closed.append(wid)
            module.close_non_target_wechat_windows(
                {},
                module.Window("main", 0, 0, 1000, 700),
                module.TargetSpec(
                    name="🍓我的设备",
                    query="我的设备",
                    expected_title="🍓我的设备",
                    expected_title_aliases=("我的设备",),
                ),
            )
        finally:
            module.run = original_run
            module.request_close = original_close

        self.assertEqual(closed, ["file"])

    def test_reset_send_surface_dismisses_transient_children_before_target_open(self):
        module = load_wechat_gui_send()
        calls = []
        target = module.TargetSpec(
            name="MEMO写作—外语—挣钱",
            query="MEMO写作",
            expected_title="MEMO写作—外语—挣钱",
        )
        with (
            mock.patch.object(
                module,
                "close_non_target_wechat_windows",
                side_effect=lambda *_args: calls.append("close"),
            ),
            mock.patch.object(
                module,
                "focus",
                side_effect=lambda *_args: calls.append("focus"),
            ),
            mock.patch.object(
                module,
                "key",
                side_effect=lambda _env, value: calls.append(value),
            ),
            mock.patch.object(
                module,
                "dismiss_internal_file_transfer_surface",
                side_effect=lambda *_args: calls.append("dismiss"),
            ),
            mock.patch.object(module.time, "sleep"),
        ):
            module.reset_wechat_send_surface(
                {},
                module.Window("main", 0, 0, 1000, 700),
                target,
                0.2,
            )

        self.assertEqual(
            calls,
            ["close", "focus", "Escape", "Escape", "dismiss", "close", "focus"],
        )

    def test_dismiss_internal_file_transfer_surface_uses_bounded_close_control(self):
        module = load_wechat_gui_send()
        target = module.TargetSpec(
            name="MEMO写作—外语—挣钱",
            query="MEMO写作",
            expected_title="MEMO写作—外语—挣钱",
        )
        points = []
        with (
            mock.patch.object(
                module,
                "internal_file_transfer_surface_visible",
                side_effect=[True, False],
            ),
            mock.patch.object(
                module,
                "click",
                side_effect=lambda _env, x, y: points.append((x, y)),
            ),
            mock.patch.object(module.time, "sleep"),
        ):
            dismissed = module.dismiss_internal_file_transfer_surface(
                {},
                module.Window("main", 489, 193, 1020, 739),
                target,
                0.2,
            )

        self.assertTrue(dismissed)
        self.assertEqual(points, [(1247, 223)])

    def test_same_screenshot_detects_identical_files(self):
        module = load_wechat_gui_send()
        first = Path("/tmp/wechat-gui-send-same-a.png")
        second = Path("/tmp/wechat-gui-send-same-b.png")
        try:
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            self.assertTrue(module.same_screenshot(first, second))
            second.write_bytes(b"different")
            self.assertFalse(module.same_screenshot(first, second))
        finally:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
