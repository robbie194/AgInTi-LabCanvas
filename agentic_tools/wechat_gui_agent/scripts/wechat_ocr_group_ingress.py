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


GUI_TITLE_CROP_REGIONS = gui.title_crop_regions
ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve()
DEFAULT_STATE = PRIVATE / "wechat-ocr-group-ingress.state.json"
PASSIVE_STATE_PREFIX = "wechat-ocr-passive"
GUI_LOCK = PRIVATE / "wechat_gui_send.lock"
CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-HJ-NP-Z2-9]{8}(?![A-Za-z0-9])", re.IGNORECASE)
GROUP_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
LEGACY_LOCAL_GROUP_ID_RE = re.compile(r"^ocr-title-[a-f0-9]{24}$", re.IGNORECASE)
# Cloud now allocates compact four-character titles. Keep the six-character
# format accepted so groups created by an older Cloud release continue to
# work. The registered value is always the stable prefix; a customer may add
# a readable suffix in WeChat (for example ``OverTree-ABCD-论文讨论``).
GROUP_TITLE_RE = re.compile(r"^(?:OverTree-[A-HJ-NP-Z2-9]{4}|OverTree-G-[A-HJ-NP-Z2-9]{6})$")
COPY_MENU_LABELS = {"复制", "複製", "copy", "copymessage", "copytext"}
CONTEXT_MENU_LABELS = {
    "转发", "轉發", "forward", "收藏", "favorite", "多选", "多選",
    "引用", "quote", "删除", "刪除", "delete", "撤回", "recall",
    "搜一搜", "search", "翻译", "翻譯", "translate", "编辑", "編輯", "edit",
}
OCR_DEBUG_ROW_RE = re.compile(
    r"^\s*[2-5]\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+-?(?:\d+(?:\.\d+)?|\d*\.\d+)\s+.+$"
)
PASSIVE_SIGNATURE_SIZE = (96, 64)
COPY_MENU_SCALE = 2.5
_REMOTE_GROUP_CACHE: dict[str, Any] = {"fetched_at": 0.0, "configs": [], "error": "", "local_group_key": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configs", default=os.environ.get("WECHAT_OCR_GROUP_CONFIGS", ""))
    parser.add_argument("--display", default=os.environ.get("WECHAT_DISPLAY", ":97"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("WECHAT_OCR_GROUP_INTERVAL", "3")))
    parser.add_argument(
        "--active-rescan-seconds",
        type=float,
        default=float(os.environ.get("WECHAT_OCR_GROUP_ACTIVE_RESCAN_SECONDS", "180")),
        help="Run an allowlisted chat scan after this many seconds even when the screen signature is unchanged.",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--require-unread-badge",
        action="store_true",
        default=False,
        help="Optional strict mode: require the target row's unread badge. Normally read exact visible/current groups too.",
    )
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
    last_heartbeat = 0.0
    while True:
        results = scan_once(args, configs)
        heartbeat_due = time.monotonic() - last_heartbeat >= 60
        if args.json or args.once or heartbeat_due or any(
            not item.get("ok") or item.get("submitted") or item.get("response_sent")
            for item in results
        ):
            print(json.dumps({
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "registry_observed_titles": _REMOTE_GROUP_CACHE.get("observed_count", 0),
                "registry_admitted_groups": len(_REMOTE_GROUP_CACHE.get("configs") or []),
                "results": results,
            }, ensure_ascii=False), flush=True)
            last_heartbeat = time.monotonic()
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
    results: list[dict[str, Any]] = []
    local_configs: dict[str, tuple[dict[str, Any], Path]] = {}
    for path in configs:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            group_id = str(config.get("chatroom_id") or "").lower()
            if group_id:
                local_configs[group_id] = (config, path)
        except Exception as exc:
            results.append({"ok": False, "config": str(path), "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
    prepared: list[tuple[dict[str, Any], Path]] = []
    if remote_group_sync_enabled():
        try:
            registered = registered_group_configs(local_configs=local_configs)
        except Exception as exc:
            results.append({"ok": False, "registry": "cloud", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            # A stale local config must never bring a revoked, expired, or
            # otherwise unregistered group back into the active scan set.
            registered = None
        if registered is None:
            return results
        if _REMOTE_GROUP_CACHE.get("discovery_error"):
            results.append({"ok": False, "registry": "visible_group_discovery", "error": _REMOTE_GROUP_CACHE["discovery_error"]})
        # Cloud returns registrations oldest first.  Scan the newest groups
        # first so a newly generated connection code is handled promptly even
        # when older groups are temporarily slow to open in the GUI. Cloud is
        # also the sole admission source: local files only preserve their
        # existing private state path and cannot re-enable stale registrations.
        for config in reversed(registered):
            group_id = str(config.get("chatroom_id") or "").lower()
            local = local_configs.get(group_id)
            if local:
                local_path = str(local[0].get("state_path") or "").strip()
                if local_path:
                    config["state_path"] = local_path
            path = PRIVATE / f"{group_id}-registered-group.json"
            prepared.append((config, path))
    else:
        prepared.extend(local_configs.values())

    if not prepared:
        return results

    by_display: dict[str, list[tuple[dict[str, Any], Path]]] = {}
    for config, path in prepared:
        display = str(config.get("display") or args.display)
        by_display.setdefault(display, []).append((config, path))
    for display, display_configs in by_display.items():
        results.extend(scan_display_groups(args, display, display_configs))
    return results


def scan_display_groups(
    args: argparse.Namespace,
    display: str,
    prepared: list[tuple[dict[str, Any], Path]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    monitor_path = passive_state_path(display)
    ensure_private_path(monitor_path)
    monitor_state = load_state(monitor_path)
    group_key = configured_group_key(prepared)
    try:
        observation = observe_passive_screen(display)
    except Exception as exc:
        observation = None
        results.append({
            "ok": False,
            "display": display,
            "monitor": "passive_screen",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        })
    now = time.time()
    active_scan = False
    scan_reason = "window_unavailable"
    if observation:
        active_scan, scan_reason = active_scan_decision(
            monitor_state,
            observation["signature"],
            group_key,
            now=now,
            rescan_seconds=max(30.0, min(3600.0, float(args.active_rescan_seconds))),
        )

    for config, path in prepared:
        try:
            result = scan_group(
                args,
                config,
                path,
                scan_gui=active_scan,
                seed_only=scan_reason in {"initial_baseline", "allowlist_changed"},
            )
            if not active_scan and observation:
                result["scan_skipped"] = "screen_unchanged"
            elif active_scan:
                result["scan_reason"] = scan_reason
            results.append(result)
        except Exception as exc:
            results.append({"ok": False, "config": str(path), "error": f"{type(exc).__name__}: {str(exc)[:500]}"})

    if observation:
        # Active group navigation changes the visible screen itself. Store a
        # post-scan baseline so those deliberate GUI operations do not trigger
        # another full pass on the next interval. The bounded rescan below
        # remains the recovery path for an event that races this baseline.
        if active_scan:
            try:
                final_observation = observe_passive_screen(display)
            except Exception:
                final_observation = None
            monitor_state["screen_signature"] = (
                final_observation["signature"] if final_observation else observation["signature"]
            )
            monitor_state["last_active_scan_at"] = now
            monitor_state["last_active_scan_reason"] = scan_reason
        else:
            monitor_state["screen_signature"] = observation["signature"]
        monitor_state["group_key"] = group_key
        monitor_state["last_observed_at"] = int(now)
        save_state(monitor_path, monitor_state)
    return results


def passive_state_path(display: str) -> Path:
    key = hashlib.sha256(str(display).encode("utf-8")).hexdigest()[:12]
    return PRIVATE / f"{PASSIVE_STATE_PREFIX}-{key}.json"


def configured_group_key(prepared: list[tuple[dict[str, Any], Path]]) -> str:
    identities = sorted(
        f"{str(config.get('chatroom_id') or '')}|{str(config.get('chat_name') or '')}"
        for config, _path in prepared
    )
    return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()


def active_scan_decision(
    state: dict[str, Any],
    signature: str,
    group_key: str,
    *,
    now: float,
    rescan_seconds: float,
) -> tuple[bool, str]:
    previous = str(state.get("screen_signature") or "")
    if not previous:
        return True, "initial_baseline"
    if str(state.get("group_key") or "") != group_key:
        return True, "allowlist_changed"
    if previous != signature:
        return True, "screen_changed"
    try:
        last_scan = float(state.get("last_active_scan_at") or 0)
    except (TypeError, ValueError):
        last_scan = 0
    if last_scan <= 0 or now - last_scan >= max(30.0, rescan_seconds):
        return True, "safety_rescan_due"
    return False, "screen_unchanged"


def observe_passive_screen(display: str) -> dict[str, Any] | None:
    """Take a low-cost local screenshot sample without OCR or input events."""
    env = os.environ.copy()
    env["DISPLAY"] = str(display)
    env["WECHAT_DISPLAY"] = str(display)
    window = gui.wait_for_main_wechat_window(env, timeout=1)
    if not window or window.width < 500 or window.height < 500:
        return None
    with GUI_LOCK.open("a", encoding="utf-8") as lock:
        acquire_lock(lock, 15)
        with tempfile.TemporaryDirectory(prefix="wechat-passive-") as temporary:
            screenshot = Path(temporary) / "screen.png"
            gui.screenshot(env, screenshot)
            signature = passive_screen_signature(env, screenshot, window)
    return {"signature": signature, "window": window}


def passive_screen_signature(env: dict[str, str], screenshot: Path, window: gui.Window) -> str:
    """Hash a quantized crop containing chat rows and the live conversation tail."""
    left = max(0, window.x + int(window.width * 0.05))
    top = max(0, window.y + int(window.height * 0.08))
    width = max(1, int(window.width * 0.81))
    height = max(1, int(window.height * 0.66))
    geometry = f"{width}x{height}+{left}+{top}"
    command = [
        "convert", str(screenshot), "-crop", geometry, "+repage",
        "-alpha", "off", "-colorspace", "Gray",
        "-resize", f"{PASSIVE_SIGNATURE_SIZE[0]}x{PASSIVE_SIGNATURE_SIZE[1]}!",
        "-depth", "8", "gray:-",
    ]
    try:
        result = subprocess.run(command, env=env, capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not compute the passive WeChat screen signature") from exc
    expected = PASSIVE_SIGNATURE_SIZE[0] * PASSIVE_SIGNATURE_SIZE[1]
    if result.returncode != 0 or len(result.stdout) != expected:
        raise RuntimeError("ImageMagick returned an invalid passive WeChat screen sample")
    return quantized_pixel_signature(result.stdout)


def quantized_pixel_signature(pixels: bytes) -> str:
    expected = PASSIVE_SIGNATURE_SIZE[0] * PASSIVE_SIGNATURE_SIZE[1]
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} grayscale pixels, received {len(pixels)}")
    quantized = bytes((value // 16) * 16 for value in pixels)
    return hashlib.sha256(quantized).hexdigest()


def remote_group_sync_enabled() -> bool:
    return str(os.environ.get("WECHAT_OCR_GROUP_REMOTE_SYNC") or "false").lower() == "true"


def registered_group_configs(
    *,
    force_refresh: bool = False,
    local_configs: dict[str, tuple[dict[str, Any], Path]] | None = None,
    visible_titles: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch signed, Cloud-registered group titles and map them to stable IDs."""
    if not remote_group_sync_enabled():
        return []
    local_configs = local_configs or {}
    local_group_key = ",".join(sorted(local_configs))
    now = time.monotonic()
    ttl = max(10.0, float(os.environ.get("WECHAT_OCR_GROUP_REGISTRY_REFRESH_SECONDS", "30")))
    if (
        not force_refresh
        and _REMOTE_GROUP_CACHE.get("local_group_key") == local_group_key
        and now - float(_REMOTE_GROUP_CACHE.get("fetched_at") or 0) < ttl
    ):
        if _REMOTE_GROUP_CACHE.get("error"):
            raise PersonalGroupCloudError(str(_REMOTE_GROUP_CACHE["error"]))
        return list(_REMOTE_GROUP_CACHE.get("configs") or [])
    try:
        from wechat_personal_group_cloud import build_registry_client

        # Read status is not identity: the currently open chat is already read.
        # Discover generated titles only on the visible desktop; Cloud still
        # requires a live code and exact registered prefix before admission.
        # No local UUID exists yet for a new group: Cloud must resolve its
        # registered prefix before we can create its isolated configuration.
        discovery_error = ""
        try:
            pending_titles = visible_titles if visible_titles is not None else visible_registered_group_titles(
                os.environ.get("WECHAT_DISPLAY", ":97")
            )
        except Exception as exc:
            # Discovery failure must not interrupt already-bound task/outbox
            # reconciliation. Admit no newly observed group on this refresh.
            pending_titles = []
            discovery_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        response = build_registry_client().list_registered_groups(
            sorted(local_configs), pending_titles=pending_titles,
        )
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
            binding_status = str(group.get("bindingStatus") or "unbound").strip().lower()
            local = local_configs.get(group_id)
            if binding_status == "pending" and not local and title not in pending_titles:
                # A pending group needs either an operator's exact local entry
                # or its own visible title, plus Cloud's live code check.
                continue
            local_title = str(local[0].get("chat_name") or "").strip() if local else ""
            if not title and binding_status == "bound":
                # A legacy binding may have no registered slot/title. Cloud
                # confirms the exact local UUID; retain the operator's
                # already-configured title for its visual GUI guard.
                title = local_title
            if not group_id_is_supported(group_id):
                continue
            # Cloud includes bound groups and explicitly requested pending
            # groups with a live first-binding code. Dormant registrations and
            # revoked/unpaired groups never enter the local scan list.
            if binding_status not in {"bound", "pending"}:
                continue
            if not GROUP_TITLE_RE.fullmatch(title) and not (local and title == local_title and binding_status == "bound"):
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
                    "expected_title_aliases": [f"{title}-"],
                    # Inbound scanning never uses search. Keep the registered
                    # binding bit on the config so the outbound sender can
                    # locate a bound target when a completed Cloud reply is due.
                    "allow_search": binding_status == "bound",
                },
                "personal_group_cloud": {
                    "enabled": True,
                    "trigger_prefixes": triggers,
                    "artifact_base_url": os.environ.get("WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL", "https://cloud.overtree.top"),
                },
                "chat_region": {"left": 390, "top": 100, "right": 0, "bottom": 170},
                "ocr_psm": 11,
                "group_registration_id": group_id,
                "binding_status": binding_status,
            })
        os.environ["WECHAT_PERSONAL_GROUP_ALLOWLIST"] = ",".join(sorted(allowed_ids))
        _REMOTE_GROUP_CACHE.update({
            "fetched_at": now,
            "configs": configs,
            "error": "",
            "local_group_key": local_group_key,
            "discovery_error": discovery_error,
            "observed_count": len(pending_titles),
        })
        return list(configs)
    except Exception as exc:
        error = str(exc)[:500] or type(exc).__name__
        _REMOTE_GROUP_CACHE.update({
            "fetched_at": now,
            "configs": [],
            "error": error,
            "local_group_key": local_group_key,
        })
        raise


def scan_group(
    args: argparse.Namespace,
    config: dict[str, Any],
    config_path: Path,
    *,
    scan_gui: bool = True,
    seed_only: bool = False,
) -> dict[str, Any]:
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
    messages: list[dict[str, Any]] = []
    observed: list[tuple[str, str]] = []
    title_failure: dict[str, Any] | None = None
    extraction_method = "not_scanned"
    opened = False
    rows: list[dict[str, Any]] = []
    fingerprint = ""
    seeded = False
    if scan_gui:
        with tempfile.TemporaryDirectory(prefix="wechat-ocr-") as temp:
            evidence = Path(temp)
            env = os.environ.copy()
            env["DISPLAY"] = str(config.get("display") or args.display)
            env["WECHAT_DISPLAY"] = env["DISPLAY"]
            gui.require_tools("xdotool", "import", "convert", "tesseract", "xclip")
            window = gui.wait_for_main_wechat_window(env, timeout=10)
            if not window or window.width < 500 or window.height < 500:
                raise RuntimeError("WECHAT_ENTRY_REQUIRED: the Agent WeChat window is not ready")
            # A badge is only an activity hint. Opening a group clears it, so
            # requiring it would suppress every subsequent message in that chat.
            # open_target still requires the exact current/visible title and
            # cannot search globally or fall back to an unverified conversation.
            unread_gate_passed = has_unread_badge(env, window, target, evidence)
            if unread_gate_passed or not getattr(args, "require_unread_badge", False):
                # Serialize navigation, bubble Copy, and clipboard access with every
                # other GUI sender on this isolated WeChat desktop.
                with GUI_LOCK.open("a", encoding="utf-8") as lock:
                    acquire_lock(lock, 15)
                    original_title_regions = gui.title_crop_regions
                    gui.title_crop_regions = ocr_title_crop_regions
                    try:
                        opened_result = gui.open_target(
                            env, window, target, pause=float(config.get("open_pause_seconds") or 0.6),
                            out_dir=evidence, shot_prefix="ocr-open", skip_title_guard=False,
                            # Search fallback is intentionally disabled for GUI
                            # ingress so idle groups never populate the search box.
                            prefer_current=True, allow_search=False,
                            fail_closed_after_visible_match=True,
                        )
                    finally:
                        gui.title_crop_regions = original_title_regions
                    if not opened_result.get("ok"):
                        title_failure = {
                            "status": "title_guard_failed",
                            "method": opened_result.get("method"), "window_title": opened_result.get("window_title"),
                        }
                        extraction_method = "title_guard_failed"
                    else:
                        opened = True
                        config["_ocr_bubble_kind_cache"] = state.get("ocr_bubble_kind_cache", {})
                        extraction = ocr_triggered_message(
                            env, window, config, evidence,
                            prefer_binding_code=not bool(state.get("personal_group_cloud_binding_id")),
                            all_messages=True,
                        )
                        state["ocr_bubble_kind_cache"] = config.get("_ocr_bubble_kind_cache", {})
                        messages = extraction.get("messages", [extraction])
                        extraction_method = str(extraction.get("method") or "none")
            else:
                extraction_method = "no_unread_badge"
        for message in messages:
            text = str(message.get("text") or "")
            if not text:
                continue
            row, fingerprint = build_ocr_row(config, state, text)
            row["local_type"] = int(message.get("local_type") or 1)
            if fingerprint not in {item[0] for item in observed} and not ocr_fingerprint_is_recent(state, fingerprint, text):
                if (seed_only and not unread_gate_passed
                    and config.get("binding_status") != "pending"
                    and message.get("method") != "ocr_binding_code"):
                    # A process restart or newly added group must establish a
                    # visible-message baseline without replaying old questions.
                    seeded = True
                else:
                    rows.append(row)
                observed.append((fingerprint, text))
                extracted = text
    result = run_cloud_once(
        config, state, rows, send=True,
        eligible=lambda _row: True,
        inbound=lambda _row: True,
        visible_text=lambda row: str(row.get("content") or ""),
        send_text=send_gui_message,
        send_file=send_gui_file,
    )
    state = result.get("state") or state
    if observed:
        state.setdefault("ocr_seen_fingerprints", []).extend(item[0] for item in observed)
        state["ocr_seen_fingerprints"] = state["ocr_seen_fingerprints"][-200:]
        times = state.setdefault("ocr_fingerprint_times", {})
        if isinstance(times, dict):
            times.update({item[0]: int(time.time()) for item in observed})
            state["ocr_fingerprint_times"] = {
                str(key): int(value) for key, value in list(times.items())[-200:]
                if str(key)
            }
        state["ocr_last_text"] = extracted[:500]
        state["ocr_last_at"] = int(time.time())
    state["ocr_last_poll_at"] = int(time.time())
    if scan_gui:
        state["ocr_last_scan_at"] = int(time.time())
        state["ocr_last_scan_method"] = extraction_method
        state["ocr_last_detected_count"] = len(messages)
        state["ocr_last_queued_count"] = len(rows)
    save_state(state_path, state)
    metrics = result.get("metrics") or {}
    delivery_error = str(metrics.get("send_error") or "")
    return {
        "ok": not bool(delivery_error or title_failure),
        "chat": chat_name,
        "opened": opened,
        "text_detected": bool(messages),
        "extraction_method": extraction_method,
        "seeded": seeded,
        "detected_count": len(messages),
        "queued_count": len(rows),
        "submitted": bool(result.get("task_enqueued")),
        "response_sent": bool(result.get("response_sent")),
        "metrics": metrics,
        **({"error": delivery_error} if delivery_error else {}),
        **(title_failure or {}),
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


def group_id_is_supported(group_id: str) -> bool:
    return bool(GROUP_ID_RE.fullmatch(group_id) or LEGACY_LOCAL_GROUP_ID_RE.fullmatch(group_id))


def ocr_title_crop_regions(window: gui.Window) -> list[dict[str, int | str]]:
    """Use the same header geometry for ingress identity and outbound delivery."""
    return GUI_TITLE_CROP_REGIONS(window)


def ocr_triggered_text(env: dict[str, str], window: gui.Window, config: dict[str, Any], evidence: Path) -> str:
    return str(ocr_triggered_message(env, window, config, evidence).get("text") or "")


def incoming_bubble_candidates(source: Path, window: gui.Window) -> list[dict[str, Any]]:
    """Locate incoming avatars, never outgoing bubbles, in the guarded chat.

    The compact Linux layout shares its pane offset with the title guard.
    Inspecting a context menu is read-only; no media is opened or downloaded.
    """
    if not source.is_file():
        return []
    from PIL import Image
    with Image.open(source) as image:
        image = image.convert("RGB")
        pane = int(ocr_title_crop_regions(window)[0]["left"])
        left, right = pane + 16, pane + 42
        top, bottom = window.y + 90, window.y + window.height - 170
        if left < 0 or right >= image.width or bottom >= image.height:
            return []
        runs = []
        start = None
        # Avatars occupy a narrow, fixed column separate from message text.
        # Require a roughly square filled patch, excluding labels/timestamps.
        for y in range(top, bottom):
            background = image.getpixel((pane + 7, y))
            filled = sum(max(abs(a - b) for a, b in zip(image.getpixel((x, y)), background)) > 30
                         for x in range(left, right)) >= 18
            if filled and start is None:
                start = y
            if not filled and start is not None:
                if 24 <= y - start <= 46:
                    runs.append((start, y))
                start = None
        candidates = []
        for y0, y1 in runs[-12:]:
            # The first body rows and avatar are stable when the viewport moves;
            # hash pixels only, without retaining the media or forwarding OCR.
            patch = image.crop((left, y0, min(window.x + window.width - 75, pane + 280),
                                min(bottom, y0 + 44)))
            fingerprint = hashlib.sha256(patch.tobytes()).hexdigest()[:32]
            candidates.append({"x": pane + 80, "y": y0 + 28, "top": y0, "fingerprint": fingerprint})
        return candidates


def media_kind_from_menu(lines: list[dict[str, Any]], x: int, y: int) -> str:
    """Require corroborated native menu actions, not words in chat/image OCR."""
    labels = []
    for line in lines:
        cx = (int(line["left"]) + int(line["right"])) // 2
        cy = (int(line["top"]) + int(line["bottom"])) // 2
        label = re.sub(r"[\s:：.·()（）]", "", str(line.get("text") or "")).casefold()
        if abs(cx - x) <= 300 and -340 <= cy - y <= 420:
            labels.append((label, cx, cy))
    media_actions = {
        "image": {"saveas", "saveimageas", "savephoto", "extracttext", "copyimage", "另存为", "另存為", "提取文字", "识别图中文字", "複製圖片", "复制图片"},
        "voice": {"transcribe", "converttotext", "voicetotext", "转文字", "轉文字", "语音转文字", "語音轉文字", "转为文字", "轉為文字", "播放语音", "播放語音"},
        "file": {"open", "openfile", "openfolder", "showinfolder", "打开", "打開", "打开文件夹", "打開檔案", "另存为文件"},
        "other": {"addtostickers", "添加到表情", "添加表情", "viewcontact", "查看名片", "viewlocation", "查看位置"},
    }
    for kind, actions in media_actions.items():
        for label, cx, cy in labels:
            if label in actions and any(other in CONTEXT_MENU_LABELS and abs(ox-cx) <= 180
                                       and 8 <= abs(oy-cy) <= 260 for other, ox, oy in labels):
                return kind
    # Voice, contact cards, stickers and other native non-text bubbles may
    # expose only Forward/Delete/etc. A verified text menu always has Copy.
    if not any(label in COPY_MENU_LABELS or label in {"copyc", "复制c", "複製c"}
               for label, _x, _y in labels):
        for label, cx, cy in labels:
            if label in CONTEXT_MENU_LABELS and any(
                other != label and other in CONTEXT_MENU_LABELS and abs(ox - cx) <= 180
                and 8 <= abs(oy - cy) <= 260 for other, ox, oy in labels
            ):
                return "other"
    return ""


def inspect_media_bubble(env: dict[str, str], window: gui.Window, candidate: dict[str, Any], evidence: Path) -> str:
    x, y = int(candidate["x"]), int(candidate["y"])
    try:
        gui.run(["xdotool", "mousemove", str(x), str(y), "click", "3"], env=env)
        time.sleep(0.2)
        source = evidence / "media-menu.png"
        crop = evidence / "media-menu-crop.png"
        gui.screenshot(env, source)
        # Native popup menus may extend below/outside the main chat window.
        # Cropping to the window hid Save As on the real Linux image menu.
        from PIL import Image
        with Image.open(source) as screen:
            screen_width, screen_height = screen.size
        left, top = max(0, x - 300), max(0, y - 340)
        width = min(700, screen_width - left)
        height = min(900, screen_height - top)
        run_command(["convert", str(source), "-crop", f"{width}x{height}+{left}+{top}",
                     "+repage", "-resize", "250%", str(crop)], env)
        tsv = run_command(["tesseract", str(crop), "stdout", "-l", "chi_sim+chi_tra+eng",
                           "--psm", "11", "tsv"], env, check=False).stdout
        lines = parse_ocr_lines(tsv, scale=2.5, offset=(left, top))
        kind = media_kind_from_menu(lines, x, y)
        if kind == "other":
            # Do not reject ordinary text merely because one OCR pass missed
            # its Copy action. Confirm generic non-text menus independently.
            second = run_command(["tesseract", str(crop), "stdout", "-l", "chi_sim+chi_tra+eng",
                                  "--psm", "6", "tsv"], env, check=False).stdout
            if media_kind_from_menu(parse_ocr_lines(second, scale=2.5, offset=(left, top)), x, y) != "other":
                return ""
        return kind or ("text" if find_copy_menu_action(lines, x, y) else "")
    finally:
        dismiss_bubble_menu(env, window)


def visible_media_messages(env: dict[str, str], window: gui.Window, source: Path,
                           evidence: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    cache = config.setdefault("_ocr_bubble_kind_cache", {})
    candidates = incoming_bubble_candidates(source, window)
    messages = []
    for candidate in candidates:
        fingerprint = candidate["fingerprint"]
        entry = cache.get(fingerprint)
        if not isinstance(entry, dict) or (not entry.get("kind") and time.time() - float(entry.get("at") or 0) >= 30):
            entry = {"kind": inspect_media_bubble(env, window, candidate, evidence), "at": time.time()}
            cache[fingerprint] = entry
        kind = entry["kind"]
        if kind and kind != "text":
            messages.append({"text": f"[不支持的微信消息:{kind}:{fingerprint}]",
                             "method": "native_media_menu", "local_type": 34 if kind == "voice" else 49,
                             "top": candidate["top"]})
    config["_ocr_bubble_kind_cache"] = dict(list(cache.items())[-200:])
    return messages


def ocr_triggered_message(
    env: dict[str, str],
    window: gui.Window,
    config: dict[str, Any],
    evidence: Path,
    *,
    prefer_binding_code: bool = False,
    all_messages: bool = False,
) -> dict[str, Any]:
    region = chat_region(window, config)
    source = evidence / "screen.png"
    crop = evidence / "chat.png"
    gui.screenshot(env, source)
    run_command(["convert", str(source), "-crop", f"{region['width']}x{region['height']}+{region['left']}+{region['top']}", "-resize", "160%", str(crop)], env)
    tsv = run_command(["tesseract", str(crop), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", str(config.get("ocr_psm") or 11), "tsv"], env, check=False).stdout
    lines = parse_ocr_lines(tsv)
    media_messages = visible_media_messages(env, window, source, evidence, config)
    # Media bodies can contain arbitrary printed @mentions or eight-character
    # strings. Never interpret their OCR as text questions or binding codes.
    media_tops = {int(item["top"]) for item in media_messages}
    candidates = incoming_bubble_candidates(source, window)
    boundaries = sorted(int(item["top"]) for item in candidates)
    def is_media_line(line: dict[str, Any]) -> bool:
        screen_y = region["top"] + int(line["top"]) / 1.6
        preceding = [top for top in boundaries if top <= screen_y]
        return bool(preceding and max(preceding) in media_tops)
    lines = [line for line in lines if not is_media_line(line)]
    prefixes = [str(item).strip() for item in (config.get("personal_group_cloud") or {}).get("trigger_prefixes", []) if str(item).strip()]
    # Keep bubbles separate and chronological. Picking only the newest question
    # silently loses earlier messages arriving between polling passes. Native
    # Copy supplies each full bubble; the state-backed fingerprint suppresses
    # history across repeated scans and process restarts.
    binding_code = ""
    binding_line: dict[str, Any] | None = None
    for line in reversed(lines):
        compact = "".join(str(line["text"]).split())
        match = CODE_RE.fullmatch(compact)
        # A small glyph can disappear (J/I) or become punctuation. Such a line
        # may locate a candidate bubble, but only its exact native Copy text may
        # establish a valid code. Never repair/guess the code sent to Cloud.
        native_candidate = bool(
            re.fullmatch(r"[A-Za-z0-9|!(){}\[\]._-]{6,12}", compact)
            and any(character.isdigit() for character in compact)
        )
        if match or native_candidate:
            binding_code = match.group(0).upper() if match else ""
            binding_line = line
            # OCR often splits or misreads tiny code glyphs. Use it to locate
            # the bubble, then prefer its exact native text just like questions.
            point = ocr_line_screen_point(line, region, scale=1.6)
            if point and str(os.environ.get("WECHAT_OCR_GROUP_NATIVE_COPY", "true")).lower() != "false":
                copied = copy_text_bubble(env, window, *point, evidence=evidence)
                if not copied:
                    text_x = region["left"] + round((line["left"] + line["right"]) / 3.2)
                    text_y = region["top"] + round((line["top"] + line["bottom"]) / 3.2)
                    copied = copy_code_word(env, window, text_x, text_y)
                if copied:
                    native_match = CODE_RE.fullmatch(copied.strip())
                    if not native_match:
                        binding_code = ""
                        binding_line = None
                        continue
                    binding_code = native_match.group(0).upper()
            if not binding_code:
                binding_line = None
                continue
            break
    if prefer_binding_code and binding_code and not all_messages:
        return {"text": binding_code, "method": "ocr_binding_code"}
    messages: list[dict[str, Any]] = list(media_messages)
    seen_text: set[str] = set()
    selected_lines = lines if all_messages else list(reversed(lines))
    if all_messages and prefer_binding_code and binding_code:
        # Binding must precede the questions in the same visible batch. Earlier
        # history belongs to the previous/unbound session, not this connection.
        code_index = lines.index(binding_line)
        selected_lines = lines[code_index:]
    for line in selected_lines:
        text = line["text"]
        if any(prefix.casefold() in text.casefold() for prefix in prefixes):
            copied = ""
            point = ocr_line_screen_point(line, region, scale=1.6)
            copy_enabled = str(os.environ.get("WECHAT_OCR_GROUP_NATIVE_COPY", "true")).lower() != "false"
            if point and copy_enabled:
                copied = copy_text_bubble(env, window, *point, evidence=evidence)
            if copied and any(prefix.casefold() in copied.casefold() for prefix in prefixes):
                normalized = normalize_message_text(copied)
                if looks_like_ocr_debug_text(normalized):
                    # A failed native copy can return a prior OCR/Tesseract
                    # dump from the clipboard. Never submit diagnostic rows as
                    # a user's question; require the next clean message.
                    if not all_messages:
                        return {"text": "", "method": "invalid_native_copy"}
                    continue
                message = {"text": normalized[:30_000], "method": "native_copy"}
            else:
                message = {"text": normalize_message_text(text)[:30_000], "method": "ocr_fallback"}
        else:
            if line is not binding_line:
                continue
            message = {"text": binding_code, "method": "ocr_binding_code"}
        if not all_messages:
            return message
        if message["text"] not in seen_text:
            seen_text.add(message["text"])
            messages.append(message)
    if all_messages:
        return {"text": "", "method": "visible_batch", "messages": messages}
    return {"text": "", "method": "none"}


def parse_ocr_lines(
    tsv: str,
    *,
    scale: float = 1.0,
    offset: tuple[int, int] = (0, 0),
) -> list[dict[str, Any]]:
    import csv
    from io import StringIO

    if scale <= 0:
        raise ValueError("OCR coordinate scale must be positive")
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    # Tesseract TSV has literal text, not CSV quoted fields. An unmatched quote
    # in a rename notice otherwise swallows subsequent rows into one text cell
    # (the numeric TSV dumps previously forwarded into customer conversations).
    for raw in csv.DictReader(StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        try:
            left, top = int(raw.get("left") or 0), int(raw.get("top") or 0)
        except ValueError:
            continue
        key = (str(raw.get("block_num") or ""), str(raw.get("par_num") or ""), str(raw.get("line_num") or ""))
        try:
            width = int(raw.get("width") or 0)
            height = int(raw.get("height") or 0)
        except ValueError:
            width, height = 0, 0
        screen_left = offset[0] + round(left / scale)
        screen_top = offset[1] + round(top / scale)
        screen_right = offset[0] + round((left + width) / scale)
        screen_bottom = offset[1] + round((top + height) / scale)
        groups.setdefault(key, []).append({
            "text": text,
            "left": screen_left,
            "top": screen_top,
            "right": screen_right,
            "bottom": screen_bottom,
        })
    lines = []
    for words in groups.values():
        words.sort(key=lambda item: item["left"])
        lines.append({
            "text": " ".join(item["text"] for item in words),
            "left": min(item["left"] for item in words),
            "top": min(item["top"] for item in words),
            "right": max(item["right"] for item in words),
            "bottom": max(item["bottom"] for item in words),
        })
    return sorted(lines, key=lambda item: item["top"])


def ocr_line_screen_point(
    line: dict[str, Any],
    region: dict[str, int],
    *,
    scale: float,
) -> tuple[int, int] | None:
    try:
        left = int(line["left"])
        top = int(line["top"])
        right = int(line["right"])
        bottom = int(line["bottom"])
        if right <= left or bottom <= top or scale <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return (
        # Current Linux WeChat does not consistently open the bubble menu on
        # selectable text. Its padded background immediately after the text
        # opens the full-message menu, including for multiline messages.
        region["left"] + round(right / scale) + 5,
        region["top"] + round(bottom / scale) + 1,
    )


def copy_text_bubble(
    env: dict[str, str],
    window: gui.Window,
    x: int,
    y: int,
    *,
    evidence: Path,
) -> str:
    """Use the visible WeChat bubble's native Copy menu; OCR only locates it."""
    sentinel = f"__OVERTREE_WECHAT_COPY_{os.getpid()}_{time.time_ns()}__"
    owner: subprocess.Popen[str] | None = None
    menu_open = False
    try:
        owner = start_clipboard_probe(env, sentinel)
        gui.run(["xdotool", "mousemove", str(x), str(y)], env=env)
        time.sleep(0.15)
        gui.run(["xdotool", "click", "3"], env=env)
        menu_open = True
        time.sleep(0.20)
        menu_screenshot = evidence / "copy-menu.png"
        gui.screenshot(env, menu_screenshot)
        display_size = gui.run(["xdotool", "getdisplaygeometry"], env=env).stdout.split()
        if len(display_size) != 2:
            return ""
        screen_width, screen_height = (int(value) for value in display_size)
        crop_left = max(0, x - 300)
        crop_top = max(0, y - 220)
        crop_width = min(700, screen_width - crop_left)
        crop_height = min(600, screen_height - crop_top)
        if crop_width < 180 or crop_height < 140:
            return ""
        menu_crop = evidence / "copy-menu-crop.png"
        geometry = f"{crop_width}x{crop_height}+{crop_left}+{crop_top}"
        run_command(
            ["convert", str(menu_screenshot), "-crop", geometry, "+repage", "-resize", f"{COPY_MENU_SCALE * 100:g}%", str(menu_crop)],
            env,
        )
        tsv = run_command(
            ["tesseract", str(menu_crop), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "11", "tsv"],
            env,
            check=False,
        ).stdout
        menu_lines = parse_ocr_lines(tsv, scale=COPY_MENU_SCALE, offset=(crop_left, crop_top))
        action = find_copy_menu_action(menu_lines, x, y)
        if action is None:
            return ""
        gui.click(env, action[0], action[1])
        menu_open = False
        copied = wait_for_clipboard_text(env, sentinel, timeout=1.5)
        if not copied or copied.strip() == sentinel:
            return ""
        return copied[:30_000]
    except Exception:
        return ""
    finally:
        # Escape in the main Linux client can open its logout confirmation
        # when no menu actually appeared. A blank chat-margin click only
        # dismisses the menu/selection and never invokes a session action.
        if menu_open:
            dismiss_bubble_menu(env, window)
        stop_clipboard_probe(owner)


def copy_code_word(env: dict[str, str], window: gui.Window, x: int, y: int) -> str:
    """Copy a standalone code using native word selection if its menu fails.

    Only used for OCR-located standalone code candidates. Do not use selected
    words for questions: those require the complete bubble, including newlines.
    """
    sentinel = f"__OVERTREE_CODE_COPY_{os.getpid()}_{time.time_ns()}__"
    owner = None
    try:
        owner = start_clipboard_probe(env, sentinel)
        gui.double_click(env, x, y)
        time.sleep(0.15)
        gui.run(["xdotool", "key", "--clearmodifiers", "ctrl+c"], env=env)
        copied = wait_for_clipboard_text(env, sentinel, timeout=1.5).strip()
        return copied if CODE_RE.fullmatch(copied) else ""
    except Exception:
        return ""
    finally:
        dismiss_bubble_menu(env, window)
        stop_clipboard_probe(owner)


def dismiss_bubble_menu(env: dict[str, str], window: gui.Window) -> None:
    gui.run([
        "xdotool", "mousemove", str(window.x + window.width - 30),
        str(window.y + window.height - 195), "click", "1",
    ], env=env, check=False)


def start_clipboard_probe(env: dict[str, str], sentinel: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-quiet"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(sentinel)
        process.stdin.close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if read_x_clipboard(env) == sentinel:
                return process
            if process.poll() is not None:
                break
            time.sleep(0.05)
    except Exception:
        stop_clipboard_probe(process)
        raise
    stop_clipboard_probe(process)
    raise RuntimeError("WECHAT_COPY_CLIPBOARD_UNAVAILABLE: sentinel did not reach X clipboard")


def stop_clipboard_probe(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def read_x_clipboard(env: dict[str, str]) -> str:
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-o"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    return result.stdout if result.returncode == 0 else ""


def wait_for_clipboard_text(env: dict[str, str], sentinel: str, *, timeout: float) -> str:
    """Wait briefly for the native menu action to replace our fresh clipboard marker."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        copied = read_x_clipboard(env)
        if copied and copied.strip() != sentinel:
            return copied
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.05)


def find_copy_menu_action(
    lines: list[dict[str, Any]],
    anchor_x: int,
    anchor_y: int,
) -> tuple[int, int] | None:
    normalized_lines: list[tuple[str, int, int, int, int]] = []
    for line in lines:
        text = re.sub(r"[\s:：.·()（）\[\]【】]", "", str(line.get("text") or "")).casefold()
        try:
            left, top = int(line["left"]), int(line["top"])
            right, bottom = int(line["right"]), int(line["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        normalized_lines.append((text, left, top, right, bottom))

    candidates: list[tuple[int, int, int]] = []
    for text, left, top, right, bottom in normalized_lines:
        if text not in COPY_MENU_LABELS and text not in {"copyc", "复制c", "複製c"}:
            continue
        center_x, center_y = (left + right) // 2, (top + bottom) // 2
        dx, dy = center_x - anchor_x, center_y - anchor_y
        # A native context menu opens beside the right-click point. Keep the
        # search bounded so a matching word in nearby chat history cannot be
        # mistaken for the menu command.
        if abs(dx) > 300 or dy < -340 or dy > 420:
            continue
        # Require one neighboring, exact menu-action label as corroboration.
        # If OCR cannot read enough of the menu, use the normal OCR fallback
        # instead of clicking an ambiguous occurrence in the chat body.
        corroborated = False
        for other_text, other_left, other_top, other_right, other_bottom in normalized_lines:
            if other_text not in CONTEXT_MENU_LABELS:
                continue
            other_x = (other_left + other_right) // 2
            other_y = (other_top + other_bottom) // 2
            if abs(other_x - center_x) <= 180 and 8 <= abs(other_y - center_y) <= 260:
                corroborated = True
                break
        if not corroborated:
            continue
        candidates.append((dx * dx + dy * dy, center_x, center_y))
    if not candidates:
        return None
    _, x, y = min(candidates)
    return x, y


def chat_region(window: gui.Window, config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("chat_region") if isinstance(config.get("chat_region"), dict) else {}
    left = max(0, int(raw.get("left", 390)))
    top = max(0, int(raw.get("top", 100)))
    right = max(0, int(raw.get("right", 0)))
    bottom = max(0, int(raw.get("bottom", 170)))
    return {"left": window.x + left, "top": window.y + top, "width": max(100, window.width - left - right), "height": max(100, window.height - top - bottom)}


def visible_unread_group_titles(display: str) -> list[str]:
    """Read generated prefixes in visible unread rows, without UI navigation."""
    env = os.environ.copy()
    env["DISPLAY"] = str(display)
    env["WECHAT_DISPLAY"] = str(display)
    window = gui.wait_for_main_wechat_window(env, timeout=1)
    if not window or window.width < 760 or window.height < 500:
        return []
    with GUI_LOCK.open("a", encoding="utf-8") as lock:
        acquire_lock(lock, 15)
        with tempfile.TemporaryDirectory(prefix="wechat-discovery-") as temporary:
            snapshot = unread_chat_list_snapshot(env, window, Path(temporary))
            if snapshot is None:
                return []
            return unread_registered_titles_from_snapshot(*snapshot)


def visible_registered_group_titles(display: str) -> list[str]:
    """Observe generated prefixes in visible rows/header, independent of read state.

    Discovery never opens a chat and never authorizes one: Cloud validates each
    observed prefix against its live first-binding registration before scanning.
    """
    env = os.environ.copy()
    env["DISPLAY"] = str(display)
    env["WECHAT_DISPLAY"] = str(display)
    window = gui.wait_for_main_wechat_window(env, timeout=1)
    if not window or window.width < 500 or window.height < 500:
        return []
    with GUI_LOCK.open("a", encoding="utf-8") as lock:
        acquire_lock(lock, 15)
        with tempfile.TemporaryDirectory(prefix="wechat-discovery-") as temporary:
            evidence = Path(temporary)
            titles: set[str] = set()
            if window.width >= 760:
                snapshot = unread_chat_list_snapshot(env, window, evidence)
                if snapshot:
                    titles.update(registered_titles_from_lines(parse_ocr_lines(snapshot[0])))
            # The active group may have scrolled out of the list, or be open in
            # its own native window. Observe only the header, never message text.
            source = evidence / "header-screen.png"
            gui.screenshot(env, source)
            for index, region in enumerate(ocr_title_crop_regions(window)):
                crop = evidence / f"header-{index}.png"
                geometry = f"{region['width']}x{region['height']}+{region['left']}+{region['top']}"
                run_command(["convert", str(source), "-crop", geometry, "+repage", str(crop)], env)
                tsv = run_command([
                    "tesseract", str(crop), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "11", "tsv",
                ], env, check=False).stdout
                titles.update(registered_titles_from_lines(parse_ocr_lines(tsv)))
            return sorted(titles)[:100]


def registered_titles_from_lines(lines: list[dict[str, Any]]) -> set[str]:
    alphabet = r"[A-HJ-NP-Z2-9]"
    pattern = re.compile(
        rf"(?:Over\s*Tree\s*-\s*G\s*-\s*(?:{alphabet}\s*){{5}}{alphabet}"
        rf"|Over\s*Tree\s*-\s*(?:{alphabet}\s*){{3}}{alphabet})(?=$|[^A-Za-z0-9])",
        re.IGNORECASE,
    )
    titles: set[str] = set()
    for line in lines:
        # Title identity starts at the row/header, not in an arbitrary preview
        # or quoted line containing a generated-looking identifier.
        match = pattern.match(str(line["text"]).strip())
        if match:
            value = "".join(match.group(0).split())
            titles.add("OverTree-" + value[len("OverTree-"):].upper())
    return titles


def unread_registered_titles_from_snapshot(tsv: str, image: Any) -> list[str]:
    # OCR may split the prefix into several words. Never repair ambiguous
    # letters/digits or accept a partial four-character code from a longer ID.
    pattern = re.compile(
        r"(?:OverTree-G-[A-HJ-NP-Z2-9]{6}|OverTree-[A-HJ-NP-Z2-9]{4})(?=$|[^A-Za-z0-9])",
        re.IGNORECASE,
    )
    candidates: set[str] = set()
    for line in parse_ocr_lines(tsv):
        compact = "".join(str(line["text"]).split())
        for match in pattern.finditer(compact):
            value = match.group(0)
            title = "OverTree-" + value[len("OverTree-"):].upper()
            target = target_from_config({"send_target": {
                "expected_title_aliases": [f"{title}-"],
            }}, title)
            if unread_badge_for_target(tsv, image, target):
                candidates.add(title)
    return sorted(candidates)[:100]


def unread_chat_list_snapshot(
    env: dict[str, str], window: gui.Window, evidence: Path,
) -> tuple[str, Any] | None:
    region = gui.chat_list_crop_region(window)
    if not region:
        return None
    from PIL import Image
    source = evidence / "badge-screen.png"
    crop = evidence / "badge-list.png"
    gui.screenshot(env, source)
    run_command(["convert", str(source), "-crop", f"{region['width']}x{region['height']}+{region['left']}+{region['top']}", str(crop)], env)
    tsv = run_command(
        ["tesseract", str(crop), "stdout", "-l", "chi_sim+chi_tra+eng", "--psm", "11", "tsv"],
        env, check=False,
    ).stdout
    with Image.open(crop) as source_image:
        image = source_image.convert("RGB")
    return tsv, image


def unread_badge_for_target(tsv: str, image: Any, target: gui.TargetSpec) -> bool:
    match = gui.visible_chat_list_match_from_tsv(tsv, target)
    if not match:
        return False
    row_y = int(float(match["center_y"]))
    y0, y1 = max(0, row_y - 22), min(image.height, row_y + 22)
    return y1 > y0 and red_badge_component_present(
        image.crop((0, y0, image.width, y1)),
        min_pixels=max(8, int(os.environ.get("WECHAT_OCR_MIN_RED_PIXELS", "12"))),
    )


def has_unread_badge(env: dict[str, str], window: gui.Window, target: gui.TargetSpec, evidence: Path) -> bool:
    """Require a fresh red marker in the exact configured chat-list row."""
    try:
        snapshot = unread_chat_list_snapshot(env, window, evidence)
        return snapshot is not None and unread_badge_for_target(*snapshot, target)
    except Exception:
        return False


def red_badge_component_present(image: Any, *, min_pixels: int) -> bool:
    """Detect one compact red badge; isolated red OCR/text pixels do not count."""
    width, height = image.size
    pixels = image.load()
    visited: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            if (x, y) in visited or not (r > 170 and r > g * 1.35 and r > b * 1.35 and g < 150):
                continue
            stack = [(x, y)]
            visited.add((x, y))
            count = 0
            min_x = max_x = x
            min_y = max_y = y
            while stack:
                px, py = stack.pop()
                count += 1
                min_x, max_x = min(min_x, px), max(max_x, px)
                min_y, max_y = min(min_y, py), max(max_y, py)
                # Bound growth early so a red theme accent cannot become a
                # badge merely by connecting a large part of the chat row.
                if count > 500 or max_x - min_x > 34 or max_y - min_y > 24:
                    break
                for ny in range(max(0, py - 1), min(height, py + 2)):
                    for nx in range(max(0, px - 1), min(width, px + 2)):
                        if (nx, ny) in visited:
                            continue
                        nr, ng, nb = pixels[nx, ny]
                        if nr > 170 and nr > ng * 1.35 and nr > nb * 1.35 and ng < 150:
                            visited.add((nx, ny))
                            stack.append((nx, ny))
            if count >= min_pixels and max_x - min_x <= 34 and max_y - min_y <= 24:
                return True
    return False


def build_ocr_row(config: dict[str, Any], state: dict[str, Any], text: str) -> tuple[dict[str, Any], str]:
    normalized = normalize_message_text(text)
    # OCR has no durable WeChat message ID. Keep the fingerprint stable across
    # scans; bounded persisted fingerprints prevent the same visible history
    # from being submitted again on every polling pass or process restart.
    identity_text = " ".join(normalized.split())
    fingerprint = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:32]
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
    # A still-visible old bubble does not become a new message after 15 minutes.
    # These content IDs already form the Cloud idempotency key; retain the last
    # 200 rather than repeatedly replaying them on every safety rescan.
    return fingerprint in recent_fingerprints(state)


def send_gui_message(config: dict[str, Any], message: str) -> str:
    from wechat_direct_chatops import send_gui_message as sender
    send_config = dict(config)
    send_config["_visual_only_send"] = str(config.get("ingress") or "") == "ocr_gui"
    # A queued Cloud answer is an active delivery, distinct from passive
    # inbound monitoring. Only a Cloud-registered, already-bound group gets a
    # narrowly scoped global-search allowance for that outbound send.
    target = send_config.get("send_target") if isinstance(send_config.get("send_target"), dict) else {}
    group_id = str(send_config.get("chatroom_id") or "").lower()
    registered_id = str(send_config.get("group_registration_id") or "").lower()
    title = str(send_config.get("chat_name") or "").strip()
    expected = str(target.get("expected_title") or "").strip()
    allowed = (
        str(send_config.get("binding_status") or "") == "bound"
        and group_id_is_supported(group_id)
        and registered_id == group_id
        and bool(GROUP_TITLE_RE.fullmatch(expected))
        and title == expected
    )
    if not allowed:
        return sender(send_config, message)
    env_key = "WECHAT_ENABLE_GLOBAL_ACCOUNT_SEARCH"
    previous = os.environ.get(env_key)
    os.environ[env_key] = "1"
    try:
        return sender(send_config, message)
    finally:
        if previous is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = previous


def send_gui_file(config: dict[str, Any], path: Path) -> str:
    from wechat_direct_chatops import send_gui_file as sender
    return sender(config, path)


def normalize_message_text(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def looks_like_ocr_debug_text(text: str) -> bool:
    """Detect Tesseract TSV rows accidentally copied into the chat payload."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return False
    debug_rows = sum(bool(OCR_DEBUG_ROW_RE.match(line)) for line in lines)
    return debug_rows >= 3 and debug_rows * 2 >= len(lines)


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
