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
        self.last_claim_message_id = ""
        self.submitted = []
        self.polled = []
        self.uploaded = []
        self.resolve_calls = 0

    def resolve(self, group_id: str) -> dict:
        self.resolve_calls += 1
        if not self.bound:
            raise bridge.PersonalGroupCloudError("missing", status_code=404)
        return {"ok": True, "bindingId": "binding-1", "lastClaimMessageId": self.last_claim_message_id}

    def claim(self, group_id: str, code: str, claim_message_id: str) -> dict:
        self.claimed.append((group_id, code, claim_message_id))
        switched = self.bound
        self.bound = True
        self.last_claim_message_id = claim_message_id
        if self.claim_error:
            raise self.claim_error
        return {"ok": True, "bindingId": "binding-1", "switched": switched}

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

    def run_once(self, client, rows, *, state=None, send_text=None, send_file=None):
        bridge.PRIVATE.mkdir(parents=True, exist_ok=True)
        state_dir = tempfile.TemporaryDirectory(prefix="personal-group-test-", dir=bridge.PRIVATE)
        self.addCleanup(state_dir.cleanup)
        config = self.config()
        config["_runtime_state_path"] = str(Path(state_dir.name) / "group.state.json")
        return bridge.run_once(
            config, state if state is not None else {}, rows, send=True,
            inbound=lambda row: row.get("sender") != "agent-wxid",
            eligible=lambda row: row.get("sender") != "agent-wxid",
            visible_text=lambda row: row["content"],
            send_text=send_text or (lambda _config, _text: None),
            send_file=send_file,
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

    def test_binding_claim_carries_an_idempotency_receipt_in_the_private_body(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"ok": True, "bindingId": "binding-1", "switched": True})

        client = bridge.PersonalGroupCloudClient(
            base_url="https://cloud.example",
            secret="s" * 48,
            agent_account_id="agent-1",
            opener=opener,
        )
        result = client.claim("group-exact-1", "ABCD2345", "message_4.db:server-5:5")
        request = captured["request"]
        self.assertEqual(urlsplit(request.full_url).path, "/internal/wechat/personal-groups/v1/bindings/claim")
        body = json.loads(request.data)
        self.assertEqual(body["code"], "ABCD2345")
        self.assertEqual(body["claimMessageId"], "message_4.db:server-5:5")
        self.assertNotIn("ABCD2345", request.full_url)
        self.assertTrue(result["switched"])

    def test_registered_group_list_uses_the_signed_private_route(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"ok": True, "groups": [{"groupId": "group-1", "title": "OverTree-G-7K4Q2M"}]})

        client = bridge.PersonalGroupCloudClient(
            base_url="https://cloud.example",
            secret="s" * 48,
            agent_account_id="agent-1",
            opener=opener,
        )
        result = client.list_registered_groups(["local-id"], pending_titles=["OverTree-2AAC"])
        request = captured["request"]
        parsed = urlsplit(request.full_url)
        self.assertEqual(request.method, "GET")
        self.assertEqual(parsed.path, "/internal/wechat/personal-groups/v1/groups")
        self.assertEqual(parse_qs(parsed.query)["agentAccountId"], ["agent-1"])
        self.assertEqual(parse_qs(parsed.query)["groupId"], ["local-id"])
        self.assertEqual(parse_qs(parsed.query)["pendingTitle"], ["OverTree-2AAC"])
        self.assertEqual(result["groups"][0]["title"], "OverTree-G-7K4Q2M")
        timestamp = request.get_header("X-overtree-wechat-timestamp")
        nonce = request.get_header("X-overtree-wechat-nonce")
        canonical = "\n".join(("GET", parsed.path + "?" + parsed.query, timestamp, nonce, hashlib.sha256(b"").hexdigest())).encode()
        expected = __import__("base64").urlsafe_b64encode(hmac.new(b"s" * 48, canonical, hashlib.sha256).digest()).decode().rstrip("=")
        self.assertEqual(request.get_header("X-overtree-wechat-signature"), expected)

    def test_submits_any_member_with_same_role_and_never_submits_agent_echo(self):
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
        self.assertEqual(message["actorRole"], "group_member")
        self.assertEqual(message["actorId"], "support-wxid")
        self.assertEqual(message["content"], "@OverTree 补充一个背景")
        self.assertEqual(result["metrics"]["pending"], 1)
        self.assertEqual(sent, ["已收到，OverTree 正在处理。完成后会把结果发到本群。"])

    def test_claim_code_and_next_same_poll_message_are_both_processed(self):
        client = FakeClient(bound=False)
        code = self.row(9, "customer-wxid", "ABC23456")
        followup = self.row(10, "customer-wxid", "@OverTree 我想分析这个项目")
        sent = []
        result = self.run_once(client, [code, followup], send_text=lambda _config, text: sent.append(text))
        self.assertEqual(client.claimed, [("group-exact-1", "ABC23456", "message_4.db:server-9:9")])
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(client.submitted[0][1]["content"], "@OverTree 我想分析这个项目")
        self.assertEqual(sent, ["此群已连接到对应的 OverTree 项目和会话。"])
        self.assertEqual(result["metrics"]["transport"], "personal_group_cloud")

    def test_nontext_inputs_are_rejected_once_without_cloud_or_media_access(self):
        for kind in (3, 34, 42, 43, 47, 48, 49, 999, 0):
            with self.subTest(kind=kind):
                client = FakeClient()
                state = {}
                sent = []
                # Includes a plausible binding code and transcript: neither is
                # allowed to bypass the message-type guard.
                row = self.row(kind + 100, "customer-wxid", "ABC23456", local_type=kind)
                with mock.patch.object(bridge, "_upload_attachments") as upload:
                    self.run_once(client, [row], state=state, send_text=lambda _c, text: sent.append(text))
                    self.run_once(client, [row], state=state, send_text=lambda _c, text: sent.append(text))
                self.assertEqual(sent, [bridge.TEXT_ONLY_NOTICE])
                self.assertFalse(client.submitted)
                self.assertFalse(client.claimed)
                self.assertFalse(client.uploaded)
                self.assertEqual(client.resolve_calls, 0)
                upload.assert_not_called()

    def test_nontext_policy_does_not_reply_to_outbound_agent_media(self):
        client = FakeClient()
        sent = []
        self.run_once(client, [self.row(11, "agent-wxid", "[image]", local_type=3)],
                      send_text=lambda _c, text: sent.append(text))
        self.assertEqual(sent, [])
        self.assertFalse(client.submitted)

    def test_unsent_legacy_media_pending_is_removed_without_upload_or_submission(self):
        client = FakeClient()
        media = self.row(11, "customer-wxid", "@OverTree image", local_type=3)
        text = self.row(12, "customer-wxid", "@OverTree 帮我画图")
        state = {"personal_group_cloud_pending": [{"messageId": "old-media", "sourceRow": media,
                                                  "submitted": False, "content": "image"}]}
        sent = []
        self.run_once(client, [text], state=state, send_text=lambda _c, t: sent.append(t))
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(client.submitted[0][1]["content"], text["content"])
        self.assertFalse(client.uploaded)
        self.assertEqual(sent, [bridge.TEXT_ONLY_NOTICE])

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

    def test_new_code_switches_only_this_groups_cloud_session_and_discards_old_delivery_queue(self):
        client = FakeClient(bound=True)
        code = self.row(17, "customer-wxid", "XYZ23456")
        state = {
            "personal_group_cloud_binding_id": "binding-1",
            "personal_group_cloud_binding_checked_at": int(time.time()),
            "personal_group_cloud_pending": [{"messageId": "old-session-result", "submitted": True}],
            "personal_group_cloud_outbox": [{"id": "old-project-file", "text": "private old project link"}],
        }
        sent = []
        result = self.run_once(client, [code], state=state, send_text=lambda _config, text: sent.append(text))
        self.assertEqual(client.claimed, [("group-exact-1", "XYZ23456", "message_4.db:server-17:17")])
        self.assertEqual(client.polled, [])
        self.assertEqual(client.submitted, [])
        self.assertNotIn("private old project link", sent)
        self.assertEqual(sent, ["此群已切换到新选择的 OverTree 项目和会话。"])
        self.assertEqual(result["state"]["personal_group_cloud_pending"], [])

    def test_unbound_mentioned_message_explains_that_the_group_needs_a_fresh_code(self):
        client = FakeClient(bound=False)
        state = {}
        sent = []
        first = self.row(18, "customer-wxid", "@OverTree 帮我继续")
        second = self.row(19, "customer-wxid", "@OverTree 再试一次")
        self.run_once(client, [first], state=state, send_text=lambda _config, text: sent.append(text))
        self.run_once(client, [second], state=state, send_text=lambda _config, text: sent.append(text))
        self.assertEqual(len(sent), 1)
        self.assertIn("因空闲超时断开", sent[0])

    def test_terminal_result_is_delivered_and_pending_item_is_cleared(self):
        client = FakeClient(receipt={
            "status": "finished",
            "result": "已完成",
            "deliveryPlan": {
                "version": 1,
                "limits": {"maxTextBytes": 1800, "maxMessages": 5, "maxMediaFiles": 4, "maxArtifactBytes": 50 * 1024 * 1024},
                "textParts": ["已完成"],
                "files": [],
            },
        })
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
        self.assertEqual(sent, ["已完成"])
        self.assertNotIn("https://cloud.example", sent[0])
        self.assertEqual(result["metrics"]["pending"], 0)
        self.assertEqual(result["metrics"]["outbox"], 0)

    def test_terminal_result_queues_cloud_text_parts_and_media_without_local_policy(self):
        artifact = {
            "name": "chart.png",
            "path": "output/chart.png",
            "size": 1200,
            "url": "https://cloud.example/wechat/files/token",
            "mediaType": "image",
            "maxBytes": 50 * 1024 * 1024,
        }
        client = FakeClient(receipt={
            "status": "finished",
            "result": "原始结果忽略，本地必须消费 Cloud 计划",
            "deliveryPlan": {
                "version": 1,
                "limits": {"maxTextBytes": 1800, "maxMessages": 5, "maxMediaFiles": 4, "maxArtifactBytes": 50 * 1024 * 1024},
                "textParts": ["Cloud 第一段", "Cloud 第二段"],
                "files": [artifact],
            },
        })
        state = {
            "personal_group_cloud_pending": [{
                "messageId": "message_4.db:server-8:8",
                "submitted": True,
                "sourceRow": {"_message_db": "message_4.db", "local_id": 8, "server_id": "server-8"},
            }]
        }
        sent_text = []
        sent_files = []

        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "chart.png"
            downloaded.write_bytes(b"png")
            with mock.patch.object(bridge, "_download_artifact", return_value=downloaded):
                self.run_once(client, [], state=state, send_text=lambda _config, text: sent_text.append(text),
                              send_file=lambda _config, file_path: sent_files.append(file_path.name))
                self.assertEqual(sent_text, ["Cloud 第一段"])
                self.assertEqual(len(state["personal_group_cloud_outbox"]), 2)
                self.assertEqual(state["personal_group_cloud_outbox"][1]["artifact"], artifact)

                self.run_once(client, [], state=state, send_text=lambda _config, text: sent_text.append(text),
                              send_file=lambda _config, file_path: sent_files.append(file_path.name))
                self.run_once(client, [], state=state, send_text=lambda _config, text: sent_text.append(text),
                              send_file=lambda _config, file_path: sent_files.append(file_path.name))
        self.assertEqual(sent_text, ["Cloud 第一段", "Cloud 第二段"])
        self.assertEqual(sent_files, ["chart.png"])
        self.assertEqual(state["personal_group_cloud_outbox"], [])
        self.assertNotIn("https://cloud.example", " ".join(sent_text))

    def test_personal_group_rejects_an_unknown_delivery_plan_version(self):
        with self.assertRaisesRegex(bridge.PersonalGroupCloudError, "unsupported WeChat delivery plan"):
            bridge._validate_delivery_plan({"deliveryPlan": {"version": 2}})

    def test_personal_group_rejects_delivery_limits_outside_the_shared_v1_contract(self):
        plan = {
            "version": 1,
            "limits": {
                "maxTextBytes": 1800,
                "maxMessages": 5,
                "maxMediaFiles": 4,
                "maxArtifactBytes": 50 * 1024 * 1024,
            },
            "textParts": ["完成"],
            "files": [],
        }
        for key, value in (("maxMessages", 6), ("maxMediaFiles", 5), ("maxArtifactBytes", 100 * 1024 * 1024)):
            with self.subTest(limit=key), self.assertRaisesRegex(bridge.PersonalGroupCloudError, "unsupported WeChat delivery limits"):
                bridge._validate_delivery_plan({"deliveryPlan": {**plan, "limits": {**plan["limits"], key: value}}})

    def test_new_cloud_task_sends_the_same_processing_ack_as_enterprise_group(self):
        client = FakeClient()
        row = self.row(20, "customer-wxid", "@OverTree 请分析这份资料")
        sent = []
        result = self.run_once(client, [row], send_text=lambda _config, text: sent.append(text))
        self.assertEqual(result["task_enqueued"], "run-1")
        self.assertEqual(sent, ["已收到，OverTree 正在处理。完成后会把结果发到本群。"])

    def test_cloud_artifact_requires_explicit_https_origin(self):
        with self.assertRaises(bridge.PersonalGroupCloudError):
            bridge._download_artifact(
                {"personal_group_cloud": {}},
                {"url": "https://files.example/result.pdf", "name": "result.pdf", "maxBytes": 50 * 1024 * 1024},
            )
        with self.assertRaises(bridge.PersonalGroupCloudError):
            bridge._download_artifact(
                {"personal_group_cloud": {"artifact_base_url": "https://cloud.example"}},
                {"url": "https://other.example/result.pdf", "name": "result.pdf", "maxBytes": 50 * 1024 * 1024},
            )

    def test_cloud_artifact_redirect_is_rejected_and_partial_file_removed(self):
        handlers = []

        class RedirectingOpener:
            def open(self, request, timeout):
                handlers[0].redirect_request(request, None, 302, "Found", {}, "https://other.example/file")

        def build_redirect_rejecting_opener(handler):
            handlers.append(handler)
            return RedirectingOpener()

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            bridge, "PRIVATE", Path(directory)
        ), mock.patch.object(bridge, "build_opener", side_effect=build_redirect_rejecting_opener):
            with self.assertRaisesRegex(bridge.PersonalGroupCloudError, "redirects are not accepted"):
                bridge._download_artifact(
                    {"personal_group_cloud": {"artifact_base_url": "https://cloud.example"}},
                    {"url": "https://cloud.example/wechat/files/token", "name": "result.pdf", "maxBytes": 50 * 1024 * 1024},
                )
            self.assertEqual(list(Path(directory).rglob("*.partial")), [])

    def test_cloud_artifact_download_obeys_the_limit_from_the_delivery_plan(self):
        class LargeResponse:
            headers = {"Content-Length": "4"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FixedOpener:
            def open(self, _request, timeout):
                return LargeResponse()

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(bridge, "PRIVATE", Path(directory)), \
                mock.patch.object(bridge, "build_opener", return_value=FixedOpener()):
            with self.assertRaisesRegex(bridge.PersonalGroupCloudError, "exceeds the local delivery limit"):
                bridge._download_artifact(
                    {"personal_group_cloud": {"artifact_base_url": "https://cloud.example"}},
                    {
                        "url": "https://cloud.example/wechat/files/token",
                        "name": "result.pdf",
                        "maxBytes": 3,
                    },
                )
            self.assertEqual(list(Path(directory).rglob("*.partial")), [])

    def test_file_outbox_sends_cloud_artifact_through_guarded_file_callback(self):
        state = {"personal_group_cloud_outbox": [{
            "id": "artifact-1",
            "kind": "file",
            "artifact": {"url": "https://cloud.example/wechat/files/token", "name": "result.pdf", "maxBytes": 50 * 1024 * 1024},
            "sourceRow": {},
        }]}
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.pdf"
            path.write_bytes(b"pdf")
            with mock.patch.object(bridge, "_download_artifact", return_value=path):
                result = bridge._flush_outbox(
                    self.config(), state, lambda *_args: None, time.time(),
                    send_file=lambda _config, file_path: sent.append((file_path.name, file_path.read_bytes())),
                )
        self.assertEqual(result, ("", None))
        self.assertEqual(sent, [("result.pdf", b"pdf")])
        self.assertEqual(state["personal_group_cloud_outbox"], [])
        self.assertFalse(path.exists())

    def test_successful_file_retry_removes_only_its_stale_failure_warning(self):
        state = {"personal_group_cloud_outbox": [{
            "id": "artifact-1",
            "kind": "file",
            "artifact": {"url": "https://cloud.example/wechat/files/token", "name": "result.png", "maxBytes": 50 * 1024 * 1024},
            "resultId": "result-1",
            "sourceRow": {},
        }, {
            "id": "file-warning-artifact-1",
            "kind": "file-warning",
            "text": "文件“result.png”暂时无法发送，请在 OverTree 项目文件区查看。",
            "relatedArtifactId": "artifact-1",
            "resultId": "result-1",
        }, {
            "id": "file-warning-artifact-2",
            "kind": "file-warning",
            "text": "文件“other.pdf”暂时无法发送，请在 OverTree 项目文件区查看。",
            "relatedArtifactId": "artifact-2",
            "resultId": "result-1",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.png"
            path.write_bytes(b"image")
            with mock.patch.object(bridge, "_download_artifact", return_value=path):
                sent, error = bridge._flush_outbox(
                    self.config(), state, lambda *_args: None, time.time(), send_file=lambda *_args: None
                )

        self.assertEqual((sent, error), ("", None))
        self.assertEqual([item["id"] for item in state["personal_group_cloud_outbox"]], ["file-warning-artifact-2"])

    def test_permanently_missing_cloud_artifact_does_not_block_following_outbox_items(self):
        state = {"personal_group_cloud_outbox": [{
            "id": "artifact-1",
            "kind": "file",
            "text": "文件：old.png",
            "artifact": {"url": "https://cloud.example/wechat/files/old", "name": "old.png"},
            "sourceRow": {},
        }, {
            "id": "reply-1",
            "kind": "result",
            "text": "结果文字",
            "sourceRow": {},
        }]}
        with mock.patch.object(
            bridge,
            "_download_artifact",
            side_effect=bridge.PersonalGroupCloudError("expired", status_code=404),
        ):
            sent, error = bridge._flush_outbox(
                self.config(), state, lambda *_args: None, time.time(), send_file=lambda *_args: None
            )

        self.assertIsNone(sent)
        self.assertIsNone(error)
        self.assertEqual(len(state["personal_group_cloud_outbox"]), 2)
        self.assertEqual(state["personal_group_cloud_outbox"][0]["id"], "reply-1")
        self.assertIn("old.png", state["personal_group_cloud_outbox"][1]["text"])
        self.assertEqual(state["personal_group_cloud_outbox"][1]["relatedArtifactId"], "artifact-1")

    def test_ocr_uncertain_send_is_abandoned_without_blocking_following_items(self):
        state = {"personal_group_cloud_outbox": [
            {"id": "ack", "kind": "ack", "text": "received"},
            {"id": "result", "kind": "result", "text": "result"},
        ]}
        sender = mock.Mock(side_effect=RuntimeError("WECHAT_GUI_SEND_UNCERTAIN: draft remains"))
        config = {**self.config(), "ingress": "ocr_gui"}
        sent, error = bridge._flush_outbox(config, state, sender, 100)
        self.assertIsNone(sent)
        self.assertIn("abandoned", error)
        self.assertEqual(state["personal_group_cloud_outbox"][0]["id"], "result")
        self.assertEqual(state["personal_group_cloud_outbox"][-1]["kind"], "delivery-failure-notice")
        sent, error = bridge._flush_outbox(config, state, lambda *_args: None, 101)
        self.assertEqual((sent, error), ("result", None))
        self.assertEqual(sender.call_count, 1)

    def test_exhausted_legacy_ack_is_discarded_before_its_future_backoff_and_results_continue(self):
        state = {"personal_group_cloud_outbox": [{
            "id": "ack-old",
            "kind": "ack",
            "text": "received",
            "attempts": 71,
            "nextAttemptAt": 10000,
            "lastError": "RuntimeError: GUI unavailable",
        }, {
            "id": "result-text",
            "kind": "result",
            "text": "OverTree result",
        }, {
            "id": "result-image",
            "kind": "file",
            "text": "file: plot.png",
            "artifact": {"url": "https://cloud.example/wechat/files/image", "name": "plot.png", "maxBytes": 100},
        }]}
        send_text = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "plot.png"
            image.write_bytes(b"png")
            with mock.patch.object(bridge, "_download_artifact", return_value=image):
                sent, error = bridge._flush_outbox(self.config(), state, send_text, 100)
                self.assertIsNone(sent)
                self.assertIn("ack-old", error)
                self.assertEqual(state["personal_group_cloud_delivery_failures"][0]["attempts"], 71)
                self.assertEqual(state["personal_group_cloud_outbox"][0]["id"], "result-text")

                sent, error = bridge._flush_outbox(self.config(), state, send_text, 101)
                self.assertEqual((sent, error), ("OverTree result", None))
                sent, error = bridge._flush_outbox(
                    self.config(), state, send_text, 102,
                    send_file=lambda _config, path: self.assertEqual(path.read_bytes(), b"png"),
                )

        self.assertEqual((sent, error), ("file: plot.png", None))
        self.assertEqual([call.args[1] for call in send_text.call_args_list], ["OverTree result"])
        self.assertEqual(state["personal_group_cloud_outbox"][-1]["kind"], "delivery-failure-notice")

    def test_fourth_failed_send_is_abandoned_and_reported(self):
        state = {"personal_group_cloud_outbox": [{
            "id": "result-1",
            "kind": "result",
            "text": "OverTree result",
            "attempts": 3,
        }]}
        sender = mock.Mock(side_effect=RuntimeError("GUI unavailable"))
        sent, error = bridge._flush_outbox(self.config(), state, sender, 100)

        self.assertIsNone(sent)
        self.assertIn("abandoned after 4 attempt(s)", error)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(state["personal_group_cloud_delivery_failures"][0]["reason"], "retry_limit")
        self.assertEqual(state["personal_group_cloud_outbox"][0]["kind"], "delivery-failure-notice")

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
            self.assertEqual(saved_state["personal_group_cloud_claim_attempt_ids"][0]["messageId"], "message_4.db:server-13:13")
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
