#!/usr/bin/env python3
"""Send small explicit messages through native Linux WeChat GUI automation.

This script is intentionally conservative: it only sends when --send is passed,
uses the visible GUI, and stores screenshots for review.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
import unicodedata
import uuid

from file_lock import fcntl_compat as fcntl
from wechat_message_policy import file_transport_identity, is_no_reply_control
from wechat_mirror import DEFAULT_DB, record_event
from wechat_window_control import request_close
from wechat_native_text_delivery import (
    pending_receipt_path, prepare_receipt, retain_pending_receipt, wait_native_receipt,
)

try:
    from opencc import OpenCC
except ImportError:  # Optional local OCR-normalization aid.
    OpenCC = None


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve()
TITLE_T2S = OpenCC("t2s") if OpenCC is not None else None
TITLE_SCRIPT_FOLD = str.maketrans(
    {
        "備": "备",
        "懶": "懒",
        "鏈": "链",
        "錢": "钱",
        "設": "设",
        "寫": "写",
        "學": "学",
        "掙": "挣",
        "語": "语",
        "陳": "陈",
    }
)


@dataclass(frozen=True)
class Window:
    wid: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class WindowIdentity:
    wid: str
    title: str
    window_class: str
    transient_for: str = ""


@dataclass(frozen=True)
class TargetSpec:
    name: str
    query: str
    expected_title: str
    expected_title_aliases: tuple[str, ...] = ()
    allow_title_guard_fallback: bool = False
    allow_live_title_guard_fallback: bool = False
    result_click: tuple[int, int] | None = None
    fallback_clicks: tuple[tuple[int, int], ...] = ()
    open_click: tuple[int, int] | None = None
    allow_search: bool = False


class WeChatLockedError(RuntimeError):
    """Raised when the official WeChat client requires phone unlock."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", default=":97", help="X display running WeChat. Default: :97.")
    parser.add_argument("--target", action="append", default=[], help="Chat/group/contact name. Repeatable.")
    parser.add_argument("--targets-file", type=Path, help="JSON file with a target list, {targets,message}, or a target registry mapping.")
    parser.add_argument("--message", default="test", help="Message text. Default: test.")
    parser.add_argument("--send", action="store_true", help="Actually press Enter in the message composer.")
    parser.add_argument(
        "--compose-dry-run",
        action="store_true",
        help="In dry-run mode, paste the message into the composer for screenshot review.",
    )
    parser.add_argument("--pause", type=float, default=1.2, help="Pause between GUI actions.")
    parser.add_argument("--skip-title-guard", action="store_true", help="Do not OCR-check the opened chat title before composing.")
    parser.add_argument("--prefer-current", action="store_true", help="If the visible chat title already matches, send there without searching first.")
    parser.add_argument("--allow-search", action="store_true", help="Allow WeChat search fallback. Disabled by default.")
    parser.add_argument("--no-search", action="store_true", help="Compatibility flag; search is already disabled unless --allow-search is passed.")
    parser.add_argument("--output-dir", type=Path, default=Path(os.environ.get(
        "WECHAT_AGENT_LOG_DIR", str(ROOT / "output" / "wechat_gui_agent")
    )) / datetime.now().strftime("%F"))
    parser.add_argument("--mirror-db", type=Path, default=DEFAULT_DB, help="SQLite mirror database path.")
    parser.add_argument("--download-file-title", default="", help="Open and download the exact visible WeChat file card title without sending a message.")
    parser.add_argument("--download-root", type=Path, default=Path.home() / "Documents" / "xwechat_files")
    parser.add_argument("--download-wait-seconds", type=float, default=120.0)
    parser.add_argument("--download-file-size", type=int, default=0, help="Optional exact declared attachment size.")
    parser.add_argument("--download-file-md5", default="", help="Optional exact attachment MD5 from the source card.")
    parser.add_argument(
        "--file",
        type=Path,
        help="Send this exact local file after the target title guard succeeds. Requires --send.",
    )
    args = parser.parse_args()
    if args.file and not args.send:
        raise SystemExit("--file requires --send so file delivery is always explicit.")
    outgoing_file = args.file.expanduser().resolve() if args.file else None
    if outgoing_file and not outgoing_file.is_file():
        raise SystemExit(f"File does not exist: {outgoing_file}")
    lock_wait_seconds = max(
        0.0,
        float(os.environ.get("WECHAT_GUI_SEND_LOCK_WAIT_SECONDS", "60")),
    )
    minimum_timeout = int(args.download_wait_seconds + 30) if args.download_file_title else 0
    minimum_timeout = max(minimum_timeout, int(lock_wait_seconds + 45))
    install_process_timeout(minimum_seconds=minimum_timeout)

    targets, message = load_targets(args.target, args.targets_file, args.message)
    if not targets:
        raise SystemExit("No targets supplied. Use --target or --targets-file.")
    if is_no_reply_control(message):
        print(json.dumps({"status": "suppressed-control", "sent": False}, ensure_ascii=False))
        return 0
    args.message = message

    required = ["xdotool", "xclip", "import", "xprop"]
    if not args.skip_title_guard:
        required.extend(["convert", "tesseract"])
    require_tools(*required)
    env = os.environ.copy()
    env["DISPLAY"] = args.display
    env["XAUTHORITY"] = env.get("XAUTHORITY", "")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    window = wait_for_main_wechat_window(
        env,
        timeout=float(os.environ.get("WECHAT_MAIN_WINDOW_WAIT_SECONDS", "15")),
    )
    if not window:
        raise SystemExit(f"No visible WeChat window found on DISPLAY={args.display}. Log in first.")
    close_secondary_wechat_windows(env, window)
    if window.width < 500 or window.height < 500:
        screenshot(env, args.output_dir / "login_or_small_window.png")
        raise SystemExit(
            "WECHAT_ENTRY_REQUIRED: WeChat is visible but not in the main chat UI; "
            "click Enter Weixin or approve login on phone first."
        )

    PRIVATE.mkdir(parents=True, exist_ok=True)
    lock_path = PRIVATE / "wechat_gui_send.lock"
    results = []
    with lock_path.open("w", encoding="utf-8") as lock:
        acquire_gui_send_lock(lock, timeout_seconds=lock_wait_seconds)
        for index, target in enumerate(targets, start=1):
            result = send_one(
                env,
                window,
                target,
                args.message,
                args.send,
                args.compose_dry_run,
                args.pause,
                args.skip_title_guard,
                args.prefer_current,
                args.allow_search and not args.no_search,
                args.output_dir,
                args.mirror_db,
                index,
                download_file_title=args.download_file_title,
                download_root=args.download_root,
                download_wait_seconds=args.download_wait_seconds,
                download_file_size=args.download_file_size,
                download_file_md5=args.download_file_md5,
                outgoing_file=outgoing_file,
            )
            results.append(result)
        fcntl.flock(lock, fcntl.LOCK_UN)

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "display": args.display,
        "send": args.send,
        "compose_dry_run": args.compose_dry_run,
        "message": args.message,
        "download_file_title": args.download_file_title,
        "outgoing_file": str(outgoing_file) if outgoing_file else "",
        "results": results,
    }
    manifest_path = args.output_dir / "send_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def acquire_gui_send_lock(lock: Any, *, timeout_seconds: float) -> None:
    """Wait briefly for the one GUI lane instead of racing sibling workers."""
    timeout = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    "WECHAT_SEND_BUSY: serialized GUI sender remained busy; defer this send."
                ) from exc
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def install_process_timeout(*, minimum_seconds: int = 0) -> None:
    timeout = max(minimum_seconds, int(os.environ.get("WECHAT_GUI_SEND_MAX_SECONDS", "45")))
    if timeout <= 0:
        return

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"WECHAT_SEND_TIMEOUT: GUI sender exceeded {timeout} seconds")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(timeout)


def load_targets(cli_targets: list[str], targets_file: Path | None, default_message: str) -> tuple[list[TargetSpec], str]:
    raw_cli_targets: list[Any] = list(cli_targets)
    raw_targets: list[Any] = []
    message = default_message
    registry: dict[str, Any] = {}
    if targets_file:
        raw = json.loads(targets_file.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            raw_targets.extend(raw)
        elif isinstance(raw, dict):
            if "message" in raw:
                message = str(raw["message"])
            if "targets" in raw:
                file_targets = raw.get("targets", [])
                if not isinstance(file_targets, list):
                    raise SystemExit(f"{targets_file} field 'targets' must be a list")
                raw_targets.extend(file_targets)
            elif any(key in raw for key in ("name", "target", "query")):
                raw_targets.append(raw)
            else:
                registry = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
        else:
            raise SystemExit(f"{targets_file} must be a JSON list or object")
    for item in raw_cli_targets:
        key = str(item)
        raw_targets.append(registry.get(key, item))
    targets = [target_from_raw(item) for item in raw_targets]
    return targets, message


def target_from_raw(raw: Any) -> TargetSpec:
    if isinstance(raw, str):
        return TargetSpec(name=raw, query=raw, expected_title=raw)
    if not isinstance(raw, dict):
        raise SystemExit(f"Target must be a string or object, got {type(raw).__name__}")
    name = str(raw.get("name") or raw.get("target") or raw.get("query") or "").strip()
    query = str(raw.get("query") or name).strip()
    expected_title = str(raw.get("expected_title") or raw.get("title") or name).strip()
    expected_title_aliases = strings_from_raw(raw.get("expected_title_aliases") or raw.get("title_aliases"))
    if not name or not query:
        raise SystemExit("Target object requires name/target and query")
    return TargetSpec(
        name=name,
        query=query,
        expected_title=expected_title or name,
        expected_title_aliases=expected_title_aliases,
        allow_title_guard_fallback=bool(raw.get("allow_title_guard_fallback") or raw.get("relaxed_title_guard")),
        allow_live_title_guard_fallback=bool(raw.get("allow_live_title_guard_fallback")),
        result_click=point_from_raw(raw.get("result_click")),
        fallback_clicks=points_from_raw(raw.get("fallback_clicks")),
        open_click=point_from_raw(raw.get("open_click")),
        allow_search=bool(raw.get("allow_search", False)) and not bool(raw.get("no_search")),
    )


def strings_from_raw(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, list | tuple):
        raise SystemExit("title aliases must be a string or list of strings")
    return tuple(str(item).strip() for item in raw if str(item).strip())


def point_from_raw(raw: Any) -> tuple[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        raise SystemExit("Click point must be [x_offset, y_offset]")
    return int(raw[0]), int(raw[1])


def points_from_raw(raw: Any) -> tuple[tuple[int, int], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple):
        raise SystemExit("fallback_clicks must be a list of [x_offset, y_offset] points")
    points = []
    for item in raw:
        point = point_from_raw(item)
        if point is not None:
            points.append(point)
    return tuple(points)


def send_one(
    env: dict[str, str],
    window: Window,
    target: TargetSpec,
    message: str,
    do_send: bool,
    compose_dry_run: bool,
    pause: float,
    skip_title_guard: bool,
    prefer_current: bool,
    allow_search: bool,
    out_dir: Path,
    mirror_db: Path,
    index: int,
    *,
    download_file_title: str = "",
    download_root: Path | None = None,
    download_wait_seconds: float = 120.0,
    download_file_size: int = 0,
    download_file_md5: str = "",
    outgoing_file: Path | None = None,
) -> dict[str, Any]:
    pending_path = None
    if do_send and outgoing_file is None and not download_file_title:
        pending_path = pending_receipt_path(target.__dict__, message)
        if pending_path.exists():
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            evidence = wait_native_receipt(pending, message, timeout=0)
            if not evidence:
                raise RuntimeError("WECHAT_GUI_SEND_UNCERTAIN: previous submission lacks a native receipt; not resending")
            record_event(chat_name=target.name, query=target.query, action="send", direction="outbound",
                         message=message, status="sent", db_path=mirror_db,
                         metadata={"delivery_verification": evidence, "recovered": True})
            pending_path.unlink()
            return {"target": target.name, "status": "sent", "delivery_verification": evidence, "recovered": True}
    reset_wechat_send_surface(env, window, target, pause)
    attempt_id = datetime.now().strftime("%H%M%S-%f")
    shot_prefix = f"{index:02d}-{safe_name(target.name)}-{attempt_id}"
    before_path = out_dir / f"{shot_prefix}-before.png"
    screenshot(env, before_path)
    if not skip_title_guard:
        locked = detect_wechat_locked(env, window, before_path, out_dir / f"{shot_prefix}-locked.png")
        if locked["locked"]:
            record_event(
                chat_name=target.name,
                query=target.query,
                action="open",
                status="wechat-locked",
                db_path=mirror_db,
                screenshot_path=str(before_path),
                metadata={"target": target.__dict__, "lock": locked},
            )
            raise WeChatLockedError(
                "WECHAT_LOCKED: Weixin for Linux is locked and requires normal phone-side unlock before GUI sends."
            )

    relaxed_visible_fallback_allowed = target.allow_title_guard_fallback and (
        not do_send or target.allow_live_title_guard_fallback
    )
    guard = open_target(
        env,
        window,
        target,
        pause,
        out_dir,
        shot_prefix,
        skip_title_guard,
        prefer_current,
        allow_search,
        relaxed_visible_fallback_allowed=relaxed_visible_fallback_allowed,
        fail_closed_after_visible_match=do_send or outgoing_file is not None,
    )
    opened_path = out_dir / f"{shot_prefix}-opened.png"
    if not guard["ok"]:
        fallback_allowed = target.allow_title_guard_fallback and (not do_send or target.allow_live_title_guard_fallback)
        if fallback_allowed:
            guard = {**guard, "ok": True, "relaxed_title_guard": True}
        else:
            record_event(
                chat_name=target.name,
                query=target.query,
                action="open",
                status="title-guard-failed",
                db_path=mirror_db,
                screenshot_path=str(opened_path),
                metadata={"target": target.__dict__, "guard": guard},
            )
            live_note = " Live sends do not allow relaxed title fallback." if do_send and target.allow_title_guard_fallback else ""
            raise RuntimeError(f"Opened chat title guard failed for {target.name}: OCR={guard.get('ocr_text', '')!r}.{live_note}")
    if guard.get("relaxed_title_guard"):
        record_event(
            chat_name=target.name,
            query=target.query,
            action="open",
            status="title-guard-relaxed",
            db_path=mirror_db,
            screenshot_path=str(opened_path),
            metadata={"target": target.__dict__, "guard": guard},
        )

    if download_file_title:
        action_window = window_from_guard(guard) or window
        result = download_visible_file_card(
            env,
            action_window,
            download_file_title,
            download_root or (Path.home() / "Documents" / "xwechat_files"),
            out_dir,
            shot_prefix,
            pause=pause,
            wait_seconds=download_wait_seconds,
            expected_size=download_file_size,
            expected_md5=download_file_md5,
        )
        record_event(
            chat_name=target.name,
            query=target.query,
            action="download-file",
            direction="inbound",
            message=download_file_title,
            status=str(result.get("status") or "failed"),
            db_path=mirror_db,
            screenshot_path=str(result.get("screenshot_path") or opened_path),
            metadata={"target": target.__dict__, "guard": guard, "download": result},
        )
        return {"target": target.name, "screenshot_prefix": shot_prefix, **result}

    if outgoing_file is not None:
        action_window = window_from_guard(guard) or window
        identity = file_transport_identity(outgoing_file)
        record_event(
            chat_name=target.name,
            query=target.query,
            action="file_send_intent",
            direction="outbound",
            message=outgoing_file.name,
            status="sending",
            db_path=mirror_db,
            screenshot_path=str(opened_path),
            metadata={
                "target": target.__dict__,
                "guard": guard,
                "file_identity": identity,
            },
        )
        result = send_file_to_open_chat(
            env,
            action_window,
            target,
            outgoing_file,
            out_dir,
            shot_prefix,
            pause=pause,
        )
        record_event(
            chat_name=target.name,
            query=target.query,
            action="send_file",
            direction="outbound",
            message=outgoing_file.name,
            status="sent",
            db_path=mirror_db,
            screenshot_path=str(result.get("screenshot_path") or opened_path),
            metadata={
                "target": target.__dict__,
                "guard": guard,
                "file_identity": identity,
                "transport_result": result,
            },
        )
        return {"target": target.name, "screenshot_prefix": shot_prefix, **result}

    if not do_send and not compose_dry_run:
        record_event(
            chat_name=target.name,
            query=target.query,
            action="open",
            status="dry-run-opened",
            db_path=mirror_db,
            screenshot_path=str(opened_path),
            metadata={"target": target.__dict__, "guard": guard},
        )
        return {"target": target.name, "status": "dry-run-opened", "screenshot_prefix": shot_prefix}

    compose_window = window_from_guard(guard) or window
    focus(env, compose_window)

    clear_composer(env, compose_window, pause)
    paste_text(env, message)
    verify_composer_text(env, message)
    time.sleep(pause)
    composed_path = out_dir / f"{shot_prefix}-composed.png"
    screenshot(env, composed_path)
    if same_screenshot(opened_path, composed_path):
        raise RuntimeError(f"Message compose did not visibly change the WeChat window for {target.name}")
    if do_send:
        receipt = prepare_receipt(target.__dict__)
        assert pending_path is not None
        retain_pending_receipt(pending_path, receipt)
        key(env, "Return")
        time.sleep(pause)
        sent_path = out_dir / f"{shot_prefix}-sent.png"
        screenshot(env, sent_path)
        delivery_verification = wait_native_receipt(receipt, message)
        if not delivery_verification:
            record_event(chat_name=target.name, query=target.query, action="send", direction="outbound",
                         message=message, status="uncertain", db_path=mirror_db,
                         screenshot_path=str(sent_path), metadata={"guard": guard, "native_receipt_pending": True})
            raise RuntimeError("WECHAT_GUI_SEND_UNCERTAIN: no exact native outgoing message confirmed after Send")
        status = "sent"
        evidence_path = sent_path
    else:
        status = "dry-run-composed"
        evidence_path = composed_path
    record_event(
        chat_name=target.name,
        query=target.query,
        action="send",
        direction="outbound",
        message=message,
        status=status,
        db_path=mirror_db,
        screenshot_path=str(evidence_path),
        metadata={"target": target.__dict__, "guard": guard,
                  "delivery_verification": delivery_verification if do_send else None},
    )
    if do_send and pending_path is not None:
        pending_path.unlink(missing_ok=True)
    return {"target": target.name, "status": status, "screenshot_prefix": shot_prefix}


def send_file_to_open_chat(
    env: dict[str, str],
    window: Window,
    target: TargetSpec,
    file_path: Path,
    out_dir: Path,
    shot_prefix: str,
    *,
    pause: float,
) -> dict[str, Any]:
    """Send one file while the exact-target title guard and GUI lock remain active."""
    path = file_path.expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"File does not exist: {path}")

    focus(env, window)
    preflight_path = out_dir / f"{shot_prefix}-file-preflight.png"
    screenshot(env, preflight_path)
    raise_if_wechat_locked(
        env,
        window,
        preflight_path,
        out_dir / f"{shot_prefix}-file-preflight-lock.png",
        "before file selection",
    )
    clear_composer(env, window, min(max(pause, 0.1), 0.5))

    click(
        env,
        window.x + int(window.width * 0.47),
        window.y + window.height - 132,
    )
    time.sleep(max(0.5, pause))
    chooser = wait_for_verified_file_chooser(env, window)
    paste_path_into_file_chooser(env, path)
    time.sleep(max(0.5, pause))
    wait_for_wechat_focus_after_picker(env, window)

    selected_path = out_dir / f"{shot_prefix}-file-selected.png"
    screenshot(env, selected_path)
    raise_if_wechat_locked(
        env,
        window,
        selected_path,
        out_dir / f"{shot_prefix}-file-selected-lock.png",
        "after file selection",
    )
    if same_screenshot(preflight_path, selected_path):
        raise RuntimeError(
            "WECHAT_FILE_ATTACHMENT_NOT_STAGED: the verified chooser closed "
            f"without creating a visible attachment draft for {path.name}"
        )
    selected_guard = verify_opened_title(
        env,
        window,
        selected_path,
        target,
        out_dir / f"{shot_prefix}-file-selected-title.png",
        "file_selected",
    )
    if not selected_guard.get("ok"):
        raise RuntimeError(
            "WECHAT_FILE_TARGET_CHANGED: exact chat title guard failed after "
            f"native file selection for {path.name}"
        )

    click(
        env,
        window.x + window.width - 58,
        window.y + window.height - 34,
    )
    time.sleep(max(1.0, pause))
    sent_path = out_dir / f"{shot_prefix}-file-sent.png"
    screenshot(env, sent_path)
    raise_if_wechat_locked(
        env,
        window,
        sent_path,
        out_dir / f"{shot_prefix}-file-sent-lock.png",
        "after file submission",
    )
    if same_screenshot(selected_path, sent_path):
        raise RuntimeError(
            "WECHAT_FILE_SEND_VERIFY_FAILED: native file selection did not "
            f"produce a visible sent state for {path.name}"
        )
    return {
        "status": "sent-file-submitted",
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "screenshot_path": str(sent_path),
        "file_chooser": {
            "title": chooser.title,
            "window_class": chooser.window_class,
        },
        "selected_title_guard": selected_guard,
    }


def active_window_identity(env: dict[str, str]) -> WindowIdentity | None:
    active = run(["xdotool", "getactivewindow"], env=env, check=False).stdout.strip()
    if not active:
        active = run(["xdotool", "getwindowfocus"], env=env, check=False).stdout.strip()
    if not active:
        return None
    wid = active.splitlines()[-1].strip()
    return read_window_identity(env, wid)


def read_window_identity(env: dict[str, str], wid: str) -> WindowIdentity:
    title = run(["xdotool", "getwindowname", wid], env=env, check=False).stdout.strip()
    properties = run(
        ["xprop", "-id", wid, "WM_CLASS", "WM_TRANSIENT_FOR"], env=env, check=False
    ).stdout
    class_match = re.search(r'^WM_CLASS\([^)]*\) = (.*)$', properties, re.MULTILINE)
    window_class = " ".join(re.findall(r'"([^"\\]*)"', class_match[1])) if class_match else ""
    parent = re.search(r'^WM_TRANSIENT_FOR.*?window id # (0x[0-9a-fA-F]+)', properties, re.MULTILINE)
    return WindowIdentity(wid=wid, title=title, window_class=window_class,
                          transient_for=str(int(parent[1], 16)) if parent else "")


def visible_window_identities(env: dict[str, str]) -> list[WindowIdentity]:
    """List visible X11 windows when the desktop has no active-window manager."""

    result = run(
        ["xdotool", "search", "--onlyvisible", "--name", ".*"],
        env=env,
        check=False,
    )
    identities: list[WindowIdentity] = []
    seen: set[str] = set()
    for raw_wid in result.stdout.splitlines():
        wid = raw_wid.strip()
        if not wid or wid in seen:
            continue
        seen.add(wid)
        identities.append(read_window_identity(env, wid))
    return identities


def is_verified_file_chooser(identity: WindowIdentity | None, wechat_window: Window) -> bool:
    if identity is None or identity.wid == wechat_window.wid:
        return False
    if identity.title.strip().casefold() in {"open", "打开", "打開"}:
        # Qt's native chooser is simply "Open". Bind it to the exact WeChat
        # parent; a generic title in another application is not a safe picker.
        try:
            main_wid = str(int(wechat_window.wid, 16 if wechat_window.wid.startswith("0x") else 10))
        except ValueError:
            main_wid = wechat_window.wid
        return ("wechat" in identity.window_class.casefold().split()
                and identity.transient_for == main_wid)
    folded = f"{identity.title}\n{identity.window_class}".casefold()
    markers = (
        "file chooser",
        "filechooser",
        "open file",
        "select file",
        "choose file",
        "choose a file",
        "gtkfilechooserdialog",
        "xdg-desktop-portal",
        "打开文件",
        "打開檔案",
        "选择文件",
        "選擇檔案",
        "选择一个文件",
        "選擇一個檔案",
    )
    return any(marker in folded for marker in markers)


def wait_for_verified_file_chooser(
    env: dict[str, str],
    wechat_window: Window,
    *,
    timeout: float | None = None,
) -> WindowIdentity:
    if timeout is None:
        timeout = float(os.environ.get("WECHAT_FILE_CHOOSER_WAIT_SECONDS", "4"))
    deadline = time.monotonic() + max(0.1, timeout)
    last = None
    while time.monotonic() < deadline:
        last = active_window_identity(env)
        if is_verified_file_chooser(last, wechat_window):
            return last
        for candidate in visible_window_identities(env):
            if is_verified_file_chooser(candidate, wechat_window):
                return candidate
        time.sleep(0.1)
    detail = "none" if last is None else f"{last.title!r}/{last.window_class!r}"
    raise RuntimeError(
        "WECHAT_FILE_CHOOSER_NOT_OPEN: refusing to paste a local path because "
        f"no distinct native file chooser was verified (active={detail})"
    )


def wait_for_wechat_focus_after_picker(
    env: dict[str, str],
    wechat_window: Window,
    *,
    timeout: float | None = None,
) -> None:
    if timeout is None:
        timeout = float(os.environ.get("WECHAT_FILE_PICKER_RETURN_SECONDS", "6"))
    deadline = time.monotonic() + max(0.1, timeout)
    last = None
    while time.monotonic() < deadline:
        last = active_window_identity(env)
        if last is not None and last.wid == wechat_window.wid:
            return
        visible = visible_window_identities(env)
        if not any(is_verified_file_chooser(item, wechat_window) for item in visible):
            if any(item.wid == wechat_window.wid for item in visible):
                focus(env, wechat_window)
                return
        time.sleep(0.1)
    detail = "none" if last is None else f"{last.title!r}/{last.window_class!r}"
    raise RuntimeError(
        "WECHAT_FILE_PICKER_DID_NOT_RETURN: refusing to submit because the "
        f"exact WeChat window did not regain focus (active={detail})"
    )


def paste_path_into_file_chooser(env: dict[str, str], path: Path) -> None:
    hotkey(env, "ctrl+l")
    time.sleep(0.2)
    paste_text(env, str(path))
    time.sleep(0.2)
    key(env, "Return")


def raise_if_wechat_locked(
    env: dict[str, str],
    window: Window,
    screenshot_path: Path,
    crop_path: Path,
    stage: str,
) -> None:
    locked = detect_wechat_locked(env, window, screenshot_path, crop_path)
    if not locked.get("locked"):
        return
    ocr_text = str(locked.get("ocr_text") or "").replace("\n", " ")[:300]
    raise WeChatLockedError(
        "WECHAT_LOCKED: file send blocked "
        f"{stage}; screenshot={screenshot_path}; "
        f"lock_crop={locked.get('lock_crop')}; ocr={ocr_text}"
    )


def download_visible_file_card(
    env: dict[str, str],
    window: Window,
    title: str,
    download_root: Path,
    out_dir: Path,
    shot_prefix: str,
    *,
    pause: float,
    wait_seconds: float,
    expected_size: int = 0,
    expected_md5: str = "",
) -> dict[str, Any]:
    """Download one exact file card after the guarded chat title is open."""
    download_root = download_root.expanduser().resolve()
    baseline = exact_download_matches(download_root, title)
    existing = newest_complete_download(baseline, expected_size=expected_size, expected_md5=expected_md5)
    if existing is not None:
        return {
            "status": "already-downloaded",
            "filename": title,
            "downloaded_path": str(existing),
            "size_bytes": existing.stat().st_size,
            "reason": "an exact complete native-cache file already exists",
        }
    source_path = out_dir / f"{shot_prefix}-file-card-source.png"
    screenshot(env, source_path)
    tsv = run(
        ["tesseract", str(source_path), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "11", "tsv"],
        env=env,
        check=False,
    ).stdout
    card = locate_file_card_from_tsv(tsv, title, window)
    if not card:
        focused_tsv, focused_region = focused_file_card_ocr(
            env,
            source_path,
            window,
            out_dir / f"{shot_prefix}-file-card-focused.png",
        )
        card = locate_file_card_from_tsv(
            focused_tsv,
            title,
            window,
            offset_x=int(focused_region["left"]),
            offset_y=int(focused_region["top"]),
            coordinate_scale=float(focused_region["scale"]),
        )
    if not card:
        return {
            "status": "file-card-not-found",
            "filename": title,
            "screenshot_path": str(source_path),
            "reason": "the exact filename was not visible in the guarded source chat",
        }
    popup: Window | None = None
    for point in card.get("click_candidates") or [[card["click_x"], card["click_y"]]]:
        click(env, int(point[0]), int(point[1]))
        popup = wait_for_new_wechat_popup(env, excluded_wids={window.wid}, timeout=max(3.0, pause * 3))
        if popup is not None:
            break
    popup_path = out_dir / f"{shot_prefix}-file-download-popup.png"
    screenshot(env, popup_path)
    if popup is None:
        downloaded = wait_for_exact_download(
            download_root,
            title,
            baseline,
            timeout=wait_seconds,
            expected_size=expected_size,
            expected_md5=expected_md5,
        )
        if downloaded:
            return {
                "status": "downloaded-directly",
                "filename": title,
                "downloaded_path": str(downloaded),
                "size_bytes": downloaded.stat().st_size,
                "card_match": card,
                "screenshot_path": str(popup_path),
            }
        return {
            "status": "file-popup-not-found",
            "filename": title,
            "card_match": card,
            "screenshot_path": str(popup_path),
        }
    popup_text = ocr_window_text(env, popup, popup_path, out_dir / f"{shot_prefix}-file-download-popup-crop.png")
    identity_score = filename_identity_score(title, popup_text)
    if Path(title).suffix.lower().lstrip(".") not in normalize_title(popup_text) or identity_score < 0.35:
        key(env, "Escape")
        return {
            "status": "file-popup-identity-mismatch",
            "filename": title,
            "identity_score": round(identity_score, 3),
            "screenshot_path": str(popup_path),
        }
    button = locate_download_button(env, popup, popup_path)
    button_x = int(button.get("click_x") or (popup.x + popup.width * 0.5))
    button_y = int(button.get("click_y") or (popup.y + popup.height * 0.77))
    click(env, button_x, button_y)
    time.sleep(max(0.8, pause))
    clicked_path = out_dir / f"{shot_prefix}-file-download-clicked.png"
    screenshot(env, clicked_path)
    downloaded = wait_for_exact_download(
        download_root,
        title,
        baseline,
        timeout=wait_seconds,
        expected_size=expected_size,
        expected_md5=expected_md5,
    )
    if downloaded:
        return {
            "status": "downloaded",
            "filename": title,
            "downloaded_path": str(downloaded),
            "size_bytes": downloaded.stat().st_size,
            "card_match": card,
            "identity_score": round(identity_score, 3),
            "screenshot_path": str(clicked_path),
        }
    return {
        "status": "download-started",
        "filename": title,
        "card_match": card,
        "identity_score": round(identity_score, 3),
        "screenshot_path": str(clicked_path),
        "reason": "the exact file was not complete before the bounded wait ended",
    }


def focused_file_card_ocr(
    env: dict[str, str],
    source_path: Path,
    window: Window,
    crop_path: Path,
) -> tuple[str, dict[str, float]]:
    """Run a higher-resolution OCR pass over the conversation pane.

    Full-desktop OCR can truncate small attachment names even when their card is
    clearly visible. Keep the exact-title guard, but enlarge only the guarded
    chat pane before retrying so the extension and remaining filename survive.
    """
    left = window.x + int(window.width * 0.34)
    top = window.y + 70
    right = window.x + window.width
    bottom = window.y + window.height - 105
    width = max(1, right - left)
    height = max(1, bottom - top)
    scale = 3.0
    run(
        [
            "convert",
            str(source_path),
            "-crop",
            f"{width}x{height}+{left}+{top}",
            "-resize",
            f"{int(scale * 100)}%",
            str(crop_path),
        ],
        env=env,
        check=False,
    )
    proc = run(
        [
            "tesseract",
            str(crop_path),
            "stdout",
            "-l",
            "chi_sim+chi_tra+eng",
            "--psm",
            "11",
            "tsv",
        ],
        env=env,
        check=False,
    )
    return proc.stdout, {"left": float(left), "top": float(top), "scale": scale}


def locate_file_card_from_tsv(
    tsv_text: str,
    title: str,
    window: Window,
    *,
    offset_x: int = 0,
    offset_y: int = 0,
    coordinate_scale: float = 1.0,
) -> dict[str, Any] | None:
    scale = max(0.01, float(coordinate_scale))
    words = tesseract_words(tsv_text)
    if offset_x or offset_y or scale != 1.0:
        words = [
            {
                **word,
                "left": int(round(offset_x + int(word["left"]) / scale)),
                "top": int(round(offset_y + int(word["top"]) / scale)),
                "width": max(1, int(round(int(word["width"]) / scale))),
                "height": max(1, int(round(int(word["height"]) / scale))),
            }
            for word in words
        ]
    extension = Path(title).suffix.lower().lstrip(".")
    if not extension:
        return None
    candidates: list[dict[str, Any]] = []
    right_edge = window.x + window.width
    min_x = window.x + int(window.width * 0.34)
    min_y = window.y + 90
    max_y = window.y + window.height - 120
    for word in words:
        observed_word = normalize_title(str(word["text"]))
        if extension not in observed_word:
            continue
        center_x = int(word["left"]) + int(word["width"]) // 2
        center_y = int(word["top"]) + int(word["height"]) // 2
        if not (min_x <= center_x <= right_edge and min_y <= center_y <= max_y):
            continue
        nearby = [
            item
            for item in words
            if min_x <= int(item["left"]) <= right_edge
            and abs((int(item["top"]) + int(item["height"]) // 2) - center_y) <= 48
        ]
        nearby.sort(key=lambda item: (int(item["top"]), int(item["left"])))
        observed = "".join(str(item["text"]) for item in nearby)
        score = filename_identity_score(title, observed)
        if score < 0.35:
            continue
        candidates.append(
            {
                "click_x": center_x,
                "click_y": center_y,
                "click_candidates": [
                    [min(right_edge - 20, center_x + 177), center_y],
                    [min(right_edge - 20, center_x + 80), center_y + 15],
                    [center_x, center_y + 15],
                    [min(right_edge - 20, center_x + 150), center_y + 15],
                    [max(min_x + 20, center_x - 50), center_y + 15],
                ],
                "identity_score": round(score, 3),
                "observed": observed[:240],
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (float(item["identity_score"]), int(item["click_y"])), reverse=True)
    return candidates[0]


def filename_identity_score(expected: str, observed: str) -> float:
    expected_normalized = normalize_title(expected)
    observed_normalized = normalize_title(observed)
    if not expected_normalized or not observed_normalized:
        return 0.0
    return SequenceMatcher(None, expected_normalized, observed_normalized).ratio()


def tesseract_words(tsv_text: str) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    for row in reader:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        try:
            left = int(float(row.get("left") or 0))
            top = int(float(row.get("top") or 0))
            width = int(float(row.get("width") or 0))
            height = int(float(row.get("height") or 0))
        except ValueError:
            continue
        if width > 0 and height > 0:
            words.append({"text": text, "left": left, "top": top, "width": width, "height": height})
    return words


def visible_wechat_windows(env: dict[str, str]) -> list[Window]:
    windows: list[Window] = []
    ids = run(["xdotool", "search", "--onlyvisible", "--class", "wechat"], env=env, check=False).stdout.split()
    for wid in ids:
        geom = run(["xdotool", "getwindowgeometry", "--shell", wid], env=env, check=False).stdout
        values: dict[str, int] = {}
        for line in geom.splitlines():
            if "=" not in line:
                continue
            key_name, raw = line.split("=", 1)
            try:
                values[key_name] = int(raw)
            except ValueError:
                continue
        if {"X", "Y", "WIDTH", "HEIGHT"} <= values.keys():
            windows.append(Window(wid, values["X"], values["Y"], values["WIDTH"], values["HEIGHT"]))
    return windows


def wait_for_new_wechat_popup(env: dict[str, str], *, excluded_wids: set[str], timeout: float) -> Window | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        focused = focused_window(env)
        candidates = [
            item
            for item in visible_wechat_windows(env)
            if item.wid not in excluded_wids and 360 <= item.width <= 900 and 280 <= item.height <= 850
        ]
        if focused:
            candidates.sort(key=lambda item: (item.wid == focused.wid, item.width * item.height), reverse=True)
        if candidates:
            return candidates[0]
        time.sleep(0.4)
    return None


def ocr_window_text(env: dict[str, str], window: Window, source_path: Path, crop_path: Path) -> str:
    run(
        [
            "convert",
            str(source_path),
            "-crop",
            f"{window.width}x{window.height}+{window.x}+{window.y}",
            "-resize",
            "160%",
            str(crop_path),
        ],
        env=env,
        check=False,
    )
    return run(
        ["tesseract", str(crop_path), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "11"],
        env=env,
        check=False,
    ).stdout


def locate_download_button(env: dict[str, str], popup: Window, screenshot_path: Path) -> dict[str, int]:
    tsv = run(
        ["tesseract", str(screenshot_path), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "11", "tsv"],
        env=env,
        check=False,
    ).stdout
    for word in tesseract_words(tsv):
        text = normalize_title(str(word["text"]))
        center_x = int(word["left"]) + int(word["width"]) // 2
        center_y = int(word["top"]) + int(word["height"]) // 2
        if text in {"download", "下载", "下載"} and popup.x <= center_x <= popup.x + popup.width and popup.y <= center_y <= popup.y + popup.height:
            return {"click_x": center_x, "click_y": center_y}
    return {}


def exact_download_matches(root: Path, title: str) -> dict[str, tuple[int, int]]:
    matches: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return matches
    for path in root.rglob(title):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if path.is_file():
            matches[str(path.resolve())] = (stat_result.st_size, stat_result.st_mtime_ns)
    return matches


def newest_complete_download(
    matches: dict[str, tuple[int, int]],
    *,
    expected_size: int = 0,
    expected_md5: str = "",
) -> Path | None:
    complete = [
        (Path(raw), state)
        for raw, state in matches.items()
        if state[0] > 0 and not raw.lower().endswith((".tmp", ".part", ".download"))
    ]
    if not complete:
        return None
    complete.sort(key=lambda item: (item[1][1], item[1][0], str(item[0])), reverse=True)
    for path, state in complete:
        if expected_size > 0 and state[0] != expected_size:
            continue
        if expected_md5:
            try:
                if file_md5(path) != expected_md5.lower():
                    continue
            except OSError:
                continue
        return path
    return None


def wait_for_exact_download(
    root: Path,
    title: str,
    baseline: dict[str, tuple[int, int]],
    *,
    timeout: float,
    expected_size: int = 0,
    expected_md5: str = "",
) -> Path | None:
    deadline = time.monotonic() + max(1.0, timeout)
    stable: dict[str, tuple[int, int]] = {}
    while time.monotonic() < deadline:
        for raw, state in exact_download_matches(root, title).items():
            if baseline.get(raw) == state:
                continue
            size = state[0]
            previous_size, count = stable.get(raw, (-1, 0))
            count = count + 1 if previous_size == size and size > 0 else 0
            stable[raw] = (size, count)
            if count >= 2:
                path = Path(raw)
                if expected_size > 0 and size != expected_size:
                    continue
                if expected_md5:
                    try:
                        if file_md5(path) != expected_md5.lower():
                            continue
                    except OSError:
                        continue
                return path
        time.sleep(1.0)
    return None


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clear_composer(env: dict[str, str], window: Window, pause: float) -> None:
    """Clear stale text or attachment drafts before composing a new send."""
    # Click the message composer. This is deliberately biased toward the lower
    # right pane so it does not send from the search box.
    click(env, window.x + int(window.width * 0.66), window.y + window.height - 80)
    time.sleep(pause)
    for key_name in ("Escape", "Escape", "ctrl+a", "BackSpace", "Delete"):
        if "+" in key_name:
            hotkey(env, key_name)
        else:
            key(env, key_name)
        time.sleep(0.15)


def open_target(
    env: dict[str, str],
    window: Window,
    target: TargetSpec,
    pause: float,
    out_dir: Path,
    shot_prefix: str,
    skip_title_guard: bool,
    prefer_current: bool = False,
    allow_search: bool = True,
    relaxed_visible_fallback_allowed: bool = False,
    fail_closed_after_visible_match: bool = False,
) -> dict[str, Any]:
    def verify(label: str) -> dict[str, Any]:
        time.sleep(max(pause, float(os.environ.get("WECHAT_INITIAL_TITLE_WAIT", "1.2"))))
        if label.startswith("visible_chat_list_ocr"):
            retry_seconds = float(os.environ.get("WECHAT_VISIBLE_ROW_TITLE_RETRY_SECONDS", "1.8"))
        else:
            retry_seconds = float(os.environ.get("WECHAT_TITLE_RETRY_SECONDS", "3.5"))
        deadline = time.monotonic() + max(max(pause, 1.8), retry_seconds)
        last_guard: dict[str, Any] = {"ok": False, "method": label, "ocr_text": ""}
        while True:
            opened = out_dir / f"{shot_prefix}-opened.png"
            screenshot(env, opened)
            if skip_title_guard:
                compose_window = focused_window(env) or window
                return {
                    "ok": True,
                    "method": label,
                    "ocr_text": "",
                    "compose_window": window_to_dict(compose_window),
                }
            for candidate in title_window_candidates(env, window):
                suffix = "title" if candidate.wid == window.wid else f"title-{safe_name(candidate.wid)}"
                guard = verify_opened_title(env, candidate, opened, target, out_dir / f"{shot_prefix}-{suffix}.png", label)
                if guard["ok"]:
                    return guard
                last_guard = guard
            if time.monotonic() >= deadline:
                return last_guard
            time.sleep(max(pause, 1.0))

    attempts: list[dict[str, Any]] = []

    if prefer_current:
        current_guard = verify("current")
        if current_guard["ok"]:
            return current_guard
        attempts.append(current_guard)

    if os.environ.get("WECHAT_VISIBLE_CHAT_LIST_OCR", "1") != "0":
        match = click_visible_chat_list_match(env, window, target, out_dir, shot_prefix)
        if match:
            guard = verify(f"visible_chat_list_ocr:{match['text']}")
            attempts.append({**guard, "visible_chat_list_match": match})
            if guard["ok"]:
                return {**guard, "visible_chat_list_match": match}
            click(env, window.x + int(match["click_x"]), window.y + int(match["click_y"]))
            guard = verify(f"visible_chat_list_ocr_retry:{match['text']}")
            attempts.append({**guard, "visible_chat_list_match": match})
            if guard["ok"]:
                return {**guard, "visible_chat_list_match": match}
            fallback_guard = visible_chat_list_fallback_guard(guard, target, match)
            if fallback_guard and relaxed_visible_fallback_allowed:
                return fallback_guard
            if fail_closed_after_visible_match:
                return {
                    **guard,
                    "ok": False,
                    "method": str(guard.get("method") or "visible_chat_list_ocr_retry"),
                    "exact_visible_match_open_failed": True,
                    "attempts": attempts,
                    "visible_chat_list_match": match,
                }
            if not global_search_allowed(allow_search, target):
                return {
                    **guard,
                    "ok": False,
                    "method": str(guard.get("method") or "visible_chat_list_ocr"),
                    "search_disabled": True,
                    "attempts": attempts,
                    "visible_chat_list_match": match,
                }

    if target.open_click:
        click(env, window.x + target.open_click[0], window.y + target.open_click[1])
        guard = verify("open_click")
        attempts.append(guard)
        if guard["ok"]:
            return guard
        double_click(env, window.x + target.open_click[0], window.y + target.open_click[1])
        guard = verify("open_click_double")
        attempts.append(guard)
        if guard["ok"]:
            return guard

    for label, point in target_explicit_click_candidates(target):
        click(env, window.x + point[0], window.y + point[1])
        guard = verify(f"{label}_direct")
        attempts.append(guard)
        if guard["ok"]:
            return guard
        double_click(env, window.x + point[0], window.y + point[1])
        guard = verify(f"{label}_direct_double")
        attempts.append(guard)
        if guard["ok"]:
            return guard
        close_non_target_wechat_windows(env, window, target)
        focus(env, window)

    if not global_search_allowed(allow_search, target):
        guard = attempts[-1] if attempts else verify("search_disabled_current")
        return {
            **guard,
            "ok": False,
            "method": str(guard.get("method") or "search_disabled"),
            "search_disabled": True,
            "attempts": attempts,
        }

    search_query = preferred_search_query(target)
    search_for_target(env, window, search_query, pause)
    screenshot(env, out_dir / f"{shot_prefix}-search.png")
    for label, point in target_click_candidates(target):
        click(env, window.x + point[0], window.y + point[1])
        guard = verify(label)
        attempts.append(guard)
        if guard["ok"]:
            return guard
        double_click(env, window.x + point[0], window.y + point[1])
        guard = verify(f"{label}_double")
        attempts.append(guard)
        if guard["ok"]:
            return guard
        search_for_target(env, window, search_query, pause)

    key(env, "Return")
    guard = verify("return")
    if attempts:
        guard = {**guard, "attempts": attempts}
    return guard


def preferred_search_query(target: TargetSpec) -> str:
    """Separate a stable route key from the title visible in WeChat search.

    A target ``name``/``query`` may intentionally be a durable session scope
    such as an account alias. When that alias and the guarded visible title are
    unrelated, searching the alias can produce an empty result surface even
    though the contact is present. Prefer the guarded title in that case; keep
    deliberate title prefixes such as ``MEMO写作`` unchanged.
    """
    query = target.query.strip()
    expected = target.expected_title.strip()
    normalized_query = normalize_title(query)
    normalized_expected = normalize_title(expected)
    if not normalized_expected or not normalized_query:
        return query or expected
    if normalized_query in normalized_expected or normalized_expected in normalized_query:
        return query
    return expected


def visible_chat_list_fallback_guard(
    guard: dict[str, Any],
    target: TargetSpec,
    match: dict[str, Any],
) -> dict[str, Any] | None:
    """Accept a configured visible row when header OCR is too noisy.

    Emoji-heavy or compact Chinese chat titles can OCR as Latin noise even when
    the correct row is selected. This fallback is deliberately narrower than
    the generic relaxed title guard: it requires an exact source row match from
    the visible chat list, no rejected chat/search surface, and an operator
    target that explicitly enables title fallback.
    """
    if not target.allow_title_guard_fallback:
        return None
    if guard.get("surface_reject_reason"):
        return None
    normalized_match = normalize_title(str(match.get("normalized") or match.get("text") or ""))
    expected = [
        normalize_title(item)
        for item in (target.expected_title, *target.expected_title_aliases, target.name, target.query)
        if normalize_title(item)
    ]
    if not normalized_match or not any(item in normalized_match for item in expected):
        return None
    return {
        **guard,
        "ok": True,
        "relaxed_title_guard": True,
        "visible_chat_list_title_guard": True,
        "visible_chat_list_match": match,
        "title_guard_source": "visible_chat_list_match",
    }


def click_visible_chat_list_match(
    env: dict[str, str],
    window: Window,
    target: TargetSpec,
    out_dir: Path,
    shot_prefix: str,
) -> dict[str, Any] | None:
    """Click a matching row already visible in the left chat list."""
    region = chat_list_crop_region(window)
    if region is None:
        return None
    screenshot_path = out_dir / f"{shot_prefix}-chat-list-source.png"
    crop_path = out_dir / f"{shot_prefix}-chat-list.png"
    screenshot(env, screenshot_path)
    if not screenshot_path.exists():
        return None
    run(
        [
            "convert",
            str(screenshot_path),
            "-crop",
            f"{region['width']}x{region['height']}+{region['left']}+{region['top']}",
            str(crop_path),
        ],
        env=env,
    )
    proc = run(["tesseract", str(crop_path), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "11", "tsv"], env=env, check=False)
    match = visible_chat_list_match_from_tsv(proc.stdout, target)
    if not match:
        return None
    click_x = target.result_click[0] if target.result_click else int(region["left"] - window.x + 110)
    click_y = int(region["top"] - window.y + float(match["center_y"]))
    focus(env, window)
    click(env, window.x + click_x, window.y + click_y)
    return {
        **match,
        "crop": str(crop_path),
        "click_x": click_x,
        "click_y": click_y,
    }


def chat_list_crop_region(window: Window) -> dict[str, int] | None:
    """Return a conservative crop of the left chat list, excluding the sidebar."""
    if window.width < 760 or window.height < 360:
        return None
    left = window.x + 60
    top = window.y + 60
    right = window.x + min(370, max(300, int(window.width * 0.36)))
    bottom = window.y + window.height
    return {
        "left": left,
        "top": top,
        "width": max(240, right - left),
        "height": max(220, bottom - top),
    }


def visible_chat_list_match_from_tsv(tsv_text: str, target: TargetSpec) -> dict[str, Any] | None:
    expected_titles = [
        item
        for item in (
            target.expected_title,
            *target.expected_title_aliases,
            target.name,
            target.query,
        )
        if normalize_title(item)
    ]
    if not expected_titles:
        return None
    rows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    for raw in reader:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        try:
            left = int(float(raw.get("left") or 0))
            top = int(float(raw.get("top") or 0))
            width = int(float(raw.get("width") or 0))
            height = int(float(raw.get("height") or 0))
        except ValueError:
            continue
        if width <= 0 or height <= 0:
            continue
        key = (
            str(raw.get("block_num") or ""),
            str(raw.get("par_num") or ""),
            str(raw.get("line_num") or ""),
        )
        rows.setdefault(key, []).append({"text": text, "left": left, "top": top, "width": width, "height": height})
    matches: list[dict[str, Any]] = []
    for words in rows.values():
        words.sort(key=lambda item: (int(item["left"]), int(item["top"])))
        # Tesseract sometimes puts a few pixels from the avatar into the same
        # TSV line as the title. Try bounded title prefixes that begin in the
        # title column instead of rejecting the whole row because of that
        # unrelated leading token. Prefixes also avoid appending a timestamp
        # that Tesseract occasionally groups onto the same line.
        for start, first in enumerate(words):
            first_left = int(first["left"])
            if first_left < 45 or first_left > 145:
                continue
            for end in range(start + 1, len(words) + 1):
                title_words = words[start:end]
                text = "".join(str(item["text"]) for item in title_words)
                normalized = normalize_title(text)
                identity = visible_chat_row_identity(
                    text,
                    expected_titles,
                    allow_ocr_repair=target.allow_title_guard_fallback,
                )
                if not normalized or not identity:
                    continue
                min_left = int(title_words[0]["left"])
                max_right = max(int(item["left"]) + int(item["width"]) for item in title_words)
                min_top = min(int(item["top"]) for item in title_words)
                max_bottom = max(int(item["top"]) + int(item["height"]) for item in title_words)
                matches.append(
                    {
                        "text": text,
                        "normalized": normalized,
                        "left": min_left,
                        "top": min_top,
                        "right": max_right,
                        "bottom": max_bottom,
                        "center_y": (min_top + max_bottom) / 2.0,
                        "identity_mode": identity,
                    }
                )
    if not matches:
        return None
    matches.sort(key=lambda item: (int(item["top"]), abs(int(item["left"]) - 65)))
    return matches[0]


def target_explicit_click_candidates(target: TargetSpec) -> list[tuple[str, tuple[int, int]]]:
    """Click only operator-configured visible-list points before search fallback."""
    candidates: list[tuple[str, tuple[int, int]]] = []
    seen: set[tuple[int, int]] = set()

    def add(label: str, point: tuple[int, int] | None) -> None:
        if point is None or point in seen:
            return
        candidates.append((label, point))
        seen.add(point)

    add("result_click", target.result_click)
    for index, point in enumerate(target.fallback_clicks, start=1):
        add(f"fallback_click_{index}", point)
    if target.result_click:
        x, y = target.result_click
        add("result_click_row_center", (x, max(70, y - 26)))
        add("result_click_title_offset", (x + 35, max(70, y - 26)))
        add("result_click_preview_offset", (x + 35, y))
    return candidates


def target_click_candidates(target: TargetSpec) -> list[tuple[str, tuple[int, int]]]:
    """Return result click candidates while preserving configured points first."""
    candidates: list[tuple[str, tuple[int, int]]] = []
    seen: set[tuple[int, int]] = set()

    def add(label: str, point: tuple[int, int]) -> None:
        if point in seen:
            return
        candidates.append((label, point))
        seen.add(point)

    if target.result_click:
        x, y = target.result_click
        add("result_click", target.result_click)
        for label, point in (
            ("result_click_row_center", (x, max(70, y - 26))),
            ("result_click_title_offset", (x + 35, max(70, y - 26))),
            ("result_click_preview_offset", (x + 35, y)),
        ):
            add(label, point)
    for index, point in enumerate(target.fallback_clicks, start=1):
        add(f"fallback_click_{index}", point)
    for index, point in enumerate(((165, 100), (205, 100), (165, 125), (205, 125), (165, 155)), start=1):
        add(f"default_search_row_{index}", point)
    return candidates


def search_for_target(env: dict[str, str], window: Window, query: str, pause: float) -> None:
    click(env, window.x + 118, window.y + 46)
    time.sleep(pause)
    hotkey(env, "ctrl+a")
    key(env, "BackSpace")
    paste_text(env, query)
    time.sleep(max(pause, 1.6))


def global_search_allowed(requested: bool, target: TargetSpec) -> bool:
    """Keep WeChat's global account search out of normal chat delivery.

    The top-left search surface mixes contacts, mini-programs, web results, and
    account search. It is not a reliable exact-chat selector. Existing configs
    may retain ``allow_search`` for compatibility, but live use additionally
    requires an explicit operator environment opt-in.
    """
    return (
        requested
        and target.allow_search
        and os.environ.get("WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH", "0") == "1"
    )


def verify_opened_title(
    env: dict[str, str],
    window: Window,
    screenshot_path: Path,
    target: TargetSpec,
    crop_path: Path,
    method: str,
) -> dict[str, Any]:
    expected_titles = [target.expected_title, *target.expected_title_aliases]
    expected = [normalize_title(item) for item in expected_titles if normalize_title(item)]
    window_title = run(["xdotool", "getwindowname", window.wid], env=env, check=False).stdout.strip()
    window_reject_reason = chat_surface_reject_reason(window_title)
    window_title_ok = (
        bool(window_title)
        and not window_reject_reason
        and title_identity_matches(window_title, expected_titles)
    )
    if window_title_ok:
        surface_guard = detect_rejected_chat_surface(env, window, screenshot_path, crop_path)
        if surface_guard["reason"]:
            return rejected_title_guard_result(
                method,
                target,
                "",
                [],
                window_title,
                window,
                surface_guard["reason"],
                surface_guard.get("ocr_text", ""),
                surface_guard.get("crop", ""),
            )
        return {
            "ok": True,
            "method": method,
            "expected_title": target.expected_title,
            "expected_title_aliases": list(target.expected_title_aliases),
            "ocr_text": window_title,
            "title_crop": "",
            "title_crops": [],
            "window_title": window_title,
            "compose_window": window_to_dict(window),
        }
    if specific_window_title_nonmatch(window_title, expected):
        return {
            "ok": False,
            "method": method,
            "expected_title": target.expected_title,
            "expected_title_aliases": list(target.expected_title_aliases),
            "ocr_text": window_title,
            "title_crop": "",
            "title_crops": [],
            "window_title": window_title,
            "compose_window": window_to_dict(window),
            "surface_reject_reason": window_reject_reason,
            "window_title_nonmatch": True,
        }
    ocr_texts: list[str] = []
    crop_paths: list[str] = []
    ok = False
    for region in title_crop_regions(window):
        region_crop = crop_path.with_name(f"{crop_path.stem}-{region['label']}{crop_path.suffix}")
        run(
            [
                "convert",
                str(screenshot_path),
                "-crop",
                f"{region['width']}x{region['height']}+{region['left']}+{region['top']}",
                "-colorspace",
                "Gray",
                "-resize",
                "200%",
                str(region_crop),
            ],
            env=env,
        )
        proc = run(["tesseract", str(region_crop), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "6"], env=env, check=False)
        text = proc.stdout.strip()
        ocr_texts.append(text)
        crop_paths.append(str(region_crop))
        reject_reason = chat_surface_reject_reason(text)
        if reject_reason:
            return {
                "ok": False,
                "method": method,
                "expected_title": target.expected_title,
                "expected_title_aliases": list(target.expected_title_aliases),
                "ocr_text": "\n".join(text for text in ocr_texts if text).strip(),
                "title_crop": str(region_crop),
                "title_crops": crop_paths,
                "window_title": window_title,
                "compose_window": window_to_dict(window),
                "surface_reject_reason": reject_reason,
            }
        if title_identity_matches(text, expected_titles):
            surface_guard = detect_rejected_chat_surface(env, window, screenshot_path, region_crop)
            if surface_guard["reason"]:
                return rejected_title_guard_result(
                    method,
                    target,
                    "\n".join(text for text in ocr_texts if text).strip(),
                    crop_paths,
                    window_title,
                    window,
                    surface_guard["reason"],
                    surface_guard.get("ocr_text", ""),
                    surface_guard.get("crop", ""),
                )
            ok = True
            crop_path = region_crop
            break
    return {
        "ok": ok,
        "method": method,
        "expected_title": target.expected_title,
        "expected_title_aliases": list(target.expected_title_aliases),
        "ocr_text": "\n".join(text for text in ocr_texts if text).strip(),
        "title_crop": str(crop_path),
        "title_crops": crop_paths,
        "window_title": window_title,
        "compose_window": window_to_dict(window),
        "surface_reject_reason": window_reject_reason,
    }


def rejected_title_guard_result(
    method: str,
    target: TargetSpec,
    ocr_text: str,
    crop_paths: list[str],
    window_title: str,
    window: Window,
    reason: str,
    surface_ocr_text: str = "",
    surface_crop: str = "",
) -> dict[str, Any]:
    combined_ocr = "\n".join(text for text in (ocr_text, surface_ocr_text) if text).strip()
    return {
        "ok": False,
        "method": method,
        "expected_title": target.expected_title,
        "expected_title_aliases": list(target.expected_title_aliases),
        "ocr_text": combined_ocr,
        "title_crop": surface_crop or (crop_paths[-1] if crop_paths else ""),
        "title_crops": crop_paths,
        "window_title": window_title,
        "compose_window": window_to_dict(window),
        "surface_reject_reason": reason,
        "surface_ocr_text": surface_ocr_text,
        "surface_crop": surface_crop,
    }


def normalize_title(text: str) -> str:
    return "".join(ch.lower() for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def normalize_visible_chat_title(text: str, *, separator_hint: bool = False) -> str:
    normalized = normalize_title(text)
    if TITLE_T2S is not None:
        normalized = normalize_title(TITLE_T2S.convert(normalized))
    else:
        # Exact target selection cannot depend on an optional OpenCC install.
        # Fold only characters found in configured chat titles, then retain the
        # existing bounded one-character OCR repair below.
        normalized = normalize_title(normalized.translate(TITLE_SCRIPT_FOLD))
    if separator_hint:
        normalized = normalized.replace("一", "")
    return normalized


def visible_chat_row_identity(
    observed_text: str,
    expected_titles: list[str] | tuple[str, ...],
    *,
    allow_ocr_repair: bool,
) -> str:
    """Return the bounded identity mode for one visible chat-list title.

    Exact title matching remains preferred. The repair path accepts only one
    same-length OCR substitution in a reasonably long title. It never applies
    to header verification or arbitrary search results.
    """
    if title_identity_matches(observed_text, expected_titles):
        return "exact"
    if not allow_ocr_repair:
        return ""
    for raw_expected in expected_titles:
        expected = normalize_visible_chat_title(raw_expected)
        observed = normalize_visible_chat_title(
            observed_text,
            separator_hint=title_has_explicit_separator(raw_expected),
        )
        if len(expected) < 8 or len(observed) != len(expected):
            continue
        substitutions = sum(left != right for left, right in zip(observed, expected))
        if substitutions != 1:
            continue
        if SequenceMatcher(None, observed, expected).ratio() < 0.90:
            continue
        return "ocr-single-substitution"
    return ""


def title_identity_matches(observed_text: str, expected_titles: list[str] | tuple[str, ...]) -> bool:
    """Match an exact chat title while tolerating one known OCR separator error."""
    candidates = [line for line in str(observed_text or "").splitlines() if line.strip()]
    if not candidates:
        candidates = [str(observed_text or "")]
    for candidate in candidates:
        for raw_expected in expected_titles:
            expected = normalize_visible_chat_title(raw_expected)
            observed = normalize_visible_chat_title(
                candidate,
                separator_hint=title_has_explicit_separator(raw_expected),
            ).rstrip("0123456789")
            if not observed:
                continue
            if not expected:
                continue
            if observed == expected or (
                observed.startswith(expected) and len(observed) - len(expected) <= 3
            ):
                return True
    return False


def title_has_explicit_separator(text: str) -> bool:
    return any(character.isspace() or character in "-_—–－·•/" for character in str(text or ""))


def specific_window_title_nonmatch(window_title: str, expected: list[str]) -> bool:
    observed = normalize_title(window_title)
    if not observed:
        return False
    if any(item in observed for item in expected):
        return False
    generic_titles = {
        "wechat",
        "weixin",
        "weixinforlinux",
        "微信",
    }
    return observed not in generic_titles


def chat_surface_reject_reason(text: str) -> str:
    lowered = str(text or "").lower()
    normalized = normalize_title(text)
    raw_markers = {
        "ai search": "ai-search",
        " - search": "search-webview",
        "- search": "search-webview",
        "ask a follow-up": "ai-search",
    }
    normalized_markers = {
        "aisearch": "ai-search",
        "问ai": "ai-search",
        "問ai": "ai-search",
        "快速回答": "ai-search",
        "askafollowup": "ai-search",
    }
    for marker, reason in raw_markers.items():
        if marker in lowered:
            return reason
    for marker, reason in normalized_markers.items():
        if marker in normalized:
            return reason
    return ""


def detect_rejected_chat_surface(
    env: dict[str, str],
    window: Window,
    screenshot_path: Path,
    crop_path: Path,
) -> dict[str, str]:
    ocr_texts: list[str] = []
    crop_paths: list[str] = []
    for region in chat_surface_guard_regions(window):
        region_crop = crop_path.with_name(f"{crop_path.stem}-surface-{region['label']}{crop_path.suffix}")
        try:
            run(
                [
                    "convert",
                    str(screenshot_path),
                    "-crop",
                    f"{region['width']}x{region['height']}+{region['left']}+{region['top']}",
                    "-colorspace",
                    "Gray",
                    "-resize",
                    "160%",
                    str(region_crop),
                ],
                env=env,
            )
        except Exception as exc:
            return {"reason": "", "ocr_text": "", "crop": "", "error": str(exc)[:500]}
        proc = run(["tesseract", str(region_crop), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "6"], env=env, check=False)
        text = proc.stdout.strip()
        ocr_texts.append(text)
        crop_paths.append(str(region_crop))
        reason = chat_surface_reject_reason(text)
        if reason:
            return {"reason": reason, "ocr_text": text, "crop": str(region_crop)}
    return {"reason": "", "ocr_text": "\n".join(text for text in ocr_texts if text).strip(), "crop": crop_paths[-1] if crop_paths else ""}


def chat_surface_guard_regions(window: Window) -> list[dict[str, int | str]]:
    return [
        {
            "label": "top",
            "left": window.x,
            "top": window.y,
            "width": window.width,
            "height": min(120, window.height),
        },
        {
            "label": "bottom",
            "left": window.x + max(0, int(window.width * 0.30)),
            "top": window.y + max(0, window.height - 180),
            "width": max(300, int(window.width * 0.70)),
            "height": min(180, window.height),
        },
    ]


def detect_wechat_locked(env: dict[str, str], window: Window, screenshot_path: Path, crop_path: Path) -> dict[str, Any]:
    try:
        run(
            [
                "convert",
                str(screenshot_path),
                "-crop",
                f"{window.width}x{window.height}+{window.x}+{window.y}",
                "-colorspace",
                "Gray",
                "-resize",
                "160%",
                str(crop_path),
            ],
            env=env,
        )
    except Exception as exc:
        return {"locked": False, "ocr_text": "", "lock_crop": str(crop_path), "error": str(exc)[:500]}
    proc = run(["tesseract", str(crop_path), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "6"], env=env, check=False)
    ocr_text = proc.stdout.strip()
    observed = normalize_title(ocr_text)
    locked = any(
        marker in observed
        for marker in (
            "weixinforlinuxislocked",
            "unlockonphone",
            "手机微信聊天列表顶部的状态栏解锁",
            "微信聊天列表顶部的状态栏解锁",
        )
    )
    return {
        "locked": locked,
        "ocr_text": ocr_text,
        "lock_crop": str(crop_path),
    }


def title_crop_regions(window: Window) -> list[dict[str, int | str]]:
    """OCR regions that avoid the left chat list but cover main and popup chats."""
    regions: list[dict[str, int | str]] = []
    if window.width < 760:
        regions.append(
            {
                "label": "popup_header",
                "left": window.x + 18,
                "top": window.y + 35,
                "width": max(260, window.width - 70),
                "height": 78,
            }
        )
    else:
        regions.append(
            {
                "label": "main_right_header",
                "left": window.x + 360,
                "top": window.y + 32,
                "width": max(300, window.width - 390),
                "height": 78,
            }
        )
        regions.append(
            {
                "label": "main_right_header_high",
                "left": window.x + 360,
                "top": window.y,
                "width": max(300, window.width - 390),
                "height": 96,
            }
        )
    return regions


def window_to_dict(window: Window) -> dict[str, int | str]:
    return {
        "wid": window.wid,
        "x": window.x,
        "y": window.y,
        "width": window.width,
        "height": window.height,
    }


def window_from_guard(guard: dict[str, Any]) -> Window | None:
    raw = guard.get("compose_window")
    if not isinstance(raw, dict):
        return None
    try:
        return Window(
            wid=str(raw.get("wid") or ""),
            x=int(raw["x"]),
            y=int(raw["y"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def title_window_candidates(env: dict[str, str], main: Window) -> list[Window]:
    candidates: list[Window] = []
    focused = focused_window(env)
    if focused and focused.width >= 480 and focused.height >= 420:
        candidates.append(focused)
    candidates.append(main)
    seen: set[str] = set()
    unique: list[Window] = []
    for candidate in candidates:
        if candidate.wid in seen:
            continue
        unique.append(candidate)
        seen.add(candidate.wid)
    return unique


def find_wechat_window(env: dict[str, str]) -> Window | None:
    ids = run(["xdotool", "search", "--onlyvisible", "--class", "wechat"], env=env, check=False).stdout.split()
    candidates: list[Window] = []
    for wid in ids:
        geom = run(["xdotool", "getwindowgeometry", "--shell", wid], env=env, check=False).stdout
        values: dict[str, int] = {}
        for line in geom.splitlines():
            if "=" not in line:
                continue
            key_name, raw = line.split("=", 1)
            try:
                values[key_name] = int(raw)
            except ValueError:
                pass
        if {"X", "Y", "WIDTH", "HEIGHT"} <= values.keys():
            candidates.append(Window(wid, values["X"], values["Y"], values["WIDTH"], values["HEIGHT"]))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.width * item.height)


def wait_for_main_wechat_window(
    env: dict[str, str],
    *,
    timeout: float = 15.0,
    minimum_width: int = 500,
    minimum_height: int = 500,
) -> Window | None:
    """Wait through the startup/splash window without weakening login guards."""

    deadline = time.monotonic() + max(0.0, timeout)
    latest: Window | None = None
    while True:
        candidate = find_wechat_window(env)
        if candidate is not None:
            latest = candidate
            if candidate.width >= minimum_width and candidate.height >= minimum_height:
                return candidate
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.5)


def focused_window(env: dict[str, str]) -> Window | None:
    geom = run(["xdotool", "getwindowfocus", "getwindowgeometry", "--shell"], env=env, check=False).stdout
    values: dict[str, int | str] = {}
    for line in geom.splitlines():
        if "=" not in line:
            continue
        key_name, raw = line.split("=", 1)
        if key_name == "WINDOW":
            values[key_name] = raw
            continue
        try:
            values[key_name] = int(raw)
        except ValueError:
            pass
    if {"WINDOW", "X", "Y", "WIDTH", "HEIGHT"} <= values.keys():
        return Window(
            str(values["WINDOW"]),
            int(values["X"]),
            int(values["Y"]),
            int(values["WIDTH"]),
            int(values["HEIGHT"]),
        )
    return None


def close_secondary_wechat_windows(env: dict[str, str], main: Window) -> None:
    ids = run(["xdotool", "search", "--onlyvisible", "--class", "wechat"], env=env, check=False).stdout.split()
    main_area = main.width * main.height
    for wid in ids:
        if wid == main.wid:
            continue
        geom = run(["xdotool", "getwindowgeometry", "--shell", wid], env=env, check=False).stdout
        values: dict[str, int] = {}
        for line in geom.splitlines():
            if "=" not in line:
                continue
            key_name, raw = line.split("=", 1)
            try:
                values[key_name] = int(raw)
            except ValueError:
                pass
        area = values.get("WIDTH", 0) * values.get("HEIGHT", 0)
        if 20_000 <= area < min(main_area, int(main_area * 0.25)):
            request_close(wid, display_name=env.get("DISPLAY", ":97"), protected_window_ids={main.wid})
    time.sleep(0.5)


def close_non_target_wechat_windows(env: dict[str, str], main: Window, target: TargetSpec) -> None:
    expected = [
        normalize_title(item)
        for item in (target.expected_title, *target.expected_title_aliases, target.name)
        if normalize_title(item)
    ]
    ids = run(["xdotool", "search", "--onlyvisible", "--class", "wechat"], env=env, check=False).stdout.split()
    main_area = main.width * main.height
    for wid in ids:
        if wid == main.wid:
            continue
        geom = run(["xdotool", "getwindowgeometry", "--shell", wid], env=env, check=False).stdout
        values: dict[str, int] = {}
        for line in geom.splitlines():
            if "=" not in line:
                continue
            key_name, raw = line.split("=", 1)
            try:
                values[key_name] = int(raw)
            except ValueError:
                pass
        area = values.get("WIDTH", 0) * values.get("HEIGHT", 0)
        if area < 20_000 or area > int(main_area * 0.95):
            continue
        title = run(["xdotool", "getwindowname", wid], env=env, check=False).stdout.strip()
        normalized_title = normalize_title(title)
        if normalized_title and any(item in normalized_title for item in expected):
            continue
        request_close(wid, display_name=env.get("DISPLAY", ":97"), protected_window_ids={main.wid})
    time.sleep(0.3)


def reset_wechat_send_surface(
    env: dict[str, str],
    main: Window,
    target: TargetSpec,
    pause: float,
) -> None:
    """Dismiss stale previews/transfers before clicking the guarded chat list."""
    close_non_target_wechat_windows(env, main, target)
    focus(env, main)
    for _ in range(2):
        try:
            key(env, "Escape")
        except RuntimeError:
            break
        time.sleep(max(0.1, min(pause, 0.3)))
    dismiss_internal_file_transfer_surface(env, main, target, pause)
    close_non_target_wechat_windows(env, main, target)
    focus(env, main)


def internal_file_transfer_surface_visible(env: dict[str, str], main: Window) -> bool:
    runtime = PRIVATE / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{time.time_ns()}"
    source = runtime / f"send-surface-{token}.png"
    crop = runtime / f"send-surface-{token}-top.png"
    try:
        screenshot(env, source)
        run(
            [
                "convert",
                str(source),
                "-crop",
                f"{main.width}x{min(160, main.height)}+{main.x}+{main.y}",
                "-resize",
                "180%",
                str(crop),
            ],
            env=env,
            check=False,
        )
        text = run(
            [
                "tesseract",
                str(crop),
                "stdout",
                "-l",
                "chi_sim+chi_tra+eng",
                "--psm",
                "11",
            ],
            env=env,
            check=False,
        ).stdout
        normalized = normalize_title(text)
        return "filetransfer" in normalized or (
            "filetr" in normalized and "nsfer" in normalized
        )
    finally:
        source.unlink(missing_ok=True)
        crop.unlink(missing_ok=True)


def dismiss_internal_file_transfer_surface(
    env: dict[str, str],
    main: Window,
    target: TargetSpec,
    pause: float,
) -> bool:
    target_titles = (
        target.expected_title,
        *target.expected_title_aliases,
        target.name,
    )
    if any("filetransfer" in normalize_title(title) for title in target_titles):
        return False
    if not internal_file_transfer_surface_visible(env, main):
        return False
    click(
        env,
        main.x + int(main.width * 0.744),
        main.y + min(32, max(24, int(main.height * 0.041))),
    )
    time.sleep(max(0.2, min(pause, 0.5)))
    return not internal_file_transfer_surface_visible(env, main)


def focus(env: dict[str, str], window: Window) -> None:
    run(["xdotool", "windowfocus", "--sync", window.wid], env=env, check=False)
    run(["xdotool", "windowraise", window.wid], env=env, check=False)
    time.sleep(0.2)


def click(env: dict[str, str], x: int, y: int) -> None:
    run(["xdotool", "mousemove", str(x), str(y), "click", "1"], env=env)


def double_click(env: dict[str, str], x: int, y: int) -> None:
    run(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", "2", "--delay", "80", "1"], env=env)


def key(env: dict[str, str], name: str) -> None:
    run(["xdotool", "key", name], env=env)


def hotkey(env: dict[str, str], name: str) -> None:
    run(["xdotool", "key", name], env=env)


def paste_text(env: dict[str, str], text: str) -> None:
    timeout = float(os.environ.get("WECHAT_CLIPBOARD_TIMEOUT", "6"))
    proc = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-loops", "1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(text)
    proc.stdin.close()
    time.sleep(0.2)
    run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], env=env)
    time.sleep(0.2)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
        raise RuntimeError(
            "WECHAT_CLIPBOARD_PASTE_TIMEOUT: WeChat did not consume the clipboard; "
            "the message was not pasted"
        )
    if proc.returncode not in (0, -15, None):
        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"xclip failed to set clipboard: {(stderr or stdout).strip()}")


def verify_composer_text(env: dict[str, str], expected: str) -> None:
    """Read the focused composer back through the clipboard before sending."""
    copied = read_composer_text(env)
    if normalize_composer_text(copied) != normalize_composer_text(expected):
        raise RuntimeError(
            "WECHAT_COMPOSE_VERIFY_FAILED: composed text does not match the intended message "
            f"(expected {len(expected)} chars, read back {len(copied)} chars)"
        )


def read_composer_text(env: dict[str, str]) -> str:
    # Ctrl+C on an empty/unresponsive composer leaves the old clipboard intact.
    # Own it with a fresh nonce first so the earlier pasted message cannot pass.
    marker = "labcanvas-clipboard-probe-" + uuid.uuid4().hex
    proc = subprocess.Popen(["xclip", "-selection", "clipboard", "-quiet"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, text=True, env=env)
    try:
        assert proc.stdin is not None
        proc.stdin.write(marker)
        proc.stdin.close()
        time.sleep(0.15)
        if run(["xclip", "-selection", "clipboard", "-o"], env=env).stdout != marker:
            raise RuntimeError("WECHAT_COMPOSE_VERIFY_FAILED: clipboard verification probe unavailable")
        run(["xdotool", "key", "--clearmodifiers", "ctrl+a"], env=env)
        run(["xdotool", "key", "--clearmodifiers", "ctrl+c"], env=env)
        time.sleep(0.2)
        copied = run(["xclip", "-selection", "clipboard", "-o"], env=env).stdout
        run(["xdotool", "key", "--clearmodifiers", "ctrl+End"], env=env)
        if copied == marker:
            raise RuntimeError("WECHAT_COMPOSE_VERIFY_FAILED: WeChat did not copy text from its composer")
        return copied
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)


def normalize_composer_text(value: str) -> str:
    normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", normalized)


def screenshot(env: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["import", "-window", "root", str(path)], env=env, check=False)


def same_screenshot(first: Path, second: Path) -> bool:
    if not first.exists() or not second.exists():
        return False
    if first.stat().st_size != second.stat().st_size:
        return False
    return hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()


def require_tools(*names: str) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise SystemExit(f"Missing required tool(s): {', '.join(missing)}")


def run(command: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    timeout = float(os.environ.get("WECHAT_GUI_COMMAND_TIMEOUT", "8"))
    try:
        proc = subprocess.run(command, env=env, capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError as exc:
        if not check:
            return subprocess.CompletedProcess(command, 127, "", str(exc))
        raise RuntimeError(f"{command[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        message = f"{' '.join(command)} timed out after {exc.timeout} seconds"
        if not check:
            return subprocess.CompletedProcess(command, 124, exc.stdout or "", message)
        raise RuntimeError(message) from exc
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed: {proc.stderr.strip()}")
    return proc


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value).strip("-")
    return cleaned[:48] or "target"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(124)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
