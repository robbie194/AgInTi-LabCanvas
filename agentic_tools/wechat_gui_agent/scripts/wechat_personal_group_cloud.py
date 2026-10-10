#!/usr/bin/env python3
"""Isolated LabCanvas transport adapter for OverTree Cloud personal groups.

This module never reads WeChat state itself. The existing direct-chat monitor
provides exact allowlisted-group messages; this adapter signs them to Cloud and
keeps durable result-poll state in that monitor's private JSON state file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


LINK_CODE_RE = re.compile(r"^(?:绑定\s*)?([A-HJ-NP-Z2-9]{8})$", re.IGNORECASE)
MAX_PENDING = 100
MAX_ATTACHMENTS = 10
ROOT = Path(__file__).resolve().parents[3]
PRIVATE = ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"
MEDIA_SYNC_SCRIPT = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_media_sync.py"


class PersonalGroupCloudError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class PersonalGroupCloudClient:
    def __init__(self, *, base_url: str, secret: str, agent_account_id: str, timeout: float = 20.0,
                 opener: Callable[..., Any] = urlopen):
        self.base_url = str(base_url or "").rstrip("/")
        self.secret = str(secret or "").strip()
        self.agent_account_id = str(agent_account_id or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.opener = opener
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise PersonalGroupCloudError("Personal-group Cloud URL must be an HTTP(S) origin without credentials or a path prefix.")
        if len(self.secret) < 32:
            raise PersonalGroupCloudError("Personal-group Cloud secret must contain at least 32 characters.")
        if not self.agent_account_id:
            raise PersonalGroupCloudError("Personal-group Agent account ID is required.")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._request(method, path, raw, "application/json" if body is not None else "")

    def request_bytes(self, method: str, path: str, body: bytes, *, content_type: str,
                      extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self._request(method, path, body, content_type, extra_headers=extra_headers)

    def _request(self, method: str, path: str, raw: bytes, content_type: str,
                 *, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        timestamp = str(int(time.time()))
        nonce = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
        digest = hashlib.sha256(raw).hexdigest()
        canonical = "\n".join((method.upper(), path, timestamp, nonce, digest)).encode("utf-8")
        signature = base64.urlsafe_b64encode(hmac.new(self.secret.encode(), canonical, hashlib.sha256).digest()).decode("ascii").rstrip("=")
        headers = {
            "Accept": "application/json",
            "X-OverTree-WeChat-Timestamp": timestamp,
            "X-OverTree-WeChat-Nonce": nonce,
            "X-OverTree-WeChat-Signature": signature,
        }
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(extra_headers or {})
        request = Request(f"{self.base_url}{path}", data=raw if method.upper() not in {"GET", "HEAD"} else None, headers=headers, method=method.upper())
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                payload = {}
            status = int(exc.code)
            raise PersonalGroupCloudError(
                str(payload.get("error") or f"OverTree Cloud returned HTTP {status}.")[:500],
                status_code=status,
                retryable=status >= 500 or status in {409, 429},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PersonalGroupCloudError("OverTree Cloud connection failed.", retryable=True) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PersonalGroupCloudError("OverTree Cloud returned an invalid response.", retryable=True) from exc
        if not isinstance(payload, dict):
            raise PersonalGroupCloudError("OverTree Cloud returned an invalid response.", retryable=True)
        return payload

    def claim(self, group_id: str, code: str) -> dict[str, Any]:
        return self.request("POST", "/internal/wechat/personal-groups/v1/bindings/claim", {
            "agentAccountId": self.agent_account_id,
            "groupId": group_id,
            "code": code,
        })

    def resolve(self, group_id: str) -> dict[str, Any]:
        query = urlencode({"agentAccountId": self.agent_account_id, "groupId": group_id})
        return self.request("GET", f"/internal/wechat/personal-groups/v1/bindings/resolve?{query}")

    def submit(self, group_id: str, message: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/internal/wechat/personal-groups/v1/messages", {
            "agentAccountId": self.agent_account_id,
            "groupId": group_id,
            **message,
        })

    def get_message(self, group_id: str, message_id: str) -> dict[str, Any]:
        query = urlencode({"agentAccountId": self.agent_account_id, "groupId": group_id})
        from urllib.parse import quote

        path = f"/internal/wechat/personal-groups/v1/messages/{quote(message_id, safe='')}?{query}"
        return self.request("GET", path)

    def upload_attachment(self, group_id: str, message_id: str, index: int, filename: str,
                          content_type: str, data: bytes) -> dict[str, Any]:
        query = urlencode({
            "channel": "wechat_personal_group",
            "agentAccountId": self.agent_account_id,
            "groupId": group_id,
            "messageId": message_id,
            "attachmentIndex": str(index),
            "filename": filename,
        })
        path = f"/internal/wechat/personal-groups/v1/attachments?{query}"
        # The Cloud raw upload handler reads this separate header for MIME metadata.
        return self.request_bytes(
            "PUT", path, data, content_type="application/octet-stream",
            extra_headers={"X-OverTree-WeChat-Content-Type": content_type or "application/octet-stream"},
        )


def personal_group_cloud_enabled(config: dict[str, Any]) -> bool:
    block = config.get("personal_group_cloud")
    return isinstance(block, dict) and block.get("enabled") is True


def build_client(config: dict[str, Any], *, environ: dict[str, str] | None = None,
                 opener: Callable[..., Any] = urlopen) -> tuple[PersonalGroupCloudClient, str]:
    env = os.environ if environ is None else environ
    block = config.get("personal_group_cloud") if isinstance(config.get("personal_group_cloud"), dict) else {}
    group_id = str(config.get("chatroom_id") or "").strip()
    allowlist = {value.strip() for value in str(env.get("WECHAT_PERSONAL_GROUP_ALLOWLIST") or "").split(",") if value.strip()}
    if not group_id or group_id not in allowlist:
        raise PersonalGroupCloudError("This exact WeChat group is not in WECHAT_PERSONAL_GROUP_ALLOWLIST.")
    if str(env.get("WECHAT_PERSONAL_GROUP_ENABLED") or "").lower() != "true":
        raise PersonalGroupCloudError("WECHAT_PERSONAL_GROUP_ENABLED must be true for this isolated group config.")
    client = PersonalGroupCloudClient(
        base_url=env.get("WECHAT_PERSONAL_GROUP_CLOUD_URL", ""),
        secret=env.get("WECHAT_PERSONAL_GROUP_SHARED_SECRET", ""),
        agent_account_id=env.get("WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID", ""),
        timeout=float(block.get("timeout_seconds") or 20),
        opener=opener,
    )
    return client, group_id


def _utf8_limit(value: str, maximum_bytes: int = 30_000) -> str:
    return str(value or "").encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore").strip()


def stable_message_id(row: dict[str, Any]) -> str:
    shard = Path(str(row.get("_message_db") or "message_0.db")).name
    server_id = str(row.get("server_id") or "").strip()
    local_id = str(row.get("local_id") or "").strip()
    if not local_id:
        raise PersonalGroupCloudError("A WeChat message is missing its local ID.")
    identity = f"{shard}:{server_id}:{local_id}"
    return identity[:240]


def message_actor(config: dict[str, Any], row: dict[str, Any]) -> tuple[str, str, str]:
    sender_id = str(row.get("sender") or "").strip()
    sender_name = str(row.get("sender_display") or sender_id or "群成员").strip()[:120]
    allowlisted_hashes = {
        str(item).lower() for item in (config.get("personal_group_cloud") or {}).get("support_member_sha256", [])
        if re.fullmatch(r"[0-9a-fA-F]{64}", str(item))
    }
    sender_hash = hashlib.sha256(sender_id.encode("utf-8")).hexdigest() if sender_id else ""
    role = "support_member" if sender_hash and sender_hash.lower() in allowlisted_hashes else "customer"
    return sender_id, sender_name, role


def binding_code(text: str) -> str:
    match = LINK_CODE_RE.fullmatch(str(text or "").strip())
    return match.group(1).upper() if match else ""


def _send(send_text: Callable[..., Any], config: dict[str, Any], state: dict[str, Any], text: str, source: dict[str, Any], kind: str) -> None:
    send_config = dict(config)
    send_config["_android_task_id"] = f"cloud-{kind}-" + hashlib.sha256(
        f"{config.get('chatroom_id')}|{source.get('_message_db')}|{source.get('local_id')}|{text}".encode("utf-8")
    ).hexdigest()[:24]
    send_text(send_config, text)


def _enqueue_outgoing(state: dict[str, Any], text: str, source: dict[str, Any], kind: str,
                      *, message_id: str = "") -> str:
    outbox = state.setdefault("personal_group_cloud_outbox", [])
    stable_id = message_id or hashlib.sha256(
        f"{kind}|{source.get('_message_db')}|{source.get('local_id')}|{text}".encode("utf-8")
    ).hexdigest()[:32]
    if not any(str(item.get("id") or "") == stable_id for item in outbox if isinstance(item, dict)):
        outbox.append({
            "id": stable_id,
            "text": text,
            "kind": kind,
            "sourceRow": {
                "_message_db": str(source.get("_message_db") or ""),
                "server_id": str(source.get("server_id") or ""),
                "local_id": int(source.get("local_id") or 0),
            },
            "attempts": 0,
            "nextAttemptAt": 0,
            "queuedAt": int(time.time()),
        })
    return stable_id


def _flush_outbox(config: dict[str, Any], state: dict[str, Any], send_text: Callable[..., Any], now: float) -> tuple[str | None, str | None]:
    outbox = state.setdefault("personal_group_cloud_outbox", [])
    if not outbox:
        return None, None
    item = outbox[0]
    if float(item.get("nextAttemptAt") or 0) > now:
        return None, None
    sending_at = float(item.get("sendingAt") or 0)
    if sending_at and now - sending_at < 45:
        return None, None
    item["sendingAt"] = now
    _persist_local_state(config, state)
    try:
        _send(send_text, config, state, str(item.get("text") or ""), item.get("sourceRow") or {}, str(item.get("kind") or "reply"))
    except Exception as exc:
        attempts = int(item.get("attempts") or 0) + 1
        item["attempts"] = attempts
        item["lastError"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        item["nextAttemptAt"] = now + min(300, 2 ** min(attempts, 8))
        item.pop("sendingAt", None)
        return None, item["lastError"]
    outbox.pop(0)
    return str(item.get("text") or ""), None


def _media_sync_event_files(config: dict[str, Any], row: dict[str, Any]) -> list[Path]:
    """Materialize only files carrying a media token from this exact message."""
    from wechat_direct_chatops import media_reference_tokens

    tokens = media_reference_tokens([row])
    if not tokens:
        return []
    create_time = int(row.get("create_time") or 0)
    now = int(time.time())
    start = max(0, create_time - 300) if create_time else max(0, now - 300)
    end = min(now + 60, create_time + 300) if create_time else now + 60
    command = [
        sys.executable, str(MEDIA_SYNC_SCRIPT), "--chat", str(config.get("chat_name") or "wechat-chat"),
        "--auto-source", "--since-epoch", str(start), "--until-epoch", str(end),
        "--summary-only", "--record-empty", "--require-token-match", "--db",
        str(Path(config.get("mirror_db") or PRIVATE / "wechat_mirror.sqlite")),
    ]
    for token in tokens:
        command.extend(("--match-token", token))
    timeout = max(5, int((config.get("personal_group_cloud") or {}).get("media_sync_timeout_seconds") or 30))
    try:
        proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PersonalGroupCloudError(f"Could not sync this message's exact WeChat attachments: {type(exc).__name__}.", retryable=True) from exc
    if proc.returncode != 0:
        raise PersonalGroupCloudError("Could not sync this message's exact WeChat attachments.", retryable=True)
    try:
        summary = json.loads(proc.stdout or "{}")
        event_id = int(summary.get("event_id") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not event_id:
        return []
    mirror_db = Path(config.get("mirror_db") or PRIVATE / "wechat_mirror.sqlite")
    if not mirror_db.is_file():
        return []
    try:
        with sqlite3.connect(mirror_db) as conn:
            result = conn.execute(
                "SELECT events.message FROM events JOIN chats ON chats.id=events.chat_id WHERE events.id=? AND chats.name=?",
                (event_id, str(config.get("chat_name") or "wechat-chat")),
            ).fetchone()
    except sqlite3.Error as exc:
        raise PersonalGroupCloudError("Could not read the exact WeChat media sync record.", retryable=True) from exc
    try:
        items = json.loads(result[0] or "[]") if result and result[0] else []
    except (TypeError, json.JSONDecodeError):
        return []
    download_root = (PRIVATE / "downloads").resolve()
    output: list[Path] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        matched_by = str(item.get("matched_by") or "")
        matched_token = matched_by.removeprefix("token:") if matched_by.startswith("token:") else ""
        associated_token = matched_by.removeprefix("associated:") if matched_by.startswith("associated:") else ""
        token_verified = any(
            (matched_token and (token.startswith(matched_token) or matched_token.startswith(token[:32])))
            or (associated_token and token == associated_token)
            for token in tokens
        )
        if not token_verified:
            continue
        candidate = Path(str(item.get("target") or "")).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(download_root)
            stat = resolved.stat()
        except (OSError, ValueError):
            continue
        if not resolved.is_file() or stat.st_size <= 0:
            continue
        output.append(resolved)
        if len(output) >= MAX_ATTACHMENTS:
            break
    return output


def _upload_attachments(client: PersonalGroupCloudClient, config: dict[str, Any], group_id: str,
                        item: dict[str, Any], *, message_id: str) -> list[dict[str, Any]]:
    source = item.get("sourceRow") if isinstance(item.get("sourceRow"), dict) else {}
    source_type = int(source.get("local_type") or 0) & 0xFFFFFFFF
    if source_type not in {3, 34, 42, 43, 47, 48, 49}:
        return []
    try:
        paths = _media_sync_event_files(config, source)
    except PersonalGroupCloudError:
        item["attachmentWarning"] = "未能从本地微信缓存安全提取本条附件，因此没有上传附件内容。"
        return []
    max_bytes = max(1, int(os.environ.get("WECHAT_PERSONAL_GROUP_MAX_ATTACHMENT_BYTES", str(50 * 1024 * 1024))))
    max_total_bytes = max(max_bytes, int(os.environ.get("WECHAT_PERSONAL_GROUP_MAX_ATTACHMENTS_TOTAL_BYTES", str(50 * 1024 * 1024))))
    total_bytes = 0
    results: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        try:
            size = path.stat().st_size
            if size > max_bytes:
                item["attachmentWarning"] = f"附件 {path.name} 超过单文件大小限制，未上传。"
                continue
            if total_bytes + size > max_total_bytes:
                item["attachmentWarning"] = f"附件 {path.name} 超过本条消息的附件总量限制，未上传。"
                continue
            data = path.read_bytes()
        except OSError:
            item["attachmentWarning"] = f"附件 {path.name} 无法从本地缓存读取，因此没有上传。"
            continue
        content_type = _content_type(path)
        try:
            uploaded = client.upload_attachment(group_id, message_id, index, path.name, content_type, data)
        except PersonalGroupCloudError as exc:
            if exc.status_code in {400, 413, 415}:
                item["attachmentWarning"] = f"附件 {path.name} 未被 Cloud 接受，已作为未上传附件处理。"
                continue
            raise
        relative_path = str(uploaded.get("path") or "").strip()
        if not relative_path:
            raise PersonalGroupCloudError("Cloud did not return the stored attachment path.", retryable=True)
        results.append({
            "itemIndex": index,
            "path": relative_path,
            "name": str(uploaded.get("name") or path.name),
            "contentType": content_type,
            "size": int(uploaded.get("size") or size),
        })
        total_bytes += size
    return results


def _content_type(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _reconcile_outgoing_echoes(state: dict[str, Any], rows: list[dict[str, Any]], *,
                               inbound: Callable[[dict[str, Any]], bool],
                               visible_text: Callable[[dict[str, Any]], str], now: float) -> None:
    outbox = state.get("personal_group_cloud_outbox")
    if not isinstance(outbox, list) or not outbox:
        return
    own_echoes: list[tuple[str, int]] = []
    for row in rows:
        if inbound(row):
            continue
        text = str(visible_text(row) or "").strip()
        if not text:
            continue
        try:
            created = int(row.get("create_time") or 0)
        except (TypeError, ValueError):
            created = 0
        own_echoes.append((text, created))
    remaining = []
    for item in outbox:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        queued_at = int(item.get("queuedAt") or now)
        delivered_echo = any(
            echo == text and (not created or created >= queued_at - 10)
            for echo, created in own_echoes
        )
        if not delivered_echo:
            remaining.append(item)
    state["personal_group_cloud_outbox"] = remaining


def _persist_local_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    raw_path = str(config.get("_runtime_state_path") or "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".personal-group.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _claim_private_group_state(config: dict[str, Any], state: dict[str, Any], group_id: str) -> None:
    """Require private, group-owned durable state before handling Cloud data."""
    raw_path = str(config.get("_runtime_state_path") or "").strip()
    if not raw_path:
        raise PersonalGroupCloudError("Personal-group Cloud mode requires a dedicated private state file for this group.")
    private_root = PRIVATE.resolve()
    state_path = Path(raw_path).expanduser().resolve()
    try:
        state_path.relative_to(private_root)
    except ValueError as exc:
        raise PersonalGroupCloudError("Personal-group Cloud state must stay inside the LabCanvas private directory.") from exc
    if state_path == private_root or not state_path.name:
        raise PersonalGroupCloudError("Personal-group Cloud state must name a private file.")

    owner = str(state.get("personal_group_cloud_owner_group_id") or "")
    if owner and owner != group_id:
        raise PersonalGroupCloudError("This private state file belongs to a different WeChat group; give each group its own state file.")

    # The sidecar also prevents two live monitor configs from claiming one
    # initially-empty state file at the same time.
    PRIVATE.mkdir(parents=True, exist_ok=True)
    owner_path = state_path.with_name(state_path.name + ".personal-group-owner")
    try:
        with owner_path.open("x", encoding="utf-8") as handle:
            handle.write(group_id + "\n")
    except FileExistsError:
        try:
            existing_owner = owner_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PersonalGroupCloudError("Could not verify the private WeChat group state owner.") from exc
        if existing_owner != group_id:
            raise PersonalGroupCloudError("This private state file is already assigned to a different WeChat group.")

    state["personal_group_cloud_owner_group_id"] = group_id
    config["_runtime_state_path"] = str(state_path)
    _persist_local_state(config, state)


def run_once(config: dict[str, Any], state: dict[str, Any], rows: list[dict[str, Any]], *, send: bool,
             eligible: Callable[[dict[str, Any]], bool], inbound: Callable[[dict[str, Any]], bool],
             visible_text: Callable[[dict[str, Any]], str], send_text: Callable[..., Any],
             client_factory: Callable[[dict[str, Any]], tuple[PersonalGroupCloudClient, str]] = build_client) -> dict[str, Any]:
    """Submit/poll one bound group without invoking LabCanvas's local agent."""
    if not send:
        raise PersonalGroupCloudError("Personal-group Cloud mode requires the guarded --send transport.")
    client, group_id = client_factory(config)
    _claim_private_group_state(config, state, group_id)
    now = time.time()
    pending = state.setdefault("personal_group_cloud_pending", [])
    outbox = state.setdefault("personal_group_cloud_outbox", [])
    if not rows and not pending and not outbox:
        return {
            "response_sent": None,
            "task_enqueued": None,
            "processed_rows": 0,
            "metrics": {"transport": "personal_group_cloud", "idle": True},
            "state": state,
        }
    response_sent = None
    task_enqueued = None

    # Poll existing runs first. Never start a second turn on a session that is
    # still running; preserve later messages in the private cursor-backed queue.
    resolved_binding = None
    cached_binding_id = str(state.get("personal_group_cloud_binding_id") or "")
    has_inbound_rows = any(inbound(row) for row in rows)
    has_submitted_work = any(isinstance(item, dict) and item.get("submitted") for item in pending)
    last_checked = float(state.get("personal_group_cloud_binding_checked_at") or 0)
    should_resolve = has_inbound_rows or (not cached_binding_id and has_submitted_work) or (cached_binding_id and now - last_checked >= 60)
    revoked_binding = False
    if should_resolve:
        try:
            resolved_binding = client.resolve(group_id)
            cached_binding_id = str(resolved_binding.get("bindingId") or "")
            state["personal_group_cloud_binding_id"] = cached_binding_id
            state.pop("personal_group_cloud_unbound_notified", None)
        except PersonalGroupCloudError as exc:
            if exc.status_code != 404:
                raise
            revoked_binding = bool(cached_binding_id)
            cached_binding_id = ""
            state.pop("personal_group_cloud_binding_id", None)
            if revoked_binding:
                state["personal_group_cloud_pending"] = []
                state["personal_group_cloud_outbox"] = []
                pending = state["personal_group_cloud_pending"]
                outbox = state["personal_group_cloud_outbox"]
                state["personal_group_cloud_revoked_at"] = int(now)
        state["personal_group_cloud_binding_checked_at"] = int(now)
    elif cached_binding_id:
        resolved_binding = {"ok": True, "bindingId": cached_binding_id}

    remaining: list[dict[str, Any]] = []
    for item in pending:
        if not item.get("submitted"):
            remaining.append(item)
            continue
        if float(item.get("nextPollAt") or 0) > now:
            remaining.append(item)
            continue
        receipt = client.get_message(group_id, str(item["messageId"]))
        status = str(receipt.get("status") or "running").lower()
        if status in {"running", "queued", "processing", "starting"}:
            attempts = int(item.get("pollAttempts") or 0) + 1
            item["pollAttempts"] = attempts
            item["nextPollAt"] = now + min(30, 2 ** min(attempts, 5))
            remaining.append(item)
            continue
        result = str(receipt.get("result") or "").strip()
        if status in {"failed", "error", "stopped"}:
            result = result or "这次 OverTree 任务没有完成，请在 OverTree 会话中查看状态后重试。"
        file_lines = [f"文件：{str(file.get('url') or '')}" for file in receipt.get("files", []) if isinstance(file, dict) and file.get("url")]
        outgoing = "\n\n".join(part for part in (result or "OverTree 已完成处理。", "\n".join(file_lines)) if part)
        source = item.get("sourceRow") if isinstance(item.get("sourceRow"), dict) else {}
        item["result"] = outgoing
        item["resultReady"] = True
        item["resultSource"] = source
        _enqueue_outgoing(state, outgoing, source, "result", message_id=f"result-{item['messageId']}")
        remaining.append(item)
        break
    state["personal_group_cloud_pending"] = remaining
    pending = state["personal_group_cloud_pending"]

    inbound_rows = [row for row in rows if inbound(row)]
    trigger_rows = [row for row in inbound_rows if eligible(row)]
    _reconcile_outgoing_echoes(state, rows, inbound=inbound, visible_text=visible_text, now=now)
    attempted_claim_ids = state.setdefault("personal_group_cloud_claim_attempt_ids", [])
    attempted_claims = {str(item) for item in attempted_claim_ids}
    handled_binding_ids: set[str] = set()

    # A prior claim can commit in Cloud while its HTTP response is lost. The
    # checkpointed claim ID lets the next pass recognize the now-bound group
    # without forwarding the one-time code into the user conversation.
    if resolved_binding and attempted_claims:
        for row in trigger_rows:
            message_id = stable_message_id(row)
            if message_id not in attempted_claims:
                continue
            handled_binding_ids.add(message_id)
            _enqueue_outgoing(state, "此群已连接到对应的 OverTree 项目和会话。客户与已登记客服的消息会进入同一会话。", row, "binding-recovered")
        if handled_binding_ids:
            state["personal_group_cloud_claim_attempt_ids"] = [item for item in attempted_claim_ids if str(item) not in handled_binding_ids]

    # A one-time Cloud binding code may arrive as a bare code or "绑定 CODE".
    if not resolved_binding:
        for row in trigger_rows:
            code = binding_code(visible_text(row))
            if not code:
                continue
            message_id = stable_message_id(row)
            if message_id not in attempted_claims:
                attempted_claim_ids.append(message_id)
                state["personal_group_cloud_claim_attempt_ids"] = attempted_claim_ids[-MAX_PENDING:]
                attempted_claims.add(message_id)
                _persist_local_state(config, state)
            claim = client.claim(group_id, code)
            handled_binding_ids.add(message_id)
            state["personal_group_cloud_claim_attempt_ids"] = [item for item in state["personal_group_cloud_claim_attempt_ids"] if str(item) != message_id]
            attempted_claims.discard(message_id)
            if claim.get("ok"):
                state["personal_group_cloud_binding_id"] = str(claim.get("bindingId") or "")
                state.pop("personal_group_cloud_unbound_notified", None)
                _enqueue_outgoing(state, "此群已连接到对应的 OverTree 项目和会话。客户与已登记客服的消息会进入同一会话。", row, "binding")
                resolved_binding = claim
            else:
                _enqueue_outgoing(state, "绑定码无效或已过期，请在 OverTree 中重新生成后再发送。", row, "binding-invalid")
        if not resolved_binding:
            state["personal_group_cloud_unbound_notified"] = True
            sent_outgoing, send_error = _flush_outbox(config, state, send_text, now)
            return {
                "response_sent": sent_outgoing,
                "task_enqueued": None,
                "processed_rows": len(rows),
                "metrics": {"transport": "personal_group_cloud", "binding": "unbound", "outbox": len(state.get("personal_group_cloud_outbox") or []), **({"send_error": send_error} if send_error else {})},
                "state": state,
            }

    trigger_rows = [row for row in trigger_rows if stable_message_id(row) not in handled_binding_ids]

    # Queue each sender separately so Cloud stores accurate author attribution.
    known_ids = {str(item.get("messageId") or "") for item in pending}
    for row in trigger_rows:
        message_id = stable_message_id(row)
        if message_id in known_ids:
            continue
        if len(pending) >= MAX_PENDING:
            raise PersonalGroupCloudError("Personal-group Cloud queue is full; pause this group and review pending work.")
        content = str(visible_text(row) or "").strip()
        if not content:
            continue
        actor_id, actor_name, actor_role = message_actor(config, row)
        pending.append({
            "messageId": message_id,
            "submitted": False,
            "content": content[:30_000],
            "actorId": actor_id,
            "actorName": actor_name,
            "actorRole": actor_role,
            "sourceRow": {
                "_message_db": str(row.get("_message_db") or ""),
                "server_id": str(row.get("server_id") or ""),
                "local_id": int(row.get("local_id") or 0),
                "local_type": int(row.get("local_type") or 0),
                "create_time": int(row.get("create_time") or 0),
                "content": str(row.get("content") or "")[:30_000],
            },
            "queuedAt": int(now),
        })
        known_ids.add(message_id)

    if pending and not any(item.get("submitted") for item in pending):
        item = pending[0]
        attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
        if not item.get("attachmentsUploaded"):
            try:
                attachments = _upload_attachments(client, config, group_id, item, message_id=item["messageId"])
            except PersonalGroupCloudError:
                raise
            item["attachments"] = attachments
            item["attachmentsUploaded"] = True
        content = str(item.get("content") or "")
        source_type = int((item.get("sourceRow") or {}).get("local_type") or 0) & 0xFFFFFFFF
        attachment_warning = str(item.get("attachmentWarning") or "")
        if source_type in {3, 34, 42, 43, 47, 48, 49} and not attachments and not attachment_warning:
            attachment_warning = "本条微信附件未能从本地缓存精确定位，未附加到会话。"
        if attachment_warning:
            content = f"{_utf8_limit(content, 28_000)}\n[OverTree 提示：{attachment_warning}]".strip()
        receipt = client.submit(group_id, {
            "messageId": item["messageId"],
            "actorId": item["actorId"],
            "actorRole": item["actorRole"],
            "actorName": item["actorName"],
            "content": _utf8_limit(content),
            "attachments": attachments,
        })
        item["submitted"] = True
        item["submittedAt"] = int(now)
        task_enqueued = str(receipt.get("responseId") or item["messageId"])
        if str(receipt.get("status") or "running").lower() not in {"running", "queued", "processing", "starting"}:
            # The next loop performs the same idempotent poll/delivery path.
            pass

    state["personal_group_cloud_pending"] = pending
    sent_outgoing, send_error = _flush_outbox(config, state, send_text, now)
    if sent_outgoing:
        response_sent = sent_outgoing
    if send_error:
        state["personal_group_cloud_send_error"] = send_error
    else:
        state.pop("personal_group_cloud_send_error", None)
    state["personal_group_cloud_outbox"] = list(state.get("personal_group_cloud_outbox") or [])
    if state["personal_group_cloud_pending"] and state["personal_group_cloud_pending"][0].get("resultReady"):
        queued_id = f"result-{state['personal_group_cloud_pending'][0].get('messageId')}"
        if not any(str(item.get("id") or "") == queued_id for item in state["personal_group_cloud_outbox"] if isinstance(item, dict)):
            state["personal_group_cloud_pending"] = state["personal_group_cloud_pending"][1:]
    return {
        "response_sent": response_sent,
        "task_enqueued": task_enqueued,
        "processed_rows": len(rows),
        "metrics": {"transport": "personal_group_cloud", "pending": len(state["personal_group_cloud_pending"]), "outbox": len(state["personal_group_cloud_outbox"]), **({"send_error": send_error} if send_error else {})},
        "state": state,
    }
