#!/usr/bin/env python3
"""Private structured memory database for WeChat direct monitors."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve()
DEFAULT_DB = PRIVATE / "wechat_memory.sqlite"


CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web_clip": (
        "http://",
        "https://",
        "www.",
        "<url>",
        "<appmsg",
        "link",
        "url",
        "web page",
        "website",
        "read later",
        "bookmark",
        "article",
        "paper",
        "pdf",
        "youtube",
        "youtu.be",
        "video",
        "channel",
        "shipinhao",
        "wechat channel",
        "链接",
        "網址",
        "网址",
        "网页",
        "网站",
        "视频",
        "视频号",
        "频道",
        "公众号",
        "小红书",
        "b站",
        "哔哩",
        "收藏",
        "稍后读",
        "看到",
        "转发",
        "文章",
        "论文",
    ),
    "media": (
        "image",
        "photo",
        "picture",
        "screenshot",
        "video",
        "voice",
        "audio",
        "sticker",
        "gif",
        "图片",
        "照片",
        "截图",
        "视频",
        "语音",
        "音频",
        "表情",
    ),
    "todo": (
        "todo",
        "to do",
        "task",
        "need to",
        "remember to",
        "follow up",
        "next step",
        "待办",
        "任务",
        "要做",
        "记得",
        "帮我",
        "处理",
        "安排",
    ),
    "grocery": (
        "grocery",
        "groceries",
        "shopping",
        "buy",
        "supermarket",
        "购物",
        "买菜",
        "采购",
        "超市",
        "牛奶",
        "鸡蛋",
        "水果",
        "面包",
    ),
    "calendar": (
        "calendar",
        "schedule",
        "meeting",
        "deadline",
        "remind",
        "tomorrow",
        "today",
        "tonight",
        "next week",
        "日程",
        "会议",
        "开会",
        "提醒",
        "明天",
        "后天",
        "今天",
        "今晚",
        "星期",
        "周",
        "点",
    ),
    "beat_board": (
        "beat board",
        "beat",
        "storyboard",
        "shot list",
        "scene",
        "故事板",
        "分镜",
        "剧情",
        "镜头",
        "节拍",
    ),
    "idea": (
        "idea",
        "brainstorm",
        "maybe",
        "concept",
        "想法",
        "点子",
        "创意",
        "建议",
        "灵感",
    ),
    "writing": (
        "writing",
        "draft",
        "article",
        "copywriting",
        "outline",
        "rewrite",
        "写作",
        "文章",
        "文案",
        "小说",
        "标题",
        "大纲",
        "草稿",
    ),
    "language": (
        "language",
        "english",
        "japanese",
        "grammar",
        "pronunciation",
        "translate",
        "外语",
        "英语",
        "日语",
        "中文",
        "翻译",
        "语法",
        "发音",
        "单词",
        "词汇",
        "拼音",
    ),
    "money": (
        "money",
        "income",
        "business",
        "monetize",
        "client",
        "quote",
        "挣钱",
        "赚钱",
        "变现",
        "收入",
        "商业",
        "客户",
        "报价",
    ),
    "memo": (
        "memo",
        "note",
        "record",
        "save this",
        "remember",
        "备忘",
        "笔记",
        "记录",
        "保存",
        "记一下",
    ),
    "request": (
        "could you",
        "can you",
        "please",
        "help me",
        "帮我",
        "请",
        "能不能",
        "可以",
        "麻烦",
    ),
}


REQUEST_MARKERS = (
    "could you",
    "can you",
    "please",
    "help me",
    "add",
    "save",
    "list",
    "summarize",
    "export",
    "organize",
    "帮我",
    "请",
    "能不能",
    "可以",
    "麻烦",
    "加到",
    "保存",
    "记录",
    "列出",
    "总结",
    "整理",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "summary"], nargs="?", default="summary")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--chat", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    init_db(args.db)
    payload = database_summary(args.db, chat_name=args.chat or None)
    if args.action == "init":
        payload = {"ok": True, "db": str(args.db), **payload}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_summary(payload))
    return 0


def organize_messages(
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    text_fn: Callable[[dict[str, Any]], str] | None = None,
    kind_fn: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    organizer = config.get("organizer") if isinstance(config.get("organizer"), dict) else {}
    if not bool(organizer.get("enabled", False)):
        return {"ok": True, "status": "disabled", "messages": 0, "items": 0}
    db_path = Path(str(organizer.get("db_path") or DEFAULT_DB))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    init_db(db_path)

    chat_name = str(config.get("chat_name") or "wechat-chat")
    self_wxid = str(config.get("self_wxid") or "")
    default_tags = [str(item) for item in organizer.get("default_tags", []) if str(item).strip()]
    capture_unclassified = bool(organizer.get("capture_unclassified", True))
    organize_self_messages = bool(organizer.get("organize_self_messages", False))
    organize_system_messages = bool(organizer.get("organize_system_messages", False))
    text_fn = text_fn or (lambda row: str(row.get("content") or ""))
    kind_fn = kind_fn or (lambda row: str(row.get("local_type") or "message"))

    inserted_sources = 0
    inserted_items = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in rows:
            text = text_fn(row)
            kind = kind_fn(row)
            direction = "outbound" if self_wxid and row.get("sender") == self_wxid else "inbound"
            source_id, was_inserted = upsert_source_message(conn, chat_name, row, text, kind, direction)
            inserted_sources += int(was_inserted)
            if direction == "outbound" and not organize_self_messages:
                continue
            if kind == "system" and not organize_system_messages:
                continue
            items = classify_memory_items(
                chat_name,
                row,
                text,
                kind,
                default_tags=default_tags,
                capture_unclassified=capture_unclassified,
            )
            for item in items:
                if insert_memory_item(conn, source_id, chat_name, item):
                    inserted_items += 1
    return {
        "ok": True,
        "status": "ok",
        "db": str(db_path),
        "messages": len(rows),
        "inserted_sources": inserted_sources,
        "items": inserted_items,
    }


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS source_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_name TEXT NOT NULL,
                server_id TEXT,
                local_id INTEGER,
                sender TEXT,
                sender_display TEXT,
                create_time INTEGER,
                observed_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                direction TEXT NOT NULL,
                body TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(chat_name, server_id, local_id)
            );
            CREATE INDEX IF NOT EXISTS idx_source_messages_chat_local
                ON source_messages(chat_name, local_id);
            CREATE INDEX IF NOT EXISTS idx_source_messages_body_hash
                ON source_messages(body_hash);

            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_message_id INTEGER NOT NULL,
                chat_name TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                due_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(source_message_id) REFERENCES source_messages(id),
                UNIQUE(source_message_id, category, title)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_items_chat_category
                ON memory_items(chat_name, category, status);
            CREATE INDEX IF NOT EXISTS idx_memory_items_due
                ON memory_items(due_at);

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS item_tags (
                item_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY(item_id, tag_id),
                FOREIGN KEY(item_id) REFERENCES memory_items(id),
                FOREIGN KEY(tag_id) REFERENCES tags(id)
            );
            """
        )


def upsert_source_message(
    conn: sqlite3.Connection,
    chat_name: str,
    row: dict[str, Any],
    text: str,
    kind: str,
    direction: str,
) -> tuple[int, bool]:
    server_id = str(row.get("server_id") or "")
    local_id = int(row.get("local_id") or 0)
    metadata = {
        "local_type": row.get("local_type"),
        "status": row.get("status"),
        "real_sender_id": row.get("real_sender_id"),
    }
    body_hash = hashlib.sha256(f"{chat_name}\0{server_id}\0{local_id}\0{text}".encode("utf-8")).hexdigest()
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO source_messages (
            chat_name, server_id, local_id, sender, sender_display, create_time,
            observed_at, kind, direction, body, body_hash, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_name,
            server_id,
            local_id,
            str(row.get("sender") or ""),
            str(row.get("sender_display") or row.get("sender") or ""),
            int(row.get("create_time") or 0),
            datetime.now().isoformat(timespec="seconds"),
            kind,
            direction,
            text,
            body_hash,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    record = conn.execute(
        "SELECT id FROM source_messages WHERE chat_name = ? AND server_id = ? AND local_id = ?",
        (chat_name, server_id, local_id),
    ).fetchone()
    if record is None:
        raise RuntimeError("failed to read inserted source message")
    return int(record["id"]), conn.total_changes > before


def classify_memory_items(
    chat_name: str,
    row: dict[str, Any],
    text: str,
    kind: str,
    *,
    default_tags: list[str],
    capture_unclassified: bool,
) -> list[dict[str, Any]]:
    body = normalize_body(text)
    if not body:
        if kind not in {"text", "type-1"}:
            body = f"[{kind}]"
        else:
            return []
    categories = infer_categories(body, kind)
    if not categories and capture_unclassified:
        categories = ["inbox"]

    due_at = extract_due_hint(body)
    title = make_title(body, kind)
    tags = sorted(set(default_tags + infer_tags(chat_name, body, categories)))
    now = message_datetime(row)
    items = []
    for category in categories:
        status = "open" if category in {"todo", "calendar", "grocery", "request", "inbox"} else "active"
        priority = infer_priority(body)
        item_tags = sorted(set(tags + [category]))
        items.append(
            {
                "category": category,
                "title": title,
                "body": body,
                "status": status,
                "priority": priority,
                "due_at": due_at,
                "created_at": now,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "confidence": confidence_for(category, body),
                "tags": item_tags,
                "metadata": {
                    "kind": kind,
                    "sender": str(row.get("sender_display") or row.get("sender") or ""),
                },
            }
        )
    return items


def infer_categories(text: str, kind: str) -> list[str]:
    lowered = text.lower()
    categories: list[str] = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            categories.append(category)
    if kind == "file/link" and "web_clip" not in categories:
        categories.append("web_clip")
    if kind in {"image", "video", "voice", "sticker"} and "media" not in categories:
        categories.append("media")
    if kind not in {"text", "type-1"} and "attachment" not in categories:
        categories.append("attachment")
    if not categories and is_question_or_request(text):
        categories.append("request")
    return unique_preserve_order(categories)


def is_question_or_request(text: str) -> bool:
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in REQUEST_MARKERS):
        return True
    return "?" in text or "？" in text


def infer_tags(chat_name: str, text: str, categories: list[str]) -> list[str]:
    lowered = f"{chat_name} {text}".lower()
    tags = list(categories)
    if (
        "http://" in lowered
        or "https://" in lowered
        or "<url>" in lowered
        or "链接" in lowered
        or "网页" in lowered
        or "youtube" in lowered
        or "youtu.be" in lowered
        or "视频号" in lowered
        or "shipinhao" in lowered
    ):
        tags.extend(["web-clip", "read-later"])
    if any(marker in lowered for marker in ("image", "photo", "picture", "screenshot", "图片", "照片", "截图")):
        tags.extend(["media", "image"])
    if any(marker in lowered for marker in ("voice", "audio", "语音", "音频")):
        tags.extend(["media", "audio"])
    if any(marker in lowered for marker in ("youtube", "youtu.be", "video", "视频号", "shipinhao", "bilibili", "b站", "哔哩")):
        tags.append("media")
        tags.append("video")
    if "写作" in lowered or "writing" in lowered:
        tags.append("writing")
    if "外语" in lowered or "language" in lowered or "english" in lowered or "japanese" in lowered:
        tags.append("foreign-language")
    if "挣钱" in lowered or "赚钱" in lowered or "money" in lowered or "business" in lowered:
        tags.append("money")
    if "labcanvas" in lowered or "aginti" in lowered:
        tags.append("labcanvas")
    stripped = text.lstrip()
    if (
        any(ch in text for ch in "？?")
        and "�" not in text[:128]
        and not (stripped.startswith("<?xml") or stripped.startswith("<msg"))
    ):
        tags.append("question")
    return tags


def extract_due_hint(text: str) -> str | None:
    patterns = [
        r"\b(?:today|tomorrow|tonight|next week)\b(?:\s+at\s+\d{1,2}(?::\d{2})?)?",
        r"\b\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?\b",
        r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
        r"(今天|今晚|明天|后天|下周|星期[一二三四五六日天]|周[一二三四五六日天])[^，。,.!！?？]{0,16}",
        r"\d{1,2}[点:：]\d{0,2}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return None


def infer_priority(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("urgent", "asap", "immediately", "紧急", "马上", "现在")):
        return "high"
    if any(marker in lowered for marker in ("later", "someday", "以后", "有空")):
        return "low"
    return "normal"


def confidence_for(category: str, text: str) -> float:
    lowered = text.lower()
    keywords = CATEGORY_KEYWORDS.get(category, ())
    hits = sum(1 for keyword in keywords if keyword.lower() in lowered)
    if category == "inbox":
        return 0.35
    return min(0.95, 0.55 + hits * 0.12)


def insert_memory_item(conn: sqlite3.Connection, source_id: int, chat_name: str, item: dict[str, Any]) -> bool:
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_items (
            source_message_id, chat_name, category, title, body, status, priority,
            due_at, created_at, updated_at, confidence, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            chat_name,
            item["category"],
            item["title"],
            item["body"],
            item["status"],
            item["priority"],
            item["due_at"],
            item["created_at"],
            item["updated_at"],
            item["confidence"],
            json.dumps(item.get("metadata", {}), ensure_ascii=False, sort_keys=True),
        ),
    )
    inserted = conn.total_changes > before
    record = conn.execute(
        "SELECT id FROM memory_items WHERE source_message_id = ? AND category = ? AND title = ?",
        (source_id, item["category"], item["title"]),
    ).fetchone()
    if record is not None:
        for tag in item.get("tags", []):
            attach_tag(conn, int(record["id"]), str(tag))
    return inserted


def attach_tag(conn: sqlite3.Connection, item_id: int, tag: str) -> None:
    tag = normalize_tag(tag)
    if not tag:
        return
    conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
    record = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()
    if record is not None:
        conn.execute("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES (?, ?)", (item_id, int(record["id"])))


def database_summary(path: Path, *, chat_name: str | None = None) -> dict[str, Any]:
    init_db(path)
    where = "WHERE chat_name = ?" if chat_name else ""
    params: tuple[Any, ...] = (chat_name,) if chat_name else ()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        message_count = conn.execute(f"SELECT count(*) FROM source_messages {where}", params).fetchone()[0]
        item_count = conn.execute(f"SELECT count(*) FROM memory_items {where}", params).fetchone()[0]
        by_category = {
            row["category"]: row["count"]
            for row in conn.execute(
                f"SELECT category, count(*) AS count FROM memory_items {where} GROUP BY category ORDER BY count DESC",
                params,
            )
        }
        if chat_name:
            tag_rows = conn.execute(
                """
                SELECT tags.name, count(*) AS count
                FROM tags
                JOIN item_tags ON item_tags.tag_id = tags.id
                JOIN memory_items ON memory_items.id = item_tags.item_id
                WHERE memory_items.chat_name = ?
                GROUP BY tags.name
                ORDER BY count DESC, tags.name
                LIMIT 30
                """,
                (chat_name,),
            )
        else:
            tag_rows = conn.execute(
                """
                SELECT tags.name, count(*) AS count
                FROM tags
                JOIN item_tags ON item_tags.tag_id = tags.id
                JOIN memory_items ON memory_items.id = item_tags.item_id
                GROUP BY tags.name
                ORDER BY count DESC, tags.name
                LIMIT 30
                """
            )
        by_tag = {row["name"]: row["count"] for row in tag_rows}
    return {
        "ok": True,
        "db_exists": path.exists(),
        "chat_name": chat_name or "",
        "message_count": int(message_count),
        "item_count": int(item_count),
        "by_category": by_category,
        "by_tag": by_tag,
    }


def format_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"WeChat memory: {payload.get('message_count', 0)} messages, {payload.get('item_count', 0)} organized items",
    ]
    if payload.get("chat_name"):
        lines.append(f"Chat: {payload['chat_name']}")
    categories = payload.get("by_category") or {}
    if categories:
        lines.append("Categories: " + ", ".join(f"{key}={value}" for key, value in categories.items()))
    tags = payload.get("by_tag") or {}
    if tags:
        lines.append("Tags: " + ", ".join(f"{key}={value}" for key, value in tags.items()))
    return "\n".join(lines)


def normalize_body(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:5000]


def make_title(text: str, kind: str, *, max_len: int = 96) -> str:
    title = normalize_body(text)
    if not title:
        title = f"[{kind}]"
    title = title.strip(" -:：")
    return title if len(title) <= max_len else title[: max_len - 3].rstrip() + "..."


def normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", "-", str(tag or "").strip().lower())[:80]


def message_datetime(row: dict[str, Any]) -> str:
    raw = int(row.get("create_time") or 0)
    if raw > 0:
        try:
            return datetime.fromtimestamp(raw).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now().isoformat(timespec="seconds")


def unique_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        if item in seen:
            continue
        output.append(item)
        seen.add(item)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
