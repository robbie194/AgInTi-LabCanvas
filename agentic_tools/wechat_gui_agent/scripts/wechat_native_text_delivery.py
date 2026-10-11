"""Read-only native message receipts for the serialized Linux WeChat sender."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unicodedata

from wechat_reply_mentions import mention_header


PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(Path(__file__).resolve().parents[1] / ".private"),
)).expanduser().resolve()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    # Rich @ recipients carry editor-only U+2005 padding in the native row.
    # Match the same pending send before the monitor can mistake it for input.
    if "\u2005" in value.partition("\n")[0]:
        names, _body, canonical = mention_header(value)
        if names:
            return canonical
    return value


def native_chat_binding(target: dict, private: Path = PRIVATE) -> dict:
    """Resolve one exact configured chat, never an adjacent or fuzzy match."""
    names = {str(target.get(key) or "") for key in ("name", "query", "expected_title")}
    names.discard("")
    bindings = {}
    for path in private.glob("*direct-chatops.local.json"):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        configured = config.get("send_target") or {}
        aliases = {str(config.get("chat_name") or "")}
        aliases.update(str(configured.get(key) or "") for key in ("name", "query", "expected_title"))
        if not names.intersection(aliases):
            continue
        table = str(config.get("message_table") or "")
        sender = str(config.get("self_wxid") or "")
        if re.fullmatch(r"Msg_[a-fA-F0-9]+", table) and sender:
            bindings[(table, sender)] = {"table": table, "sender": sender}
    if len(bindings) != 1:
        raise RuntimeError("WECHAT_COMPOSE_VERIFY_FAILED: native exact-chat receipt binding unavailable")
    return next(iter(bindings.values()))


def message_databases(private: Path) -> list[Path]:
    return sorted((private / "wechat_decrypt" / "decrypted" / "message").glob("message_*.db"))


def prepare_receipt(target: dict, private: Path = PRIVATE) -> dict:
    binding = native_chat_binding(target, private)
    cursors = {}
    for path in message_databases(private):
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
                cursors[path.name] = conn.execute(
                    f'SELECT COALESCE(MAX(local_id), 0) FROM "{binding["table"]}"'
                ).fetchone()[0]
        except sqlite3.Error:
            continue
    if not cursors:
        raise RuntimeError("WECHAT_COMPOSE_VERIFY_FAILED: native chat history unavailable before send")
    return {**binding, "after": cursors, "started_at": int(time.time())}


def find_native_receipt(receipt: dict, message: str, private: Path = PRIVATE) -> dict | None:
    # Reuse the intake decoder, including zstd and group sender-prefix handling.
    from wechat_direct_chatops import decode_content

    table = str(receipt.get("table") or "")
    if not re.fullmatch(r"Msg_[a-fA-F0-9]+", table):
        return None
    for path in message_databases(private):
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
                rows = conn.execute(
                    f'''SELECT m.local_id, m.server_id, m.message_content,
                               m.compress_content, m.WCDB_CT_message_content
                        FROM "{table}" AS m JOIN Name2Id AS n ON n.rowid = m.real_sender_id
                        WHERE m.local_id > ? AND m.create_time >= ? AND n.user_name = ?
                          AND m.local_type = 1 AND m.status IN (2, 3)
                          AND m.server_id IS NOT NULL AND CAST(m.server_id AS TEXT) NOT IN ('', '0')
                        ORDER BY m.local_id DESC LIMIT 128''',
                    (receipt["after"].get(path.name, 0), receipt["started_at"], receipt["sender"]),
                ).fetchall()
        except sqlite3.Error:
            continue
        for local_id, server_id, content, compressed, content_type in rows:
            text = decode_content(content, compressed, content_type)
            prefix = receipt["sender"] + ":\n"
            if text.startswith(prefix):
                text = text[len(prefix):]
            if normalize_text(text) == normalize_text(message):
                return {"verified": True, "method": "native_outbound_echo", "message_db": path.name,
                        "local_id": local_id, "server_id": str(server_id)}
    return None


def wait_native_receipt(receipt: dict, message: str, *, timeout: float = 10,
                        private: Path = PRIVATE) -> dict | None:
    deadline = time.monotonic() + max(0, timeout)
    while True:
        evidence = find_native_receipt(receipt, message, private)
        if evidence or time.monotonic() >= deadline:
            return evidence
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def pending_receipt_path(target: dict, message: str, private: Path = PRIVATE) -> Path:
    identity = json.dumps([target.get("name"), target.get("query"), normalize_text(message)], ensure_ascii=False)
    return private / "native_text_delivery" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


def pending_outbound_echo(config: dict, row: dict, private: Path = PRIVATE) -> bool:
    """Recognize our native row before the sender finishes waiting for its echo."""
    sender = str(config.get("self_wxid") or "")
    if not sender or row.get("sender") != sender or row.get("local_type") != 1:
        return False
    message = str(row.get("content") or "")
    if message.startswith(sender + ":\n"):
        message = message[len(sender) + 2:]
    target = dict(config.get("send_target") or {})
    target.setdefault("name", config.get("chat_name"))
    target.setdefault("query", config.get("chat_name"))
    path = pending_receipt_path(target, message, private)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        db = str(row.get("_message_db") or row.get("message_db") or "")
        after = receipt.get("after") or {}
        return bool(
            receipt.get("table") == config.get("message_table")
            and receipt.get("sender") == sender
            and db in after
            and int(row.get("local_id") or 0) > int(after[db])
            and 0 <= float(row.get("create_time") or 0) - float(receipt["started_at"]) <= 120
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def retain_pending_receipt(path: Path, receipt: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Persist before Enter. Even a process kill must not authorize a blind retry.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
