from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import types
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import wechat_personal_group_cloud as bridge


class FakeClient:
    def __init__(self, *, bound: bool = True, receipt: dict | None = None, claim_error: Exception | None = None):
        self.bound = bound
        self.receipt = receipt or {"status": "running", "responseId": "run-1"}
        self.claim_error = claim_error
        self.claimed = []
        self.submitted = []
        self.polled = []
        self.uploaded = []
        self.resolve_calls = 0

    def resolve(self, group_id: str) -> dict:
        self.resolve_calls += 1
        if not self.bound:
            raise bridge.PersonalGroupCloudError("missing", status_code=404)
        return {"ok": True, "bindingId": "binding-1"}

    def claim(self, group_id: str, code: str) -> dict:
        self.claimed.append((group_id, code))
        self.bound = True
        if self.claim_error:
            raise self.claim_error
        return {"ok": True, "bindingId": "binding-1"}

    def submit(self, group_id: str, message: dict) -> dict:
        self.submitted.append((group_id, message))
        return self.receipt

    def get_message(self, group_id: str, message_id: str) -> dict:
        self.polled.append((group_id, message_id))
        return self.receipt

    def upload_attachment(self, group_id: str, message_id: str, index: int, filename: str, content_type: str, data: bytes) -> dict:
        self.uploaded.append((group_id, message_id, index, filename, content_type, data))
        return {"ok": True, "path": f"wechat-inbox/binding/{index}-{filename}", "size": len(data), "name": filename}


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


class PersonalGroupCloudTests(unittest.TestCase):
    def config(self, *, enabled: bool = True) -> dict:
        return {
            "chat_name": "customer-test",
            "chatroom_id": "group-exact-1",
            "personal_group_cloud": {
                "enabled": enabled,
                "support_member_sha256": [hashlib.sha256(b"support-wxid").hexdigest()],
                "trigger_prefixes": ["@OverTree", "＠OverTree"],
            },
        }

    def row(self, local_id: int, sender: str, text: str, *, local_type: int = 1) -> dict:
        return {
            "_message_db": "message_4.db",
            "local_id": local_id,
            "server_id": f"server-{local_id}",
            "local_type": local_type,
            "create_time": 1_800_000_000,
            "sender": sender,
            "sender_display": sender,
            "content": text,
        }

    def run_once(self, client, rows, *, state=None, send_text=None):
        bridge.PRIVATE.mkdir(parents=True, exist_ok=True)
        state_dir = tempfile.TemporaryDirectory(prefix="personal-group-test-", dir=bridge.PRIVATE)
        self.addCleanup(state_dir.cleanup)
        config = self.config()
        config["_runtime_state_path"] = str(Path(state_dir.name) / "group.state.json")
        return bridge.run_once(
            config, state or {}, rows, send=True,
            inbound=lambda row: row.get("sender") != "agent-wxid",
            eligible=lambda row: row.get("sender") != "agent-wxid",
            visible_text=lambda row: row["content"],
            send_text=send_text or (lambda _config, _text: None),
            client_factory=lambda _config: (client, "group-exact-1"),
        )

    def test_exact_group_allowlist_and_enable_flags_fail_closed(self):
        config = self.config()
        for env in (
            {"WECHAT_PERSONAL_GROUP_ENABLED": "true", "WECHAT_PERSONAL_GROUP_ALLOWLIST": "another-group"},
            {"WECHAT_PERSONAL_GROUP_ENABLED": "false", "WECHAT_PERSONAL_GROUP_ALLOWLIST": "group-exact-1"},
        ):
            with self.subTest(env=env), self.assertRaises(bridge.PersonalGroupCloudError):
                bridge.build_client(config, environ={**env, "WECHAT_PERSONAL_GROUP_CLOUD_URL": "https://cloud.example", "WECHAT_PERSONAL_GROUP_SHARED_SECRET": "x" * 40, "WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID": "agent-1"})
        self.assertFalse(bridge.personal_group_cloud_enabled(self.config(enabled=False)))

    def test_idle_monitor_does_not_poll_cloud_without_rows_or_pending_work(self):
        client = FakeClient()
        result = self.run_once(client, [])
        self.assertTrue(result["metrics"]["idle"])
        self.assertEqual(client.resolve_calls, 0)

    def test_running_cloud_task_uses_bounded_poll_backoff(self):
        client = FakeClient()
        state = {
            "personal_group_cloud_binding_id": "binding-1",
            "personal_group_cloud_binding_checked_at": int(time.time()),
            "personal_group_cloud_pending": [{"messageId": "m-1", "submitted": True}],
        }
        self.run_once(client, [], state=state)
        self.assertEqual(len(client.polled), 1)
        self.run_once(client, [], state=state)
        self.assertEqual(len(client.polled), 1)
        self.assertEqual(client.resolve_calls, 0)

    def test_revoked_cloud_binding_discards_queued_replies(self):
        client = FakeClient(bound=False)
        state = {
            "personal_group_cloud_binding_id": "binding-revoked",
            "personal_group_cloud_binding_checked_at": 0,
            "personal_group_cloud_pending": [{"messageId": "old-message", "submitted": True}],
            "personal_group_cloud_outbox": [{"id": "old-reply", "text": "private result"}],
        }
        sent = []
        result = self.run_once(client, [], state=state, send_text=lambda _config, text: sent.append(text))
        self.assertEqual(result["state"]["personal_group_cloud_pending"], [])
        self.assertEqual(result["state"]["personal_group_cloud_outbox"], [])
        self.assertFalse(sent)

    def test_signed_binary_upload_uses_isolated_route_and_content_type_header(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"ok": True, "path": "wechat-inbox/binding/file.pdf", "size": 3})

        client = bridge.PersonalGroupCloudClient(
            base_url="https://cloud.example",
            secret="s" * 48,
            agent_account_id="agent-1",
            opener=opener,
        )
        result = client.upload_attachment("group one", "message:1", 0, "file.pdf", "application/pdf", b"pdf")
        request = captured["request"]
        parsed = urlsplit(request.full_url)
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.data, b"pdf")
        self.assertEqual(parse_qs(parsed.query)["channel"], ["wechat_personal_group"])
        self.assertEqual(request.get_header("X-overtree-wechat-content-type"), "application/pdf")
        timestamp = request.get_header("X-overtree-wechat-timestamp")
        nonce = request.get_header("X-overtree-wechat-nonce")
        canonical = "\n".join(("PUT", parsed.path + "?" + parsed.query, timestamp, nonce, hashlib.sha256(b"pdf").hexdigest())).encode()
        expected = __import__("base64").urlsafe_b64encode(hmac.new(b"s" * 48, canonical, hashlib.sha256).digest()).decode().rstrip("=")
        self.assertEqual(request.get_header("X-overtree-wechat-signature"), expected)
        self.assertEqual(result["path"], "wechat-inbox/binding/file.pdf")

    def test_submits_sender_attribution_and_never_submits_agent_echo(self):
        client = FakeClient()
        support = self.row(7, "support-wxid", "@OverTree 补充一个背景")
        echo = self.row(8, "agent-wxid", "OverTree answer")
        sent = []
        result = self.run_once(
            client,
            [support, echo],
            send_text=lambda _config, text: sent.append(text),
        )
        self.assertEqual(len(client.submitted), 1)
        message = client.submitted[0][1]
        self.assertEqual(message["actorRole"], "support_member")
        self.assertEqual(message["actorId"], "support-wxid")
        self.assertEqual(message["content"], "@OverTree 补充一个背景")
        self.assertEqual(result["metrics"]["pending"], 1)
        self.assertFalse(sent)

    def test_claim_code_and_next_same_poll_message_are_both_processed(self):
        client = FakeClient(bound=False)
        code = self.row(9, "customer-wxid", "ABC23456")
        followup = self.row(10, "customer-wxid", "@OverTree 我想分析这个项目")
        sent = []
        result = self.run_once(client, [code, followup], send_text=lambda _config, text: sent.append(text))
        self.assertEqual(client.claimed, [("group-exact-1", "ABC23456")])
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(client.submitted[0][1]["content"], "@OverTree 我想分析这个项目")
        self.assertEqual(sent, ["此群已连接到对应的 OverTree 项目和会话。客户与已登记客服的消息会进入同一会话。"])
        self.assertEqual(result["metrics"]["transport"], "personal_group_cloud")

    def test_exact_media_file_is_uploaded_before_cloud_message_submission(self):
        client = FakeClient()
        row = self.row(11, "customer-wxid", "@OverTree [WeChat image]", local_type=3)
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "source.png"
            image.write_bytes(b"exact-image")
            with mock.patch.object(bridge, "_media_sync_event_files", return_value=[image]):
                self.run_once(client, [row])
        self.assertEqual(len(client.uploaded), 1)
        self.assertEqual(client.uploaded[0][-1], b"exact-image")
        attachments = client.submitted[0][1]["attachments"]
        self.assertEqual(attachments[0]["path"], "wechat-inbox/binding/0-source.png")
        self.assertEqual(attachments[0]["contentType"], "image/png")

    def test_ordinary_group_chat_is_ignored_until_someone_mentions_the_agent(self):
        client = FakeClient()
        ordinary = self.row(15, "customer-wxid", "客服，我刚才把报告发你了")
        result = self.run_once(client, [ordinary])
        self.assertFalse(client.submitted)
        self.assertEqual(result["metrics"]["pending"], 0)

        addressed = self.row(16, "customer-wxid", "＠OverTree 请帮我看看这张图")
        self.run_once(client, [addressed])
        self.assertEqual(len(client.submitted), 1)
        self.assertIn("＠OverTree", client.submitted[0][1]["content"])

    def test_terminal_result_is_delivered_and_pending_item_is_cleared(self):
        client = FakeClient(receipt={"status": "finished", "result": "已完成", "files": [{"url": "https://cloud.example/result.pdf"}]})
        state = {
            "personal_group_cloud_pending": [{
                "messageId": "message_4.db:server-7:7",
                "submitted": True,
                "sourceRow": {"_message_db": "message_4.db", "local_id": 7, "server_id": "server-7"},
            }]
        }
        sent = []
        result = self.run_once(client, [], state=state, send_text=lambda _config, text: sent.append(text))
        self.assertEqual(client.polled, [("group-exact-1", "message_4.db:server-7:7")])
        self.assertEqual(sent, ["已完成\n\n文件：https://cloud.example/result.pdf"])
        self.assertEqual(result["metrics"]["pending"], 0)
        self.assertEqual(result["metrics"]["outbox"], 0)

    def test_outgoing_send_failure_is_saved_for_retry(self):
        client = FakeClient(bound=False)
        row = self.row(9, "customer-wxid", "绑定码无效")
        row["content"] = "ABC23456"
        result = self.run_once(
            client,
            [row],
            send_text=lambda _config, _text: (_ for _ in ()).throw(RuntimeError("GUI unavailable")),
        )
        outbox = result["state"]["personal_group_cloud_outbox"]
        self.assertEqual(len(outbox), 1)
        self.assertIn("GUI unavailable", outbox[0]["lastError"])
        self.assertGreater(outbox[0]["nextAttemptAt"], time.time())

    def test_exact_own_echo_reconciles_a_send_interrupted_by_restart(self):
        queued_at = int(time.time()) - 2
        state = {"personal_group_cloud_outbox": [{
            "id": "reply-1",
            "text": "OverTree answer",
            "queuedAt": queued_at,
            "sendingAt": queued_at + 1,
        }]}
        echo = self.row(14, "agent-wxid", "OverTree answer")
        echo["create_time"] = queued_at + 1
        bridge._reconcile_outgoing_echoes(
            state,
            [echo],
            inbound=lambda row: row.get("sender") != "agent-wxid",
            visible_text=lambda row: row["content"],
            now=time.time(),
        )
        self.assertEqual(state["personal_group_cloud_outbox"], [])

    def test_lost_binding_response_does_not_forward_one_time_code_to_cloud(self):
        client = FakeClient(bound=False, claim_error=bridge.PersonalGroupCloudError("lost response", retryable=True))
        row = self.row(13, "customer-wxid", "ABC23456")
        bridge.PRIVATE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=bridge.PRIVATE) as folder:
            state_path = Path(folder) / "state.json"
            config = self.config()
            config["_runtime_state_path"] = str(state_path)
            with self.assertRaises(bridge.PersonalGroupCloudError):
                bridge.run_once(
                    config, {}, [row], send=True,
                    inbound=lambda _row: True,
                    eligible=lambda _row: True,
                    visible_text=lambda item: item["content"],
                    send_text=lambda _config, _text: None,
                    client_factory=lambda _config: (client, "group-exact-1"),
                )
            saved_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("message_4.db:server-13:13", saved_state["personal_group_cloud_claim_attempt_ids"])
            sent = []
            recovered = bridge.run_once(
                config, saved_state, [row], send=True,
                inbound=lambda _row: True,
                eligible=lambda _row: True,
                visible_text=lambda item: item["content"],
                send_text=lambda _config, text: sent.append(text),
                client_factory=lambda _config: (client, "group-exact-1"),
            )
        self.assertFalse(client.submitted)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("ABC23456", sent[0])
        self.assertFalse(recovered["state"]["personal_group_cloud_claim_attempt_ids"])

    def test_group_state_is_private_and_cannot_be_reused_by_another_group(self):
        client = FakeClient()
        bridge.PRIVATE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=bridge.PRIVATE) as folder:
            config = self.config()
            config["_runtime_state_path"] = str(Path(folder) / "group.state.json")
            state = {}
            bridge._claim_private_group_state(config, state, "group-exact-1")
            with self.assertRaisesRegex(bridge.PersonalGroupCloudError, "different WeChat group"):
                bridge._claim_private_group_state(config, state, "another-group")
            with self.assertRaisesRegex(bridge.PersonalGroupCloudError, "already assigned"):
                bridge._claim_private_group_state(config, {}, "another-group")

        with tempfile.TemporaryDirectory() as folder:
            config = self.config()
            config["_runtime_state_path"] = str(Path(folder) / "public.state.json")
            with self.assertRaisesRegex(bridge.PersonalGroupCloudError, "private directory"):
                bridge._claim_private_group_state(config, {}, "group-exact-1")

    def test_group_trigger_prefix_must_be_an_explicit_at_mention(self):
        config = self.config()
        config["personal_group_cloud"]["trigger_prefixes"] = "@OverTree"
        with self.assertRaises(bridge.PersonalGroupCloudError):
            bridge.PRIVATE.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=bridge.PRIVATE) as folder:
                config["_runtime_state_path"] = str(Path(folder) / "group.state.json")
                bridge.run_once(
                    config, {}, [], send=True,
                    eligible=lambda _row: True, inbound=lambda _row: True,
                    visible_text=lambda row: row["content"], send_text=lambda _config, _text: None,
                    client_factory=lambda _config: (FakeClient(), "group-exact-1"),
                )

    def test_direct_monitor_routes_enabled_group_to_cloud_without_running_local_agent(self):
        app = types.ModuleType("agenticapp")
        app.__path__ = []
        backends = types.ModuleType("agenticapp.backends")
        backends.model_policy_for_effort = lambda *_args, **_kwargs: {"model": "gpt-test"}
        agent_backend = types.ModuleType("wechat_agent_backend")
        agent_backend.run_agent_session = lambda *_args, **_kwargs: None
        agent_backend.select_agent_backend = lambda *_args, **_kwargs: "codex"
        agent_backend.user_facing_backend_message = lambda *_args, **_kwargs: ""
        module_name = "wechat_direct_chatops_personal_group_test"
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / "wechat_direct_chatops.py")
        assert spec and spec.loader
        direct = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "agenticapp": app,
            "agenticapp.backends": backends,
            "wechat_agent_backend": agent_backend,
        }):
            sys.modules[module_name] = direct
            try:
                spec.loader.exec_module(direct)
            finally:
                sys.modules.pop(module_name, None)
        config = {
            "chat_name": "customer-test",
            "message_table": "Msg_0",
            "self_wxid": "agent-wxid",
            "send_target": {"name": "customer-test", "expected_title": "Customer Coaching"},
            "personal_group_cloud": {"enabled": True},
        }
        row = self.row(12, "customer-wxid", "请帮我看这个实验")
        adapter_result = {"response_sent": None, "task_enqueued": "run-12", "metrics": {"transport": "personal_group_cloud"}}
        with (
            mock.patch.object(direct, "read_inflight_messages", return_value=[]),
            mock.patch.object(direct, "retry_pending_voice_backlog", return_value=[]),
            mock.patch.object(direct, "read_new_messages", return_value=[row]),
            mock.patch.object(direct, "sync_row_to_mirror"),
            mock.patch.object(direct, "checkpoint_inbound_before_route"),
            mock.patch.object(direct, "run_personal_group_cloud_once", return_value=adapter_result) as cloud_route,
            mock.patch.object(direct, "run_codex", side_effect=AssertionError("local Agent must not run")),
            mock.patch.object(direct, "organize_messages", side_effect=AssertionError("local organizer must not run")),
        ):
            result = direct.run_once(config, {}, send=True, no_decrypt=True)
        cloud_route.assert_called_once()
        self.assertEqual(result["task_enqueued"], "run-12")
        self.assertEqual(result["metrics"]["transport"], "personal_group_cloud")
        self.assertNotIn("inflight_messages", result["state"])


if __name__ == "__main__":
    unittest.main()
