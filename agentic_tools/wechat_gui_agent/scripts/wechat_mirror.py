#!/usr/bin/env python3
"""Local SQLite mirror for visible WeChat GUI automation evidence."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve() / "wechat_mirror.sqlite"


def init_db(db_path: Path = DEFAULT_DB) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                query TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                direction TEXT,
                message TEXT,
                status TEXT NOT NULL,
                screenshot_path TEXT,
                ocr_text TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                event_id INTEGER,
                direction TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                screenshot_path TEXT,
                observed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id),
                FOREIGN KEY(event_id) REFERENCES events(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_files (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                event_id INTEGER,
                source_path TEXT NOT NULL,
                mirror_path TEXT NOT NULL,
                suffix TEXT,
                size_bytes INTEGER,
                source_mtime REAL,
                status TEXT NOT NULL,
                matched_by TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id),
                FOREIGN KEY(event_id) REFERENCES events(id)
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_event_direction
            ON messages(event_id, direction)
            WHERE event_id IS NOT NULL
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_media_files_chat_mirror ON media_files(chat_id, mirror_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_media_files_chat_updated ON media_files(chat_id, updated_at)")


def record_event(
    *,
    chat_name: str,
    action: str,
    status: str,
    db_path: Path = DEFAULT_DB,
    query: str | None = None,
    direction: str | None = None,
    message: str | None = None,
    screenshot_path: str | None = None,
    ocr_text: str | None = None,
    metadata: dict | None = None,
) -> int:
    init_db(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chats(name, query, created_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                query=excluded.query,
                last_seen_at=excluded.last_seen_at
            """,
            (chat_name, query, now, now),
        )
        chat_id = conn.execute("SELECT id FROM chats WHERE name = ?", (chat_name,)).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO events(
                chat_id, action, direction, message, status, screenshot_path,
                ocr_text, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                action,
                direction,
                message,
                status,
                screenshot_path,
                ocr_text,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
            ),
        )
        event_id = int(cur.lastrowid)
        if message:
            insert_message(
                conn,
                chat_id=chat_id,
                event_id=event_id,
                direction=direction or "outbound",
                body=message,
                status=status,
                screenshot_path=screenshot_path,
                observed_at=now,
            )
        if ocr_text:
            insert_message(
                conn,
                chat_id=chat_id,
                event_id=event_id,
                direction="screen_ocr",
                body=ocr_text,
                status=status,
                screenshot_path=screenshot_path,
                observed_at=now,
            )
        return event_id


def record_media_files(
    *,
    chat_name: str,
    event_id: int | None,
    files: list[dict],
    db_path: Path = DEFAULT_DB,
) -> int:
    if not files:
        return 0
    init_db(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    inserted_or_updated = 0
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chats(name, query, created_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at
            """,
            (chat_name, None, now, now),
        )
        chat_id = conn.execute("SELECT id FROM chats WHERE name = ?", (chat_name,)).fetchone()[0]
        for item in files:
            source = str(item.get("source") or "")
            target = str(item.get("target") or "")
            if not source or not target:
                continue
            conn.execute(
                """
                INSERT INTO media_files(
                    chat_id, event_id, source_path, mirror_path, suffix,
                    size_bytes, source_mtime, status, matched_by,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, mirror_path) DO UPDATE SET
                    event_id=excluded.event_id,
                    source_path=excluded.source_path,
                    suffix=excluded.suffix,
                    size_bytes=excluded.size_bytes,
                    source_mtime=excluded.source_mtime,
                    status=excluded.status,
                    matched_by=excluded.matched_by,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    chat_id,
                    event_id,
                    source,
                    target,
                    str(item.get("suffix") or Path(target).suffix.lower()),
                    int(item.get("bytes") or 0),
                    float(item.get("mtime") or 0.0),
                    str(item.get("status") or "unknown"),
                    str(item.get("matched_by") or ""),
                    json.dumps(item, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            inserted_or_updated += 1
    return inserted_or_updated


def insert_message(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    event_id: int | None,
    direction: str,
    body: str,
    status: str,
    screenshot_path: str | None,
    observed_at: str,
) -> None:
    if not body.strip():
        return
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR IGNORE INTO messages(
            chat_id, event_id, direction, body, status, screenshot_path,
            observed_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, event_id, direction, body, status, screenshot_path, observed_at, now),
    )


def capture_read(display: str, chat_name: str, output_dir: Path, db_path: Path, lang: str) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    screenshot = output_dir / f"{stamp}-{safe_name(chat_name)}-read.png"
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["XAUTHORITY"] = env.get("XAUTHORITY", "")
    subprocess.run(["import", "-window", "root", str(screenshot)], env=env, check=False)
    ocr_text = run_ocr(screenshot, lang)
    return record_event(
        chat_name=chat_name,
        action="read",
        direction="inbound",
        status="captured",
        db_path=db_path,
        screenshot_path=str(screenshot),
        ocr_text=ocr_text,
    )


def run_ocr(image_path: Path, lang: str) -> str:
    proc = subprocess.run(
        ["tesseract", str(image_path), "stdout", "-l", lang],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def list_events(db_path: Path, limit: int) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT events.id, events.created_at, chats.name, events.action,
                   events.status, events.message, events.screenshot_path
            FROM events
            JOIN chats ON chats.id = events.chat_id
            ORDER BY events.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    for row in rows:
        print("\t".join("" if item is None else str(item) for item in row))


def list_messages(db_path: Path, limit: int) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT messages.id, messages.observed_at, chats.name, messages.direction,
                   messages.status, substr(replace(messages.body, char(10), ' '), 1, 120),
                   messages.screenshot_path
            FROM messages
            JOIN chats ON chats.id = messages.chat_id
            ORDER BY messages.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    for row in rows:
        print("\t".join("" if item is None else str(item) for item in row))


def backfill_messages(db_path: Path) -> int:
    init_db(db_path)
    inserted = 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, direction, message, status, screenshot_path,
                   ocr_text, created_at
            FROM events
            WHERE message IS NOT NULL OR ocr_text IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
        for event_id, chat_id, direction, message, status, screenshot_path, ocr_text, created_at in rows:
            before = conn.total_changes
            if message:
                insert_message(
                    conn,
                    chat_id=chat_id,
                    event_id=event_id,
                    direction=direction or "outbound",
                    body=message,
                    status=status,
                    screenshot_path=screenshot_path,
                    observed_at=created_at,
                )
            if ocr_text:
                insert_message(
                    conn,
                    chat_id=chat_id,
                    event_id=event_id,
                    direction="screen_ocr",
                    body=ocr_text,
                    status=status,
                    screenshot_path=screenshot_path,
                    observed_at=created_at,
                )
            inserted += conn.total_changes - before
    return inserted


def export_json(db_path: Path, output_path: Path) -> None:
    init_db(db_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        chats = [dict(row) for row in conn.execute("SELECT * FROM chats ORDER BY id")]
        events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
        messages = [dict(row) for row in conn.execute("SELECT * FROM messages ORDER BY id")]
    output_path.write_text(
        json.dumps({"chats": chats, "events": events, "messages": messages}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def record_event_from_args(args: argparse.Namespace) -> int:
    metadata = {}
    if args.metadata_json:
        metadata = json.loads(args.metadata_json)
    return record_event(
        chat_name=args.chat,
        query=args.query,
        action=args.action,
        direction=args.direction,
        message=args.message,
        status=args.status,
        db_path=args.db,
        screenshot_path=args.screenshot,
        ocr_text=args.ocr_text,
        metadata=metadata,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    read = sub.add_parser("capture-read")
    read.add_argument("--display", default=":97")
    read.add_argument("--chat", required=True)
    read.add_argument("--output-dir", type=Path, default=ROOT / "output" / "wechat_gui_agent" / datetime.now().strftime("%F"))
    read.add_argument("--lang", default="chi_sim+chi_tra+eng")
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    message_listing = sub.add_parser("list-messages")
    message_listing.add_argument("--limit", type=int, default=20)
    sub.add_parser("backfill-messages")
    export = sub.add_parser("export-json")
    export.add_argument("--output", type=Path, required=True)
    record = sub.add_parser("record-event")
    record.add_argument("--chat", required=True)
    record.add_argument("--query")
    record.add_argument("--action", required=True)
    record.add_argument("--status", required=True)
    record.add_argument("--direction")
    record.add_argument("--message")
    record.add_argument("--screenshot")
    record.add_argument("--ocr-text")
    record.add_argument("--metadata-json")
    args = parser.parse_args()
    if args.command == "init":
        init_db(args.db)
        print(args.db)
    elif args.command == "capture-read":
        event_id = capture_read(args.display, args.chat, args.output_dir, args.db, args.lang)
        print(event_id)
    elif args.command == "list":
        list_events(args.db, args.limit)
    elif args.command == "list-messages":
        list_messages(args.db, args.limit)
    elif args.command == "backfill-messages":
        print(backfill_messages(args.db))
    elif args.command == "export-json":
        export_json(args.db, args.output)
        print(args.output)
    elif args.command == "record-event":
        print(record_event_from_args(args))
    return 0


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value).strip("-")
    return cleaned[:48] or "chat"


if __name__ == "__main__":
    raise SystemExit(main())
