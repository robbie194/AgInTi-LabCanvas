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
    def test_ocr_lines_are_sorted_and_grouped(self):
        module = load_module()
        tsv = """level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t2\t10\t50\t10\t10\t90\tquestion
5\t1\t1\t1\t1\t10\t20\t10\t10\t90\t@OverTree
5\t1\t1\t1\t2\t30\t50\t10\t10\t90\tnow
"""
        self.assertEqual([item["text"] for item in module.parse_ocr_lines(tsv)], ["@OverTree", "question now"])

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
        self.assertEqual(regions[0]["left"], 813)
        self.assertEqual(regions[0]["width"], 384)
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
            "list_registered_groups": lambda _self: {
                "ok": True,
                "groups": [{"groupId": group_id, "title": "OverTree-G-7K4Q2M"}],
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
            configs = module.registered_group_configs(force_refresh=True)
            allowlist = os.environ["WECHAT_PERSONAL_GROUP_ALLOWLIST"]

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["chatroom_id"], group_id)
        self.assertEqual(configs[0]["send_target"]["expected_title"], "OverTree-G-7K4Q2M")
        self.assertTrue(configs[0]["send_target"]["allow_search"])
        self.assertIn(group_id, allowlist)
        self.assertIn("legacy-group", allowlist)

    def test_registered_group_sync_ignores_titles_outside_generated_name_policy(self):
        module = load_module()
        cloud = sys.modules["wechat_personal_group_cloud"]
        fake_client = type("FakeRegistryClient", (), {
            "list_registered_groups": lambda _self: {
                "ok": True,
                "groups": [{"groupId": "7e3e4567-e89b-42d3-a456-426614174000", "title": "Customer private group"}],
            },
        })()
        with mock.patch.dict(os.environ, {"WECHAT_OCR_GROUP_REMOTE_SYNC": "true"}), \
             mock.patch.object(cloud, "build_registry_client", return_value=fake_client):
            module._REMOTE_GROUP_CACHE.update({"fetched_at": 0.0, "configs": [], "error": ""})
            self.assertEqual(module.registered_group_configs(force_refresh=True), [])

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


if __name__ == "__main__":
    unittest.main()
