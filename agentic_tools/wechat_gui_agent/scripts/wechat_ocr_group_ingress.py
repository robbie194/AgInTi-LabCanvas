#!/usr/bin/env python3
"""Read allowlisted personal-WeChat groups through the visible GUI only.

This is an opt-in text ingress. It never opens WeChat databases, reads process
memory, or injects code. Each configured group is opened through the existing
exact-title GUI guard, its visible chat area is OCR'd, and only a configured
mention or binding code is forwarded to the existing personal-group Cloud
adapter. Replies and artifacts use that adapter's guarded GUI sender.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

import wechat_gui_send as gui
from file_lock import fcntl_compat as fcntl
from wechat_personal_group_cloud import (
    PersonalGroupCloudError,
    personal_group_cloud_enabled,
    run_once as run_cloud_once,
)


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve()
DEFAULT_STATE = PRIVATE / "wechat-ocr-group-ingress.state.json"
GUI_LOCK = PRIVATE / "wechat_gui_send.lock"
CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-HJ-NP-Z2-9]{8}(?![A-Za-z0-9])", re.IGNORECASE)
GROUP_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
GROUP_TITLE_RE = re.compile(r"^OverTree-G-[A-HJ-NP-Z2-9]{6}$")
_REMOTE_GROUP_CACHE: dict[str, Any] = {"fetched_at": 0.0, "configs": [], "error": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configs", default=os.environ.get("WECHAT_OCR_GROUP_CONFIGS", ""))
    parser.add_argument("--display", default=os.environ.get("WECHAT_DISPLAY", ":97"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("WECHAT_OCR_GROUP_INTERVAL", "3")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--require-unread-badge", action="store_true", help="Only open rows with a visible red unread marker.")
    parser.add_argument("--send", action="store_true", help="Required for live Cloud submission and guarded replies.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.once and not args.loop:
        args.once = True
    if not args.send:
        raise SystemExit("OCR personal-group ingress requires --send; it is never a dry-run transport.")
    configs = discover_configs(args.configs)
    if not configs and not remote_group_sync_enabled():
        raise SystemExit("No OCR group configs found. Set WECHAT_OCR_GROUP_CONFIGS first.")
    while True:
        results = scan_once(args, configs)
        if args.json or args.once or any(not item.get("ok") for item in results):
            print(json.dumps({"checked_at": datetime.now().isoformat(timespec="seconds"), "results": results}, ensure_ascii=False), flush=True)
        if not args.loop:
            return 0 if all(item.get("ok") for item in results) else 1
        time.sleep(max(0.8, args.interval))


def discover_configs(raw: str) -> list[Path]:
    paths: list[Path] = []
    for item in str(raw or "").split(","):
        value = item.strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        paths.append(path.resolve())
    return paths


def scan_once(args: argparse.Namespace, configs: list[Path]) -> list[dict[str, Any]]:
    results = []
    local_group_ids: set[str] = set()
    for path in configs:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            local_group_ids.add(str(config.get("chatroom_id") or ""))
            results.append(scan_group(args, config, path))
        except Exception as exc:
            results.append({"ok": False, "config": str(path), "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
    if remote_group_sync_enabled():
        try:
            registered = registered_group_configs()
        except Exception as exc:
            results.append({"ok": False, "registry": "cloud", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            registered = []
        for config in registered:
            group_id = str(config.get("chatroom_id") or "")
            if group_id in local_group_ids:
                continue
            path = PRIVATE / f"{group_id}-registered-group.json"
            try:
                results.append(scan_group(args, config, path))
            except Exception as exc:
                results.append({"ok": False, "config": str(path), "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
    return results


def remote_group_sync_enabled() -> bool:
    return str(os.environ.get("WECHAT_OCR_GROUP_REMOTE_SYNC") or "false").lower() == "true"


def registered_group_configs(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch signed, Cloud-registered group titles and map them to stable IDs."""
    if not remote_group_sync_enabled():
        return []
    now = time.monotonic()
    ttl = max(10.0, float(os.environ.get("WECHAT_OCR_GROUP_REGISTRY_REFRESH_SECONDS", "30")))
    if not force_refresh and now - float(_REMOTE_GROUP_CACHE.get("fetched_at") or 0) < ttl:
        if _REMOTE_GROUP_CACHE.get("error"):
            raise PersonalGroupCloudError(str(_REMOTE_GROUP_CACHE["error"]))
        return list(_REMOTE_GROUP_CACHE.get("configs") or [])
    try:
        from wechat_personal_group_cloud import build_registry_client

        response = build_registry_client().list_registered_groups()
        raw_groups = response.get("groups")
        if response.get("ok") is not True or not isinstance(raw_groups, list):
            raise PersonalGroupCloudError("OverTree Cloud returned an invalid registered-group list.")
        configs: list[dict[str, Any]] = []
        allowed_ids = {
            value.strip() for value in str(os.environ.get("WECHAT_PERSONAL_GROUP_ALLOWLIST") or "").split(",")
            if value.strip()
        }
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("groupId") or "").lower()
            title = str(group.get("title") or "").strip()
            if not GROUP_ID_RE.fullmatch(group_id) or not GROUP_TITLE_RE.fullmatch(title):
                continue
            allowed_ids.add(group_id)
            triggers = [value.strip() for value in str(
                os.environ.get("WECHAT_OCR_GROUP_TRIGGER_PREFIXES") or "@OverTree,＠OverTree"
            ).split(",") if value.strip()]
            if not triggers:
                triggers = ["@OverTree", "＠OverTree"]
            configs.append({
                "enabled": True,
                "ingress": "ocr_gui",
                "chat_name": title,
                "chatroom_id": group_id,
                "display": os.environ.get("WECHAT_DISPLAY", ":97"),
                "state_path": str(PRIVATE / f"{group_id}-ocr.state.json"),
                "send_target": {
                    "name": title,
                    "query": title,
                    "expected_title": title,
                    "allow_search": True,
                },
                "personal_group_cloud": {
                    "enabled": True,
                    "trigger_prefixes": triggers,
                    "artifact_base_url": os.environ.get("WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL", "https://cloud.overtree.top"),
                },
                "chat_region": {"left": 390, "top": 100, "right": 0, "bottom": 170},
                "ocr_psm": 11,
                "group_registration_id": group_id,
            })
        os.environ["WECHAT_PERSONAL_GROUP_ALLOWLIST"] = ",".join(sorted(allowed_ids))
        _REMOTE_GROUP_CACHE.update({"fetched_at": now, "configs": configs, "error": ""})
        return list(configs)
    except Exception as exc:
        error = str(exc)[:500] or type(exc).__name__
        _REMOTE_GROUP_CACHE.update({"fetched_at": now, "configs": [], "error": error})
        raise


def scan_group(args: argparse.Namespace, config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    chat_name = str(config.get("chat_name") or "").strip()
    if not chat_name:
        raise PersonalGroupCloudError("OCR group config requires chat_name.")
    if config.get("enabled") is False or not personal_group_cloud_enabled(config):
        return {"ok": True, "chat": chat_name, "skipped": "disabled"}
    state_path = Path(config.get("state_path") or (PRIVATE / f"{slug(chat_name)}-ocr.state.json")).expanduser().resolve()
    ensure_private_path(state_path)
    state = load_state(state_path)
    config["_runtime_state_path"] = str(state_path)
    target = target_from_config(config, chat_name)
    extracted: str | None = None
    opened = False
    with tempfile.TemporaryDirectory(prefix="wechat-ocr-") as temp:
        evidence = Path(temp)
        env = os.environ.copy()
        env["DISPLAY"] = str(config.get("display") or args.display)
        env["WECHAT_DISPLAY"] = env["DISPLAY"]
        gui.require_tools("xdotool", "import", "convert", "tesseract")
        window = gui.wait_for_main_wechat_window(env, timeout=10)
        if not window or window.width < 500 or window.height < 500:
            raise RuntimeError("WECHAT_ENTRY_REQUIRED: the Agent WeChat window is not ready")
        if args.require_unread_badge and not has_unread_badge(env, window, target, evidence):
            return {"ok": True, "chat": chat_name, "skipped": "no_unread_badge"}
        # Serialize navigation with every other GUI sender. Cloud polling is
        # outside this lock; the adapter reopens the exact guarded target.
        with GUI_LOCK.open("a", encoding="utf-8") as lock:
            acquire_lock(lock, 15)
            original_title_regions = gui.title_crop_regions
            gui.title_crop_regions = ocr_title_crop_regions
            try:
                opened_result = gui.open_target(
                    env, window, target, pause=float(config.get("open_pause_seconds") or 0.6),
                    out_dir=evidence, shot_prefix="ocr-open", skip_title_guard=False,
                    prefer_current=True, allow_search=False, fail_closed_after_visible_match=True,
                )
            finally:
                gui.title_crop_regions = original_title_regions
            if not opened_result.get("ok"):
                return {
                    "ok": False, "chat": chat_name, "status": "title_guard_failed",
                    "method": opened_result.get("method"), "window_title": opened_result.get("window_title"),
                }
            opened = True
            extracted = ocr_triggered_text(env, window, config, evidence)
        rows = []
        if extracted:
            row, fingerprint = build_ocr_row(config, state, extracted)
            if not ocr_fingerprint_is_recent(state, fingerprint, extracted):
                rows.append(row)
        result = run_cloud_once(
            config, state, rows, send=True,
            eligible=lambda _row: True,
            inbound=lambda _row: True,
            visible_text=lambda row: str(row.get("content") or ""),
            send_text=send_gui_message,
            send_file=send_gui_file,
        )
    if extracted and rows:
        state.setdefault("ocr_seen_fingerprints", []).append(fingerprint)
        state["ocr_seen_fingerprints"] = state["ocr_seen_fingerprints"][-200:]
        times = state.setdefault("ocr_fingerprint_times", {})
        if isinstance(times, dict):
            times[fingerprint] = int(time.time())
            state["ocr_fingerprint_times"] = {
                str(key): int(value) for key, value in list(times.items())[-200:]
                if str(key)
            }
        state["ocr_last_text"] = extracted[:500]
        state["ocr_last_at"] = int(time.time())
    save_state(state_path, result.get("state") or state)
    return {
        "ok": True,
        "chat": chat_name,
        "opened": opened,
        "text_detected": bool(extracted),
        "submitted": bool(result.get("task_enqueued")),
        "response_sent": bool(result.get("response_sent")),
        "metrics": result.get("metrics") or {},
    }


def target_from_config(config: dict[str, Any], chat_name: str) -> gui.TargetSpec:
    raw = config.get("send_target") if isinstance(config.get("send_target"), dict) else {}
    return gui.target_from_raw({
        "name": raw.get("name") or chat_name,
        "query": raw.get("query") or chat_name,
        "expected_title": raw.get("expected_title") or chat_name,
        "expected_title_aliases": raw.get("expected_title_aliases") or [],
        "allow_title_guard_fallback": bool(raw.get("allow_title_guard_fallback", False)),
        "allow_search": bool(raw.get("allow_search", False)),
    })


def ocr_title_crop_regions(window: gui.Window) -> list[dict[str, int | str]]:
    """Crop the group header on narrow WeChat windows without toolbar icons.

    The shared sender uses a fixed 360px offset intended for wider windows.
    On the current WeChat 4.x window (about 916px wide), the conversation pane
    begins earlier, so that crop cuts the first characters from the title.
    Keep this adjustment local to the opt-in OCR ingress.
    """
    if window.width < 760:
        return [{
            "label": "ocr_group_popup_header",
            "left": window.x + 18,
            "top": window.y + 35,
            "width": max(260, window.width - 70),
            "height": 78,
        }]
    left_offset = min(360, max(260, int(window.width * 0.34)))
    width = max(260, min(420, int(window.width * 0.42)))
    return [
        {
            "label": "ocr_group_header",
            "left": window.x + left_offset,
            "top": window.y + 32,
            "width": width,
            "height": 78,
        },
        {
            "label": "ocr_group_header_high",
            "left": window.x + left_offset,
            "top": window.y,
            "width": width,
            "height": 96,
        },
    ]


def ocr_triggered_text(env: dict[str, str], window: gui.Window, config: dict[str, Any], evidence: Path) -> str:
    region = chat_region(window, config)
    source = evidence / "screen.png"
    crop = evidence / "chat.png"
    gui.screenshot(env, source)
    run_command(["convert", str(source), "-crop", f"{region['width']}x{region['height']}+{region['left']}+{region['top']}", "-resize", "160%", str(crop)], env)
    tsv = run_command(["tesseract", str(crop), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", str(config.get("ocr_psm") or 11), "tsv"], env, check=False).stdout
    lines = parse_ocr_lines(tsv)
    prefixes = [str(item).strip() for item in (config.get("personal_group_cloud") or {}).get("trigger_prefixes", []) if str(item).strip()]
    # A screenshot contains the full visible history. Use the newest OCR line
    # addressed to the Agent; concatenating every line after the first match
    # would resubmit older messages whenever the chat remains open.
    # Choose the newest addressed event regardless of whether it is a mention
    # or a binding code. Otherwise an old visible mention could mask a newer
    # code line and prevent an intentional session switch.
    for line in reversed(lines):
        text = line["text"]
        if any(prefix.casefold() in text.casefold() for prefix in prefixes):
            return normalize_message_text(text)[:30_000]
        match = CODE_RE.fullmatch(text.strip())
        if match:
            return match.group(0).upper()
    return ""


def parse_ocr_lines(tsv: str) -> list[dict[str, Any]]:
    import csv
    from io import StringIO

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for raw in csv.DictReader(StringIO(tsv), delimiter="\t"):
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        try:
            left, top = int(raw.get("left") or 0), int(raw.get("top") or 0)
        except ValueError:
            continue
        key = (str(raw.get("block_num") or ""), str(raw.get("par_num") or ""), str(raw.get("line_num") or ""))
        groups.setdefault(key, []).append({"text": text, "left": left, "top": top})
    lines = []
    for words in groups.values():
        words.sort(key=lambda item: item["left"])
        lines.append({"text": " ".join(item["text"] for item in words), "top": min(item["top"] for item in words)})
    return sorted(lines, key=lambda item: item["top"])


def chat_region(window: gui.Window, config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("chat_region") if isinstance(config.get("chat_region"), dict) else {}
    left = max(0, int(raw.get("left", 390)))
    top = max(0, int(raw.get("top", 100)))
    right = max(0, int(raw.get("right", 0)))
    bottom = max(0, int(raw.get("bottom", 170)))
    return {"left": window.x + left, "top": window.y + top, "width": max(100, window.width - left - right), "height": max(100, window.height - top - bottom)}


def has_unread_badge(env: dict[str, str], window: gui.Window, target: gui.TargetSpec, evidence: Path) -> bool:
    """Best-effort red-marker check; title matching remains the hard guard."""
    region = gui.chat_list_crop_region(window)
    if not region:
        return False
    source = evidence / "badge-screen.png"
    crop = evidence / "badge-list.png"
    gui.screenshot(env, source)
    run_command(["convert", str(source), "-crop", f"{region['width']}x{region['height']}+{region['left']}+{region['top']}", str(crop)], env)
    # The row is already selected/opened, so this is intentionally only a
    # conservative advisory gate. Pixel matching avoids another OCR pass.
    try:
        from PIL import Image
        image = Image.open(crop).convert("RGB")
        red = sum(1 for r, g, b in image.getdata() if r > 170 and r > g * 1.35 and r > b * 1.35 and g < 150)
        return red >= int(os.environ.get("WECHAT_OCR_MIN_RED_PIXELS", "8"))
    except Exception:
        return False


def build_ocr_row(config: dict[str, Any], state: dict[str, Any], text: str) -> tuple[dict[str, Any], str]:
    normalized = normalize_message_text(text)
    # OCR has no durable WeChat message ID. Keep the fingerprint stable across
    # scans; a short state-backed cooldown below prevents the same visible line
    # from being submitted on every polling pass while still allowing a later
    # identical question to be handled.
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    next_id = int(state.get("ocr_next_local_id") or 0) + 1
    state["ocr_next_local_id"] = next_id
    now = int(time.time())
    row = {
        "_message_db": f"ocr://{slug(str(config.get('chat_name') or 'wechat-group'))}",
        "server_id": f"ocr-{fingerprint}", "local_id": next_id, "local_type": 1,
        "create_time": now, "sender": "ocr-group-member", "sender_display": "群成员",
        "content": normalized,
    }
    return row, fingerprint


def recent_fingerprints(state: dict[str, Any]) -> set[str]:
    return {str(item) for item in state.get("ocr_seen_fingerprints", []) if str(item)}


def ocr_fingerprint_is_recent(state: dict[str, Any], fingerprint: str, _text: str) -> bool:
    if fingerprint not in recent_fingerprints(state):
        return False
    try:
        times = state.get("ocr_fingerprint_times") or {}
        last_at = float(times.get(fingerprint) or state.get("ocr_last_at") or 0)
    except (TypeError, ValueError):
        return False
    cooldown = max(30.0, float(os.environ.get("WECHAT_OCR_DEDUP_SECONDS", "900")))
    return time.time() - last_at < cooldown


def send_gui_message(config: dict[str, Any], message: str) -> str:
    from wechat_direct_chatops import send_gui_message as sender
    send_config = dict(config)
    send_config["_visual_only_send"] = str(config.get("ingress") or "") == "ocr_gui"
    return sender(send_config, message)


def send_gui_file(config: dict[str, Any], path: Path) -> str:
    from wechat_direct_chatops import send_gui_file as sender
    return sender(config, path)


def normalize_message_text(text: str) -> str:
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split()).strip()


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")[:80] or "wechat-group"


def ensure_private_path(path: Path) -> None:
    try:
        path.relative_to(PRIVATE)
    except ValueError as exc:
        raise PersonalGroupCloudError("OCR group state must remain inside the private LabCanvas directory.") from exc
    path.parent.mkdir(parents=True, exist_ok=True)


def acquire_lock(handle: Any, timeout: float) -> None:
    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError("WECHAT_SEND_BUSY: GUI lane remained busy")
            time.sleep(0.1)


def run_command(command: list[str], env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, env=env, capture_output=True, text=True, check=False, timeout=15)
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "command failed").strip()[:500])
    return proc


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read OCR state: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
