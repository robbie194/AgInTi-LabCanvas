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
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


LINK_CODE_RE = re.compile(r"^(?:绑定\s*)?([A-HJ-NP-Z2-9]{8})$", re.IGNORECASE)
MAX_PENDING = 100
MAX_ATTACHMENTS = 10
WECHAT_DELIVERY_PLAN_V1_LIMITS = {
    "maxTextBytes": 1800,
    "maxMessages": 5,
    "maxMediaFiles": 4,
    "maxArtifactBytes": 50 * 1024 * 1024,
}
ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve()
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

    def claim(self, group_id: str, code: str, claim_message_id: str) -> dict[str, Any]:
        return self.request("POST", "/internal/wechat/personal-groups/v1/bindings/claim", {
            "agentAccountId": self.agent_account_id,
            "groupId": group_id,
            "code": code,
            "claimMessageId": claim_message_id,
        })

    def resolve(self, group_id: str) -> dict[str, Any]:
        query = urlencode({"agentAccountId": self.agent_account_id, "groupId": group_id})
        return self.request("GET", f"/internal/wechat/personal-groups/v1/bindings/resolve?{query}")

    def list_registered_groups(self) -> dict[str, Any]:
        query = urlencode({"agentAccountId": self.agent_account_id})
        return self.request("GET", f"/internal/wechat/personal-groups/v1/groups?{query}")

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
    personal_enabled = str(env.get("WECHAT_PERSONAL_GROUP_ENABLED") or "").lower() == "true"
    ocr_enabled = (
        str(config.get("ingress") or "") == "ocr_gui"
        and str(env.get("WECHAT_OCR_GROUP_ENABLED") or "").lower() == "true"
    )
    if not (personal_enabled or ocr_enabled):
        raise PersonalGroupCloudError("The personal-group or OCR-group feature flag must be enabled for this config.")
    client = PersonalGroupCloudClient(
        base_url=env.get("WECHAT_PERSONAL_GROUP_CLOUD_URL", ""),
        secret=env.get("WECHAT_PERSONAL_GROUP_SHARED_SECRET", ""),
        agent_account_id=env.get("WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID", ""),
        timeout=float(block.get("timeout_seconds") or 20),
        opener=opener,
    )
    return client, group_id


def build_registry_client(*, environ: dict[str, str] | None = None,
                          opener: Callable[..., Any] = urlopen) -> PersonalGroupCloudClient:
    env = os.environ if environ is None else environ
    if str(env.get("WECHAT_OCR_GROUP_ENABLED") or "").lower() != "true":
        raise PersonalGroupCloudError("OCR personal-group ingress is disabled.")
    return PersonalGroupCloudClient(
        base_url=env.get("WECHAT_PERSONAL_GROUP_CLOUD_URL", ""),
        secret=env.get("WECHAT_PERSONAL_GROUP_SHARED_SECRET", ""),
        agent_account_id=env.get("WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID", ""),
        timeout=float(env.get("WECHAT_PERSONAL_GROUP_TIMEOUT_SECONDS") or 20),
        opener=opener,
    )


def _utf8_limit(value: str, maximum_bytes: int = 30_000) -> str:
    return str(value or "").encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore").strip()


def _validate_delivery_plan(receipt: dict[str, Any]) -> dict[str, Any]:
    plan = receipt.get("deliveryPlan") if isinstance(receipt, dict) else None
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise PersonalGroupCloudError("OverTree Cloud returned an unsupported WeChat delivery plan.", status_code=502, retryable=True)
    limits = plan.get("limits")
    text_parts = plan.get("textParts")
    files = plan.get("files")
    if not isinstance(limits, dict) or not isinstance(text_parts, list) or not text_parts or not isinstance(files, list):
        raise PersonalGroupCloudError("OverTree Cloud returned an invalid WeChat delivery plan.", status_code=502, retryable=True)
    limit_names = ("maxTextBytes", "maxMessages", "maxMediaFiles", "maxArtifactBytes")
    if any(not isinstance(limits.get(name), int) or isinstance(limits.get(name), bool) or limits[name] <= 0 for name in limit_names):
        raise PersonalGroupCloudError("OverTree Cloud returned invalid WeChat delivery limits.", status_code=502, retryable=True)
    if limits != WECHAT_DELIVERY_PLAN_V1_LIMITS:
        raise PersonalGroupCloudError("OverTree Cloud returned unsupported WeChat delivery limits.", status_code=502, retryable=True)
    if len(files) > limits["maxMediaFiles"] or len(files) > limits["maxMessages"] or len(text_parts) + len(files) > limits["maxMessages"]:
        raise PersonalGroupCloudError("OverTree Cloud returned a WeChat delivery plan over its message limit.", status_code=502, retryable=True)
    if any(not isinstance(part, str) or len(part.encode("utf-8")) > limits["maxTextBytes"] for part in text_parts):
        raise PersonalGroupCloudError("OverTree Cloud returned an oversized WeChat text part.", status_code=502, retryable=True)
    for artifact in files:
        if not isinstance(artifact, dict) or not artifact.get("url") or not artifact.get("name"):
            raise PersonalGroupCloudError("OverTree Cloud returned invalid WeChat artifact metadata.", status_code=502, retryable=True)
        if artifact.get("mediaType") not in {"image", "file"}:
            raise PersonalGroupCloudError("OverTree Cloud returned an unsupported WeChat media type.", status_code=502, retryable=True)
        maximum = artifact.get("maxBytes")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum != limits["maxArtifactBytes"]:
            raise PersonalGroupCloudError("OverTree Cloud returned an invalid WeChat artifact size limit.", status_code=502, retryable=True)
        size = artifact.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > maximum):
            raise PersonalGroupCloudError("OverTree Cloud returned an artifact larger than its delivery limit.", status_code=502, retryable=True)
    return plan


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
    # Group membership, not a customer/support roster, determines who may
    # address the Agent. Keep the sender identity for attribution while
    # treating every non-Agent participant with the same role.
    return sender_id, sender_name, "group_member"


def binding_code(text: str) -> str:
    match = LINK_CODE_RE.fullmatch(str(text or "").strip())
    return match.group(1).upper() if match else ""


def personal_group_trigger_prefixes(config: dict[str, Any]) -> list[str]:
    block = config.get("personal_group_cloud") if isinstance(config.get("personal_group_cloud"), dict) else {}
    raw = block.get("trigger_prefixes", [])
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def personal_group_message_is_addressed(config: dict[str, Any], row: dict[str, Any],
                                        visible_text: Callable[[dict[str, Any]], str]) -> bool:
    prefixes = personal_group_trigger_prefixes(config)
    text = str(visible_text(row) or "").casefold()
    return any(prefix.casefold() in text for prefix in prefixes)


def _send(send_text: Callable[..., Any], config: dict[str, Any], state: dict[str, Any], text: str, source: dict[str, Any], kind: str) -> None:
    send_config = dict(config)
    send_config["_android_task_id"] = f"cloud-{kind}-" + hashlib.sha256(
        f"{config.get('chatroom_id')}|{source.get('_message_db')}|{source.get('local_id')}|{text}".encode("utf-8")
    ).hexdigest()[:24]
    send_text(send_config, text)


def _enqueue_outgoing(state: dict[str, Any], text: str, source: dict[str, Any], kind: str,
                      *, message_id: str = "", artifact: dict[str, Any] | None = None,
                      result_id: str = "", related_artifact_id: str = "") -> str:
    outbox = state.setdefault("personal_group_cloud_outbox", [])
    stable_id = message_id or hashlib.sha256(
        f"{kind}|{source.get('_message_db')}|{source.get('local_id')}|{text}".encode("utf-8")
    ).hexdigest()[:32]
    if not any(str(item.get("id") or "") == stable_id for item in outbox if isinstance(item, dict)):
        outbox.append({
            "id": stable_id,
            "text": text,
            "kind": kind,
            **({"artifact": artifact} if artifact else {}),
            **({"resultId": result_id} if result_id else {}),
            **({"relatedArtifactId": related_artifact_id} if related_artifact_id else {}),
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


def _artifact_path(value: str) -> str:
    name = Path(str(value or "artifact").replace("\\", "/")).name
    name = re.sub(r"[^\w.\-\u0080-\uffff]+", "-", name).strip(".-")
    return (name[:160] or "artifact")


def _download_artifact(config: dict[str, Any], artifact: dict[str, Any]) -> Path:
    url = str(artifact.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise PersonalGroupCloudError("Cloud returned an invalid artifact URL.")
    expected_base = str(
        (config.get("personal_group_cloud") or {}).get("artifact_base_url")
        or os.environ.get("WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL")
        or ""
    ).strip().rstrip("/")
    base = urlsplit(expected_base)
    if (
        base.scheme != "https"
        or not base.netloc
        or base.path not in {"", "/"}
        or base.query
        or base.fragment
        or base.username
        or base.password
        or parsed.scheme != "https"
        or (base.scheme, base.netloc.lower()) != (parsed.scheme, parsed.netloc.lower())
    ):
        raise PersonalGroupCloudError("Cloud artifact URL must use the explicitly configured HTTPS artifact origin.")
    max_bytes = artifact.get("maxBytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise PersonalGroupCloudError("Cloud artifact is missing its delivery size limit.")
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "OverTree-LabCanvas/1"})
    filename = _artifact_path(artifact.get("name"))
    artifact_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    destination = (PRIVATE / "downloads" / "cloud-artifacts" / f"{artifact_key}-{filename}").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    total = 0
    try:
        class RejectRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, new_url):
                raise PersonalGroupCloudError("Cloud artifact redirects are not accepted.")

        opener = build_opener(RejectRedirect())
        with opener.open(request, timeout=30) as response, temporary.open("wb") as handle:
            length = int(response.headers.get("Content-Length") or 0)
            if length > max_bytes:
                raise PersonalGroupCloudError("Cloud artifact exceeds the local delivery limit.")
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PersonalGroupCloudError("Cloud artifact exceeds the local delivery limit.")
                handle.write(chunk)
        temporary.replace(destination)
        return destination
    except PersonalGroupCloudError:
        temporary.unlink(missing_ok=True)
        raise
    except HTTPError as exc:
        temporary.unlink(missing_ok=True)
        if int(exc.code or 0) in {404, 410}:
            raise PersonalGroupCloudError(
                "Cloud artifact link expired or is no longer available.",
                status_code=int(exc.code),
                retryable=False,
            ) from exc
        raise PersonalGroupCloudError("Could not download the Cloud artifact for WeChat delivery.", retryable=True) from exc
    except (URLError, TimeoutError, OSError) as exc:
        temporary.unlink(missing_ok=True)
        raise PersonalGroupCloudError("Could not download the Cloud artifact for WeChat delivery.", retryable=True) from exc


def _flush_outbox(config: dict[str, Any], state: dict[str, Any], send_text: Callable[..., Any], now: float,
                  send_file: Callable[..., Any] | None = None) -> tuple[str | None, str | None]:
    outbox = state.setdefault("personal_group_cloud_outbox", [])
    if not outbox:
        return None, None
    item = outbox[0]
    if item.get("deliveryUncertain"):
        return None, "OCR delivery requires review before retry."
    if float(item.get("nextAttemptAt") or 0) > now:
        return None, None
    sending_at = float(item.get("sendingAt") or 0)
    if sending_at and now - sending_at < 45:
        return None, None
    item["sendingAt"] = now
    _persist_local_state(config, state)
    try:
        kind = str(item.get("kind") or "reply")
        if kind == "file":
            if send_file is None:
                raise PersonalGroupCloudError("This transport has no guarded file sender.")
            artifact_path = _download_artifact(config, item.get("artifact") or {})
            try:
                send_file(config, artifact_path)
            finally:
                artifact_path.unlink(missing_ok=True)
        else:
            _send(send_text, config, state, str(item.get("text") or ""), item.get("sourceRow") or {}, kind)
    except PersonalGroupCloudError as exc:
        if kind == "file" and exc.status_code in {404, 410}:
            outbox.pop(0)
            artifact = item.get("artifact") if isinstance(item.get("artifact"), dict) else {}
            name = _artifact_path(artifact.get("name") or item.get("text") or "结果文件")
            _enqueue_outgoing(
                state,
                f"文件“{name}”暂时无法发送，请在 OverTree 项目文件区查看。",
                item.get("sourceRow") or {},
                "file-warning",
                message_id=f"file-warning-{item.get('id')}" if item.get("id") else "",
                result_id=str(item.get("resultId") or ""),
                related_artifact_id=str(item.get("id") or ""),
            )
            return None, None
        attempts = int(item.get("attempts") or 0) + 1
        item["attempts"] = attempts
        item["lastError"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        if config.get("ingress") == "ocr_gui" and "WECHAT_GUI_SEND_UNCERTAIN" in str(exc):
            item["deliveryUncertain"] = True
        item["nextAttemptAt"] = now + min(300, 2 ** min(attempts, 8))
        item.pop("sendingAt", None)
        return None, item["lastError"]
    except Exception as exc:
        attempts = int(item.get("attempts") or 0) + 1
        item["attempts"] = attempts
        item["lastError"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        if config.get("ingress") == "ocr_gui" and "WECHAT_GUI_SEND_UNCERTAIN" in str(exc):
            item["deliveryUncertain"] = True
        item["nextAttemptAt"] = now + min(300, 2 ** min(attempts, 8))
        item.pop("sendingAt", None)
        return None, item["lastError"]
    outbox.pop(0)
    if kind == "file":
        artifact_id = str(item.get("id") or "")
        if artifact_id:
            outbox[:] = [
                queued for queued in outbox
                if not (
                    isinstance(queued, dict)
                    and str(queued.get("kind") or "") == "file-warning"
                    and str(queued.get("relatedArtifactId") or "") == artifact_id
                )
            ]
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
             send_file: Callable[..., Any] | None = None,
             client_factory: Callable[[dict[str, Any]], tuple[PersonalGroupCloudClient, str]] = build_client) -> dict[str, Any]:
    """Submit/poll one bound group without invoking LabCanvas's local agent."""
    if not send:
        raise PersonalGroupCloudError("Personal-group Cloud mode requires the guarded --send transport.")
    client, group_id = client_factory(config)
    _claim_private_group_state(config, state, group_id)
    prefixes = personal_group_trigger_prefixes(config)
    if not prefixes or any(not prefix.startswith(("@", "＠")) or len(prefix) < 2 for prefix in prefixes):
        raise PersonalGroupCloudError("Personal-group Cloud mode requires the exact Agent @mention prefix in personal_group_cloud.trigger_prefixes.")
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

    # Resolve the exact group binding before processing messages.
    resolved_binding = None
    cached_binding_id = str(state.get("personal_group_cloud_binding_id") or "")
    has_inbound_rows = any(inbound(row) for row in rows)
    has_submitted_work = any(isinstance(item, dict) and item.get("submitted") for item in pending)
    has_claim_attempts = bool(state.get("personal_group_cloud_claim_attempt_ids"))
    last_checked = float(state.get("personal_group_cloud_binding_checked_at") or 0)
    should_resolve = has_inbound_rows or (not cached_binding_id and (has_submitted_work or has_claim_attempts)) or (cached_binding_id and now - last_checked >= 60)
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
            state["personal_group_cloud_claim_attempt_ids"] = []
        state["personal_group_cloud_binding_checked_at"] = int(now)
    elif cached_binding_id:
        resolved_binding = {"ok": True, "bindingId": cached_binding_id}

    inbound_rows = [row for row in rows if inbound(row)]
    trigger_rows = [row for row in inbound_rows if eligible(row)]
    _reconcile_outgoing_echoes(state, rows, inbound=inbound, visible_text=visible_text, now=now)

    # Old state files stored only raw message IDs. Keep accepting that shape,
    # and store the source locator (never the binding code itself) after this
    # point so a lost HTTP response can be acknowledged safely after restart.
    attempts = state.setdefault("personal_group_cloud_claim_attempt_ids", [])
    attempt_sources = {
        str(item.get("messageId") or ""): item.get("sourceRow") or {}
        for item in attempts if isinstance(item, dict)
    }
    attempted_claims = {
        str(item.get("messageId") or "") if isinstance(item, dict) else str(item)
        for item in attempts
    }
    handled_binding_ids: set[str] = set()

    # Resolve a claim whose HTTP response was lost. Cloud returns the exact
    # source-message receipt ID, so a pre-existing binding can never be
    # mistaken for a successful session switch.
    if resolved_binding and attempted_claims:
        committed_claim = str(resolved_binding.get("lastClaimMessageId") or "")
        if committed_claim and committed_claim in attempted_claims:
            for row in inbound_rows:
                message_id = stable_message_id(row)
                if message_id == committed_claim:
                    attempt_sources[message_id] = row
                    handled_binding_ids.add(message_id)
            source = attempt_sources.get(committed_claim, {})
            state["personal_group_cloud_pending"] = []
            state["personal_group_cloud_outbox"] = []
            pending = state["personal_group_cloud_pending"]
            outbox = state["personal_group_cloud_outbox"]
            _enqueue_outgoing(state, "此群已连接到新选择的 OverTree 项目和会话。", source, "binding-recovered")
            state["personal_group_cloud_claim_attempt_ids"] = [
                item for item in attempts
                if (str(item.get("messageId") or "") if isinstance(item, dict) else str(item)) != committed_claim
            ]
        elif committed_claim:
            # This code did not produce the active binding. Drop its checkpoint
            # so a still-visible code can be retried; if it was already read,
            # the user can resend it without leaking the code into state.
            visible_ids = {stable_message_id(row) for row in inbound_rows}
            attempts = [
                item for item in attempts
                if (str(item.get("messageId") or "") if isinstance(item, dict) else str(item)) not in visible_ids
            ]
            state["personal_group_cloud_claim_attempt_ids"] = attempts
            attempted_claims = {
                str(item.get("messageId") or "") if isinstance(item, dict) else str(item)
                for item in attempts
            }

    # A fresh code can bind an unbound group or switch this exact bound group
    # to another account/project/session. It never changes other groups.
    for row in inbound_rows:
        code = binding_code(visible_text(row))
        if not code:
            continue
        message_id = stable_message_id(row)
        if message_id in attempted_claims:
            # A retry of an uncertain one-time claim must first reconcile via
            # resolve; never resend or persist its plaintext code.
            continue
        attempts.append({
            "messageId": message_id,
            "sourceRow": {
                "_message_db": str(row.get("_message_db") or ""),
                "server_id": str(row.get("server_id") or ""),
                "local_id": int(row.get("local_id") or 0),
            },
        })
        state["personal_group_cloud_claim_attempt_ids"] = attempts[-MAX_PENDING:]
        attempted_claims.add(message_id)
        _persist_local_state(config, state)
        claim = client.claim(group_id, code, message_id)
        handled_binding_ids.add(message_id)
        attempts = [
            item for item in attempts
            if (str(item.get("messageId") or "") if isinstance(item, dict) else str(item)) != message_id
        ]
        state["personal_group_cloud_claim_attempt_ids"] = attempts
        attempted_claims.discard(message_id)
        if claim.get("ok"):
            switched = bool(claim.get("switched"))
            state["personal_group_cloud_binding_id"] = str(claim.get("bindingId") or "")
            state["personal_group_cloud_binding_checked_at"] = int(now)
            state["personal_group_cloud_claim_attempt_ids"] = []
            attempts = []
            attempted_claims.clear()
            state.pop("personal_group_cloud_unbound_notified", None)
            if switched:
                state["personal_group_cloud_pending"] = []
                state["personal_group_cloud_outbox"] = []
                pending = state["personal_group_cloud_pending"]
                outbox = state["personal_group_cloud_outbox"]
            text = "此群已切换到新选择的 OverTree 项目和会话。" if switched else "此群已连接到对应的 OverTree 项目和会话。"
            _enqueue_outgoing(state, text, row, "binding-switched" if switched else "binding")
            resolved_binding = claim
        else:
            _enqueue_outgoing(state, "绑定码无效或已过期，请在 OverTree 中重新生成后再发送。", row, "binding-invalid")

    if not resolved_binding:
        if not state.get("personal_group_cloud_unbound_notified") and any(
            personal_group_message_is_addressed(config, row, visible_text) for row in trigger_rows
        ):
            source = next(row for row in trigger_rows if personal_group_message_is_addressed(config, row, visible_text))
            _enqueue_outgoing(
                state,
                "这个群当前没有有效的 OverTree 连接码，或连接已因空闲超时断开。请在 OverTree 的微信连接页生成新码并发到本群。",
                source,
                "binding-required",
            )
            state["personal_group_cloud_unbound_notified"] = True
        sent_outgoing, send_error = _flush_outbox(config, state, send_text, now, send_file)
        return {
            "response_sent": sent_outgoing,
            "task_enqueued": None,
            "processed_rows": len(rows),
            "metrics": {"transport": "personal_group_cloud", "binding": "unbound", "outbox": len(state.get("personal_group_cloud_outbox") or []), **({"send_error": send_error} if send_error else {})},
            "state": state,
        }

    # Poll only after processing rebinding codes. A switch must never deliver
    # the previous session's pending results into the newly bound group.
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
            attempts_count = int(item.get("pollAttempts") or 0) + 1
            item["pollAttempts"] = attempts_count
            item["nextPollAt"] = now + min(30, 2 ** min(attempts_count, 5))
            remaining.append(item)
            continue
        plan = _validate_delivery_plan(receipt)
        source = item.get("sourceRow") if isinstance(item.get("sourceRow"), dict) else {}
        result_id = f"result-{item['messageId']}"
        item["result"] = "\n".join(plan["textParts"])
        item["resultReady"] = True
        item["resultSource"] = source
        item["deliveryPlanVersion"] = plan["version"]
        for index, text in enumerate(plan["textParts"]):
            text_id = result_id if index == 0 else f"{result_id}-text-{index}"
            _enqueue_outgoing(
                state,
                text,
                source,
                "result",
                message_id=text_id,
                result_id=result_id,
            )
        for index, artifact in enumerate(plan["files"]):
            _enqueue_outgoing(
                state,
                f"文件：{str(artifact.get('name') or f'artifact-{index + 1}')}",
                source,
                "file",
                message_id=f"{result_id}-artifact-{index}",
                artifact=artifact,
                result_id=result_id,
            )
        remaining.append(item)
        break
    state["personal_group_cloud_pending"] = remaining
    pending = state["personal_group_cloud_pending"]

    trigger_rows = [
        row for row in trigger_rows
        if stable_message_id(row) not in handled_binding_ids
        and personal_group_message_is_addressed(config, row, visible_text)
    ]

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
        _enqueue_outgoing(
            state,
            "已收到，OverTree 正在处理。完成后会把结果发到本群。",
            item.get("sourceRow") or {},
            "ack",
            message_id=f"ack-{item['messageId']}",
        )
        if str(receipt.get("status") or "running").lower() not in {"running", "queued", "processing", "starting"}:
            # The next loop performs the same idempotent poll/delivery path.
            pass

    state["personal_group_cloud_pending"] = pending
    sent_outgoing, send_error = _flush_outbox(config, state, send_text, now, send_file)
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
