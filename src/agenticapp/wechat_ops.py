from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = PACKAGE_ROOT / "agentic_tools" / "wechat_gui_agent"
SCRIPTS = TOOL_ROOT / "scripts"
PRIVATE = TOOL_ROOT / ".private"
DEFAULT_DIRECT_CONFIG = PRIVATE / "lazy-research-direct-chatops.local.json"
DEFAULT_CHAT_CONFIG = PRIVATE / "lazy-research-chatops.local.json"
DEFAULT_QUEUE = PRIVATE / "wechat_task_queue.jsonl"
DEFAULT_SEND_TARGETS = PRIVATE / "wechat_send_targets.local.json"
DEFAULT_DISPLAY = ":97"
DEFAULT_VNC_PORT = 5917
DEFAULT_NOVNC_PORT = 6107
DEFAULT_PREVIEW_PORT = 6108
TRANSPORT_HEALTH_GUARD = SCRIPTS / "wechat_transport_stall_guard.py"
TRANSPORT_HEALTH_SNAPSHOT = PACKAGE_ROOT / "output" / "transport-health" / "latest.json"
UNLOCK_WATCHDOG_STATE = PRIVATE / "wechat_desktop_unlock_watchdog.state.json"
WECHAT_CLIENT_KNOWN_STATES = {
    "detect_failed",
    "entry_required",
    "locked",
    "no_window",
    "unlocked",
}
WECHAT_CLIENT_UNAVAILABLE_STATES = {
    "detect_failed",
    "entry_required",
    "locked",
    "no_window",
}
CODEX_SESSION_KEY_RE = re.compile(r"^v2:[0-9a-z_.-]+-[0-9a-f]{12}:[0-9a-z_.-]+$")
QUEUE_ACTIVE_STATUSES = {
    "pending",
    "in_progress",
    "generation_waiting",
    "generation_poststage_pending",
    "publish_poststage_pending",
    "send_retrying",
}
QUEUE_DELIVERY_BLOCKED_STATUSES = {
    "send_deferred_artifact",
    "send_deferred_locked",
}
QUEUE_HUMAN_BLOCKED_STATUSES = {"waiting_confirmation"}
QUEUE_FAILED_STATUSES = {
    "worker_failed",
    "send_failed",
    "generation_stale_paused",
    "worker_abandoned",
}
QUEUE_TERMINAL_STATUSES = {
    "done",
    "send_expired",
    "expired_stale",
    "rejected",
    "ignored",
}
DEFAULT_QUEUE_ATTENTION_MAX_AGE_SECONDS = 24 * 60 * 60


def add_wechat_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("wechat", help="Control WeChat GUI/direct chatops automation.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON where supported.")
    nested = parser.add_subparsers(dest="wechat_command", required=True)

    status = nested.add_parser("status", help="Show desktop, tmux, and private config status.")
    status.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    status.set_defaults(func=cmd_status)

    health = nested.add_parser("health", help="Check direct monitor state, self-message guards, and DB catch-up.")
    health.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    health.add_argument(
        "--compact",
        action="store_true",
        help="Return a bounded agent-facing summary of schedules, queues, and phone intake lanes.",
    )
    health.set_defaults(func=cmd_health)

    doctor = nested.add_parser("doctor", help="Check local commands and WeChat tool files.")
    doctor.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    doctor.set_defaults(func=cmd_doctor)

    control = nested.add_parser("control-map", help="Show safe WeChat control surfaces, guardrails, and reference projects.")
    control.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    control.set_defaults(func=cmd_control_map)

    routines = nested.add_parser("routines", help="List reusable WeChat worker routines and stage contracts.")
    routines.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    routines.set_defaults(func=cmd_routines)

    selftest = nested.add_parser("selftest", help="Run focused automation self-tests for critical WeChat routines.")
    selftest.add_argument("--suite", choices=["publish-poststage", "transport-resume", "all"], default="publish-poststage")
    selftest.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    selftest.set_defaults(func=cmd_selftest)

    init_config = nested.add_parser("init-config", help="Write ignored private config templates.")
    init_config.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    init_config.add_argument("--chat", default="wechat-chat")
    init_config.add_argument("--display", default=DEFAULT_DISPLAY)
    init_config.add_argument("--chatroom-id", default="")
    init_config.add_argument("--message-table", default="")
    init_config.add_argument("--self-wxid", default="")
    init_config.add_argument("--agent-backend", choices=["aginti", "codex", "claude"], default="aginti", help="AI backend for direct monitor and worker tasks.")
    init_config.add_argument("--force", action="store_true")
    init_config.set_defaults(func=cmd_init_config)

    desktop = nested.add_parser("desktop", help="Start, inspect, or keep awake the isolated WeChat desktop.")
    desktop.add_argument("action", choices=["start", "status", "keep-awake"], nargs="?", default="status")
    desktop.add_argument("--interval", type=int, default=55, help="Keep-awake refresh interval in seconds.")
    desktop.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    desktop.set_defaults(func=cmd_desktop)

    unlock = nested.add_parser("unlock-watchdog", help="Keep official desktop WeChat unlocked through the owner's Android WeChat UI.")
    unlock.add_argument("action", choices=["once", "start", "stop", "restart", "status"], nargs="?", default="once")
    unlock.add_argument("--display", default=DEFAULT_DISPLAY)
    unlock.add_argument(
        "--serial",
        default=(
            os.environ.get("WECHAT_UNLOCK_ADB_SERIAL")
            or os.environ.get("ANDROID_SERIAL", "")
        ),
    )
    unlock.add_argument("--interval", type=float, default=20)
    unlock.add_argument("--flush-deferred", action="store_true", help="Flush one deferred WeChat outbox item after unlocking.")
    unlock.add_argument("--dry-run", action="store_true")
    unlock.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    unlock.set_defaults(func=cmd_unlock_watchdog)

    browser = nested.add_parser("browser-assist", help="Open a browser in the isolated virtual desktop for manual login/CAPTCHA/download help.")
    browser.add_argument("--url", default="about:blank")
    browser.add_argument("--display", default=DEFAULT_DISPLAY)
    browser.add_argument("--browser")
    browser.add_argument("--wait-seconds", type=float, default=0.0)
    browser.add_argument("--reuse-window", action="store_true")
    browser.add_argument("--capture", action="store_true")
    browser.add_argument("--wait-readable-seconds", type=float, default=0.0)
    browser.add_argument("--poll-seconds", type=float, default=3.0)
    browser.add_argument("--close-after", action="store_true")
    browser.add_argument("--output-dir", type=Path)
    browser.add_argument("--allow-mp-weixin", action="store_true")
    browser.add_argument("--dry-run", action="store_true")
    browser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    browser.set_defaults(func=cmd_browser_assist)

    monitor = nested.add_parser("monitor", help="Control the fast direct chat monitor tmux session.")
    monitor.add_argument("action", choices=["start", "stop", "restart", "status", "once"], nargs="?", default="status")
    monitor.add_argument("--config", type=Path, default=DEFAULT_DIRECT_CONFIG)
    monitor.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    monitor.add_argument("--send", action="store_true", help="Allow live replies for one-shot mode.")
    monitor.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    monitor.set_defaults(func=cmd_monitor)

    hold = nested.add_parser("hold", help="Control the tmux supervisor. Normal restart reloads workers without restarting WeChat.")
    hold.add_argument("action", choices=["start", "stop", "restart", "reload-workers", "restart-all", "status"], nargs="?", default="start")
    hold.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    hold.set_defaults(func=cmd_hold)

    stack = nested.add_parser("stack", help="Control the WeChat supervisor plus LabCanvas web control panel. Normal restart preserves WeChat GUI.")
    stack.add_argument("action", choices=["start", "stop", "restart", "restart-all", "status"], nargs="?", default="start")
    stack.add_argument("--web-port", type=int, default=19474)
    stack.add_argument("--web-session", default="labcanvas-web")
    stack.add_argument("--career-session", default="labcanvas-career-daily")
    stack.add_argument("--career-send-chat", default="lachlanchan")
    stack.add_argument("--career-morning-time", default="08:30")
    stack.add_argument("--no-career", action="store_true", help="Do not manage the daily career/self-analysis scheduler.")
    stack.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    stack.set_defaults(func=cmd_stack)

    send = nested.add_parser("send", help="Send a message to a guarded WeChat group/contact target.")
    send.add_argument("--config", type=Path, default=DEFAULT_CHAT_CONFIG)
    send.add_argument("--chat", default="", help="Chat key in the private send-target registry.")
    send.add_argument("--send-targets", type=Path, default=DEFAULT_SEND_TARGETS)
    send.add_argument("--target", default="", help="Explicit target name/query. Prefer private registry for normal use.")
    send.add_argument("--display", default="")
    send.add_argument("--message")
    send.add_argument("--file", type=Path)
    send.add_argument("--dry-run", action="store_true", help="Open/check target without pressing Enter.")
    send.add_argument("--allow-search", action="store_true", help="Allow WeChat search fallback for this send.")
    send.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    send.set_defaults(func=cmd_send)

    worker = nested.add_parser("worker", help="Enqueue or process slower backend tasks.")
    worker.add_argument(
        "action",
        choices=[
            "enqueue",
            "once",
            "loop",
            "repair-artifacts",
            "repair-result",
            "reprocess",
        ],
    )
    worker.add_argument("request", nargs="*", help="Task text for enqueue.")
    worker.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    worker.add_argument("--chat", default="wechat-chat")
    worker.add_argument("--send", action="store_true")
    worker.add_argument(
        "--artifact-recovery-only",
        action="store_true",
        help="Recover completed exact-task research artifacts without rerunning the agent.",
    )
    worker.set_defaults(func=cmd_worker)

    queue = nested.add_parser("queue", help="Inspect the private worker queue.")
    queue.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    queue.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    queue.add_argument("--limit", type=int, default=10)
    queue.set_defaults(func=cmd_queue)

    memory = nested.add_parser("memory", help="Initialize or summarize the private organized WeChat memory database.")
    memory.add_argument("action", choices=["init", "summary"], nargs="?", default="summary")
    memory.add_argument("--db", type=Path, default=PRIVATE / "wechat_memory.sqlite")
    memory.add_argument("--chat", default="")
    memory.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    memory.set_defaults(func=cmd_memory)

    career = nested.add_parser("career-agent", help="Run or supervise the daily writing/career/money strategy agent.")
    career.add_argument(
        "action",
        choices=["once", "organize", "catch-up", "start", "stop", "restart", "status"],
        nargs="?",
        default="once",
    )
    career.add_argument("--chat", action="append", default=[], help="Memory chat to include. Repeatable.")
    career.add_argument("--send-chat", default="lachlanchan")
    career.add_argument("--send", action="store_true")
    career.add_argument("--attach-report", action="store_true")
    career.add_argument("--organize-report", action="store_true")
    career.add_argument("--organize-chat", default="MEMO写作—外语—挣钱")
    career.add_argument("--force-organize", action="store_true")
    career.add_argument("--morning-time", default="08:30")
    career.add_argument("--session", default="labcanvas-career-daily")
    career.add_argument("--model", default=os.environ.get("WECHAT_CAREER_AGENT_MODEL", "gpt-5.6-sol"))
    career.add_argument("--reasoning-effort", default=os.environ.get("WECHAT_CAREER_AGENT_EFFORT", "xhigh"))
    career.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    career.set_defaults(func=cmd_career_agent)

    approve = nested.add_parser("approve", help="Approve a worker task that is waiting for confirmation.")
    approve.add_argument("task_id", nargs="?", help="Task id. Defaults to the newest waiting_confirmation task.")
    approve.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    approve.add_argument("--note", default="")
    approve.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    approve.set_defaults(func=cmd_approve)

    reject = nested.add_parser("reject", help="Reject/cancel a worker task that is waiting for confirmation.")
    reject.add_argument("task_id", nargs="?", help="Task id. Defaults to the newest waiting_confirmation task.")
    reject.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    reject.add_argument("--note", default="")
    reject.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    reject.set_defaults(func=cmd_reject)

    media = nested.add_parser("media-sync", help="Copy recent WeChat downloads/cache files into the private workspace.")
    media.add_argument("--chat", required=True)
    media.add_argument("--source", action="append", type=Path, default=[])
    media.add_argument("--auto-source", action="store_true", help="Auto-discover local xwechat_files media folders.")
    media.add_argument("--since-minutes", type=float, default=60)
    media.add_argument("--since-epoch", type=float)
    media.add_argument("--until-epoch", type=float)
    media.add_argument("--match-token", action="append", default=[])
    media.add_argument("--dry-run", action="store_true")
    media.add_argument("--summary-only", action="store_true")
    media.add_argument("--record-empty", action="store_true")
    media.set_defaults(func=cmd_media_sync)

    voice = nested.add_parser("voice-transcribe", help="Transcribe a WeChat voice message from decrypted media_0.db.")
    voice.add_argument("--config", type=Path, default=DEFAULT_DIRECT_CONFIG)
    voice.add_argument("--local-id", type=int, required=True)
    voice.add_argument("--model", default=os.environ.get("WECHAT_VOICE_WHISPER_MODEL", "base"))
    voice.add_argument(
        "--backend",
        choices=["auto", "faster-whisper", "whisper"],
        default=os.environ.get("WECHAT_VOICE_WHISPER_BACKEND", "auto"),
        help="ASR backend. auto tries faster-whisper then OpenAI whisper.",
    )
    voice.add_argument(
        "--python",
        default=os.environ.get("WECHAT_VOICE_TRANSCRIBE_PYTHON", sys.executable),
        help="Python interpreter that can import faster_whisper or whisper.",
    )
    voice.add_argument("--refresh", action="store_true")
    voice.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    voice.set_defaults(func=cmd_voice_transcribe)

    audio = nested.add_parser(
        "audio-intake",
        help="Transcribe one exact local WeChat audio/video attachment for a worker agent.",
    )
    audio.add_argument("--input", type=Path, required=True)
    audio.add_argument("--output-dir", type=Path, required=True)
    audio.add_argument("--source-local-id", type=int)
    audio.add_argument("--model", default=os.environ.get("WECHAT_AUDIO_WHISPER_MODEL", "medium"))
    audio.add_argument(
        "--backend",
        choices=["auto", "faster-whisper", "whisper"],
        default=os.environ.get("WECHAT_AUDIO_WHISPER_BACKEND", "auto"),
    )
    audio.add_argument(
        "--python",
        default=os.environ.get(
            "WECHAT_AUDIO_TRANSCRIBE_PYTHON",
            os.environ.get("WECHAT_VOICE_TRANSCRIBE_PYTHON", sys.executable),
        ),
        help="Python interpreter that can import faster_whisper or whisper.",
    )
    audio.add_argument("--refresh", action="store_true")
    audio.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    audio.set_defaults(func=cmd_audio_intake)

    finder = nested.add_parser(
        "shipinhao-transcribe",
        help="Resolve and transcribe one exact WeChat Channels/Finder card with source-identity checks.",
    )
    finder.add_argument("--source-text-file", type=Path, required=True, help="UTF-8 file containing the exact Finder card XML/message.")
    finder.add_argument("--output-dir", type=Path, required=True)
    finder.add_argument("--cache-root", type=Path, default=PRIVATE / "shipinhao_media_transcripts")
    finder.add_argument("--capture-manifest", type=Path)
    finder.add_argument("--model", default=os.environ.get("WECHAT_SHIPINHAO_WHISPER_MODEL", "turbo"))
    finder.add_argument("--device", default=os.environ.get("WECHAT_SHIPINHAO_WHISPER_DEVICE", "auto"))
    finder.add_argument("--language", default=os.environ.get("WECHAT_SHIPINHAO_LANGUAGE", ""))
    finder.add_argument("--search-hint", action="append", default=[], help="Optional public-source search hint. Repeatable.")
    finder.add_argument("--max-duration", type=float, default=float(os.environ.get("WECHAT_SHIPINHAO_MAX_DURATION_SECONDS", "3600")))
    finder.add_argument("--no-public-mirror-recovery", action="store_true")
    finder.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    finder.set_defaults(func=cmd_shipinhao_transcribe)

    native_video = nested.add_parser(
        "native-save-video",
        help="Save one exact Android WeChat video through the native album and remove the phone export.",
    )
    native_video.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", ""))
    native_video.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    native_video.add_argument("--target", required=True, help="Allowlisted Android WeChat target key.")
    native_video.add_argument("--targets-file", type=Path)
    native_video.add_argument("--task-id", required=True)
    native_video.add_argument("--output-dir", type=Path, required=True)
    native_video.add_argument("--filename", default="wechat-native-video.mp4")
    native_video.add_argument("--video-tap", required=True, help="Exact visible video center as x,y.")
    native_video.add_argument("--older-pages", type=int, default=0)
    native_video.add_argument("--expected-duration-seconds", type=float, default=0.0)
    native_video.add_argument("--duration-tolerance-seconds", type=float, default=2.0)
    native_video.add_argument("--expected-original-size-mb", type=float, default=0.0)
    native_video.add_argument("--original-download-timeout", type=float, default=600.0)
    native_video.add_argument("--album-export-timeout", type=float, default=120.0)
    native_video.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    native_video.set_defaults(func=cmd_native_save_video)

    autopub = nested.add_parser("autopublish-video", help="Copy the latest mirrored WeChat video to Nutstore AutoPublish.")
    autopub.add_argument("--chat", action="append", default=[], help="Chat/group name to search. Repeatable. Defaults to all mirrored chats.")
    autopub.add_argument("--source", type=Path, help="Explicit local video path. Bypasses the mirror query.")
    autopub.add_argument("--db", type=Path, default=PRIVATE / "wechat_mirror.sqlite")
    autopub.add_argument(
        "--dest",
        type=Path,
        default=Path(os.environ.get("LABCANVAS_AUTOPUBLISH_DIR", "/home/lachlan/Nutstore Files/AutoPublish/AutoPublish")),
    )
    autopub.add_argument("--title", default="", help="Output basename. _COMPLETED is appended if missing.")
    autopub.add_argument("--match-token", action="append", default=[])
    autopub.add_argument("--message-local-id", action="append", type=int, default=[], help="Use an exact WeChat video message local_id. Repeatable.")
    autopub.add_argument(
        "--message-ref",
        action="append",
        default=[],
        help="Use an exact rotated message reference as message_N.db:local_id. Repeatable.",
    )
    autopub.add_argument("--since-minutes", type=float, default=180)
    autopub.add_argument("--limit", type=int, default=10)
    autopub.add_argument("--sync", action="store_true", help="Run media-sync before selecting the video.")
    autopub.add_argument("--fetch-gui", action="store_true", help="Open WeChat and click the latest video to force the client to cache it.")
    autopub.add_argument("--fetch-timeout", type=float, default=90)
    autopub.add_argument("--display", default=DEFAULT_DISPLAY)
    autopub.add_argument("--video-click", default="", help="Relative x,y click inside the WeChat window for the latest visible video. Use x,y;x,y for fallback clicks.")
    autopub.add_argument("--no-auto-source", action="store_true")
    autopub.add_argument("--replace", action="store_true")
    autopub.add_argument("--list", action="store_true")
    autopub.add_argument("--dry-run", action="store_true")
    autopub.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    autopub.set_defaults(func=cmd_autopublish_video)

    backend = nested.add_parser("backend", help="Control optional external WeChat decrypt/MCP receive backends.")
    backend.add_argument(
        "action",
        choices=["install", "status", "probe", "init-config", "find-keys", "decrypt", "monitor", "monitor-web", "api-history", "mcp-server", "mcp-config"],
    )
    backend.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    backend.add_argument("--external", type=Path, default=PRIVATE / "external" / "wechat-decrypt")
    backend.add_argument("--db-dir", type=Path)
    backend.add_argument("--chat", default="wechat-chat")
    backend.add_argument("--repo", default="https://github.com/ylytdeng/wechat-decrypt.git")
    backend.add_argument("--update", action="store_true")
    backend.add_argument("--skip-deps", action="store_true")
    backend.add_argument("--incremental", action="store_true")
    backend.add_argument("--dry-run", action="store_true")
    backend.add_argument("--host", default="127.0.0.1")
    backend.add_argument("--port", type=int, default=5678)
    backend.add_argument("--limit", type=int, default=20)
    backend.add_argument("--filter-chat", default="")
    backend.add_argument("--since", type=int, default=0)
    backend.add_argument("--raw", action="store_true")
    backend.set_defaults(func=cmd_backend)

    rename = nested.add_parser("rename", help="Best-effort group rename through the visible WeChat GUI.")
    rename.add_argument("--chat", default="wechat-chat")
    rename.add_argument("--name", required=True)
    rename.add_argument("--display", default=DEFAULT_DISPLAY)
    rename.add_argument("--dry-run", action="store_true")
    rename.set_defaults(func=cmd_rename)

    alias = nested.add_parser("alias", help="Set this account's in-group alias through WeChat group settings.")
    alias.add_argument("--chat", default="wechat-chat")
    alias.add_argument("--name", required=True)
    alias.add_argument("--display", default=DEFAULT_DISPLAY)
    alias.add_argument("--dry-run", action="store_true")
    alias.add_argument("--skip-ocr-guard", action="store_true")
    alias.set_defaults(func=cmd_alias)

    create_group = nested.add_parser("create-group", help="Create a WeChat group by selecting searched contacts.")
    create_group.add_argument("--display", default=DEFAULT_DISPLAY)
    create_group.add_argument("--plan", type=Path)
    create_group.add_argument("--member-query", action="append", default=[])
    create_group.add_argument("--search-box")
    create_group.add_argument("--search-result-checkbox")
    create_group.add_argument("--create", action="store_true")
    create_group.set_defaults(func=cmd_create_group)

    install = nested.add_parser("install-user-scripts", help="Install small launch wrappers into ~/scripts.")
    install.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    install.set_defaults(func=cmd_install_user_scripts)


def status_payload() -> dict[str, Any]:
    runtime_paths = configured_runtime_paths()
    backends = configured_agent_backends()
    return {
        "ok": True,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "tool_root": str(TOOL_ROOT),
        "private_dir_exists": PRIVATE.exists(),
        "chat_config_exists": DEFAULT_CHAT_CONFIG.exists(),
        "direct_config_exists": DEFAULT_DIRECT_CONFIG.exists(),
        "desktop": desktop_status(),
        "sessions": {
            "supervisor": tmux_status("labcanvas-wechat"),
            "direct_monitor": tmux_status("labcanvas-wechat-direct-chatops"),
            "gui_monitor": tmux_status("labcanvas-wechat-chatops"),
            "career_daily": tmux_status(os.environ.get("WECHAT_CAREER_SESSION", "labcanvas-career-daily")),
        },
        "queue": queue_summary(runtime_paths["queue"]),
        "mirror": mirror_summary(runtime_paths["mirror_db"]),
        "external_backend": external_backend_summary(),
        "codex_sessions": codex_session_summary(),
        "agent_backends": backends,
        "media_sources": [str(path) for path in discover_media_sources()],
        "novnc_url": f"http://127.0.0.1:{DEFAULT_PREVIEW_PORT}/",
        "direct_novnc_url": f"http://127.0.0.1:{DEFAULT_NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale",
    }


def configured_runtime_paths() -> dict[str, Path]:
    paths = {"queue": DEFAULT_QUEUE, "mirror_db": PRIVATE / "wechat_mirror.sqlite"}
    for config_path in discover_direct_monitor_configs():
        if not config_path.exists():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        queue = str(config.get("worker_queue") or "").strip()
        mirror = str(config.get("mirror_db") or "").strip()
        if queue:
            paths["queue"] = Path(queue)
        if mirror:
            paths["mirror_db"] = Path(mirror)
        break
    return paths


def configured_agent_backends() -> list[str]:
    configured: list[str] = []
    env_backend = normalize_agent_backend(os.environ.get("WECHAT_AGENT_BACKEND") or "")
    if env_backend:
        configured.append(env_backend)
    for config_path in discover_direct_monitor_configs():
        if not config_path.exists():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        backend = normalize_agent_backend(str(config.get("agent_backend") or ""))
        if backend and backend not in configured:
            configured.append(backend)
    if not configured:
        from .backends import load_model_policy

        shared_backend = normalize_agent_backend(
            str(load_model_policy().get("primary_backend") or "")
        )
        configured.append(shared_backend or "codex")
    return configured


def normalize_agent_backend(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"claude", "claude-code", "claude_code", "anthropic"}:
        return "claude"
    if normalized in {"codex", "codex-cli", "openai"}:
        return "codex"
    if normalized in {"aginti", "agintiflow", "aginti-flow", "labcanvas"}:
        return "aginti"
    return ""


def run_wechat_action(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    if action == "status":
        return status_payload()
    if action == "start-hold":
        proc = run_command([str(SCRIPTS / "wechat_supervisor_tmux.sh"), "start"], capture=True)
        result = status_payload()
        result.update({"action": action, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(), "returncode": proc.returncode})
        return result
    if action == "start-stack":
        proc = run_command([str(SCRIPTS / "wechat_stack_tmux.sh"), "start"], capture=True)
        result = status_payload()
        result.update({"action": action, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(), "returncode": proc.returncode})
        return result
    if action == "stop-hold":
        proc = run_command([str(SCRIPTS / "wechat_supervisor_tmux.sh"), "stop"], capture=True)
        result = status_payload()
        result.update({"action": action, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(), "returncode": proc.returncode})
        return result
    if action == "send-message":
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")
        return send_wechat_message_api(
            message=message,
            chat=str(payload.get("chat") or ""),
            target=payload.get("target") if isinstance(payload.get("target"), dict) else None,
            target_name=str(payload.get("target") or ""),
            send_targets=Path(str(payload.get("send_targets") or DEFAULT_SEND_TARGETS)),
            config=Path(str(payload.get("config") or DEFAULT_CHAT_CONFIG)),
            display=str(payload.get("display") or ""),
            dry_run=bool(payload.get("dry_run", False)),
            allow_search=bool(payload.get("allow_search", False)) if "allow_search" in payload else None,
        )
    if action == "send-api":
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")
        return send_wechat_message_api(
            message=message,
            chat=str(payload.get("chat") or ""),
            target=payload.get("target") if isinstance(payload.get("target"), dict) else None,
            target_name=str(payload.get("target") or ""),
            send_targets=Path(str(payload.get("send_targets") or DEFAULT_SEND_TARGETS)),
            config=Path(str(payload.get("config") or DEFAULT_CHAT_CONFIG)),
            display=str(payload.get("display") or ""),
            dry_run=bool(payload.get("dry_run", False)),
            allow_search=bool(payload.get("allow_search", False)) if "allow_search" in payload else None,
        )
    if action == "worker-once":
        proc = run_command([sys.executable, str(SCRIPTS / "wechat_task_worker.py"), "--queue", str(DEFAULT_QUEUE), "--once", "--send"], capture=True)
        result = status_payload()
        result.update({"action": action, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(), "returncode": proc.returncode})
        return result
    if action in {"approve-next", "reject-next"}:
        decision = "approve" if action == "approve-next" else "reject"
        note = str(payload.get("note") or "").strip()
        task = update_waiting_task(DEFAULT_QUEUE, None, decision=decision, note=note)
        result = status_payload()
        result.update({"action": action, "task": task})
        return result
    return {"ok": False, "error": f"Unsupported WeChat action: {action}"}


def send_wechat_message_api(
    *,
    message: str,
    chat: str = "",
    target: dict[str, Any] | None = None,
    target_name: str = "",
    send_targets: Path = DEFAULT_SEND_TARGETS,
    config: Path = DEFAULT_CHAT_CONFIG,
    display: str = "",
    dry_run: bool = False,
    allow_search: bool | None = None,
) -> dict[str, Any]:
    """Send a target-scoped WeChat message through the guarded GUI sender.

    This is the reusable API boundary for CLI, web, and future local callers.
    It keeps target resolution, title-guard requirements, dry-run behavior, and
    stdout parsing in one place instead of sending through ad hoc GUI commands.
    """
    clean_message = sanitize_outbound_wechat_message(message)
    if not clean_message:
        raise ValueError("message is empty after sanitization")
    selected_target = resolve_wechat_send_target(chat, target=target, target_name=target_name, send_targets=send_targets, config=config)
    if allow_search is not None:
        selected_target["allow_search"] = bool(allow_search)
        if not allow_search:
            selected_target["no_search"] = True
    validate_wechat_send_target(selected_target)
    config_data = load_json_file(config)
    display_value = display or str(config_data.get("display") or DEFAULT_DISPLAY)
    with tempfile.NamedTemporaryFile("w+", suffix=".json", encoding="utf-8", delete=False) as handle:
        target_file = Path(handle.name)
        json.dump({"message": clean_message, "targets": [selected_target]}, handle, ensure_ascii=False)
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_gui_send.py"),
        "--display",
        display_value,
        "--targets-file",
        str(target_file),
        "--prefer-current",
        "--pause",
        str(selected_target.get("pause_seconds") or os.environ.get("WECHAT_API_SEND_PAUSE", "0.35")),
        "--mirror-db",
        str(configured_runtime_paths()["mirror_db"]),
    ]
    if dry_run:
        command.append("--compose-dry-run")
    else:
        command.append("--send")
    if bool(selected_target.get("allow_search")) and not bool(selected_target.get("no_search")):
        command.append("--allow-search")
    else:
        command.append("--no-search")
    try:
        proc = run_command(command, capture=True, env=wechat_api_send_env())
    finally:
        target_file.unlink(missing_ok=True)
    parsed = parse_json_object(proc.stdout)
    result = {
        "ok": proc.returncode == 0,
        "action": "send-api",
        "dry_run": dry_run,
        "chat": chat or str(selected_target.get("name") or selected_target.get("target") or ""),
        "target": public_send_target(selected_target),
        "message_chars": len(clean_message),
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-1200:].strip(),
        "stderr_tail": (proc.stderr or "")[-1200:].strip(),
        "manifest": parsed if isinstance(parsed, dict) else None,
    }
    if proc.returncode != 0:
        result["error"] = result["stderr_tail"] or result["stdout_tail"] or f"send failed with exit {proc.returncode}"
    return result


def wechat_api_send_env() -> dict[str, str]:
    env = os.environ.copy()
    timeout = int(os.environ.get("WECHAT_API_SEND_TIMEOUT_SECONDS", os.environ.get("WECHAT_WORKER_SEND_TIMEOUT_SECONDS", "120")))
    env.setdefault("WECHAT_GUI_SEND_MAX_SECONDS", str(max(45, timeout - 5)))
    env.setdefault("WECHAT_INITIAL_TITLE_WAIT", os.environ.get("WECHAT_API_INITIAL_TITLE_WAIT", "0.8"))
    env.setdefault("WECHAT_TITLE_RETRY_SECONDS", os.environ.get("WECHAT_API_TITLE_RETRY_SECONDS", "8.0"))
    return env


def sanitize_outbound_wechat_message(message: str, *, max_chars: int = 1800) -> str:
    raw = str(message or "").strip()
    if not raw:
        return ""
    if is_no_reply_control_text(raw):
        return ""
    lines: list[str] = []
    dropped = 0
    noisy = (
        re.compile(r"^\s*(?:debug|trace|stdout|stderr|returncode|command|executing|running)\b[:\s-]", re.I),
        re.compile(r"^\s*(?:aginti|codex|claude|backend|model|reasoning_effort|sandbox)\b[:\s-]", re.I),
        re.compile(r"^\s*[\w./-]+(?:\.py|\.sh)(?:\s|:)", re.I),
    )
    for line in raw.splitlines():
        stripped = line.strip()
        if any(pattern.search(stripped) for pattern in noisy):
            dropped += 1
            continue
        lines.append(line.rstrip())
    cleaned = "\n".join(lines).strip()
    if is_no_reply_control_text(cleaned):
        return ""
    if not cleaned and dropped:
        cleaned = "后台任务已结束，但输出主要是工具日志；原始日志已过滤。"
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 18].rstrip() + "\n...[已截断]"
    return cleaned


def is_no_reply_control_text(text: str) -> bool:
    control = str(text or "").strip().lstrip("`*_#>•- ")
    control = re.sub(r"^(?:chat|ack)\s*[:：]\s*", "", control, count=1, flags=re.I)
    return bool(re.match(r"^no[\s_-]*reply(?:\b|_)", control, flags=re.I))


def resolve_wechat_send_target(
    chat: str,
    *,
    target: dict[str, Any] | None,
    target_name: str,
    send_targets: Path,
    config: Path,
) -> dict[str, Any]:
    if target:
        return dict(target)
    explicit = target_name.strip()
    if explicit:
        return {"name": explicit, "query": explicit, "expected_title": explicit}
    chat_key = chat.strip()
    config_data = load_json_file(config)
    if not chat_key:
        chat_key = str(config_data.get("chat_name") or "").strip()
    direct_target = config_data.get("send_target") if isinstance(config_data.get("send_target"), dict) else None
    registry = load_json_file(send_targets)
    registry_target = registry.get(chat_key) if isinstance(registry, dict) else None
    if isinstance(direct_target, dict) and (not chat_key or str(config_data.get("chat_name") or "") == chat_key):
        if isinstance(registry_target, dict):
            return {**direct_target, **registry_target}
        return dict(direct_target)
    if isinstance(registry_target, dict):
        return dict(registry_target)
    for direct_config in PRIVATE.glob("*direct-chatops.local.json"):
        data = load_json_file(direct_config)
        if str(data.get("chat_name") or "") != chat_key:
            continue
        raw = data.get("send_target")
        if isinstance(raw, dict):
            if isinstance(registry_target, dict):
                return {**raw, **registry_target}
            return dict(raw)
    raise ValueError(f"No guarded WeChat send target configured for chat {chat_key!r}")


def validate_wechat_send_target(target: dict[str, Any]) -> None:
    name = str(target.get("name") or target.get("target") or target.get("query") or "").strip()
    query = str(target.get("query") or name).strip()
    expected = str(target.get("expected_title") or target.get("title") or name).strip()
    aliases = target.get("expected_title_aliases") or target.get("title_aliases") or []
    if not name or not query:
        raise ValueError("WeChat send target requires name/query")
    if not expected and not aliases:
        raise ValueError(f"WeChat send target {name!r} requires expected_title or expected_title_aliases")
    target.setdefault("name", name)
    target.setdefault("query", query)
    target.setdefault("expected_title", expected or name)


def public_send_target(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(target.get("name") or target.get("target") or ""),
        "query": str(target.get("query") or ""),
        "expected_title": str(target.get("expected_title") or target.get("title") or ""),
        "expected_title_aliases": [str(item) for item in target.get("expected_title_aliases", []) or []],
        "allow_search": bool(target.get("allow_search")) and not bool(target.get("no_search")),
    }


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def cmd_status(args: argparse.Namespace) -> int:
    payload = status_payload()
    print_payload(payload, args.json, f"wechat: {payload['desktop']['status']} {payload['novnc_url']}")
    return 0


def phone_ingress_status(script_name: str) -> dict[str, Any]:
    """Probe one bounded Android WeChat intake lane without reading chat content."""

    script = SCRIPTS / script_name
    if not script.is_file():
        return {"ok": False, "reason": "status_script_missing"}
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--status"],
            cwd=PACKAGE_ROOT,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "reason": type(exc).__name__}
    payload = parse_json_object(proc.stdout) or {}
    if not payload:
        return {
            "ok": False,
            "reason": "invalid_status_output",
            "returncode": proc.returncode,
        }
    return payload


def status_timestamp_age_seconds(value: object) -> int | None:
    parsed = parse_health_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def compact_phone_lane(payload: dict[str, Any], *, self_authored: bool) -> dict[str, Any]:
    last_poll = payload.get("last_poll_at")
    age = status_timestamp_age_seconds(last_poll)
    fresh = age is not None and age <= 120
    result = {
        "ok": bool(payload.get("ok")) and fresh,
        "fresh": fresh,
        "last_poll_age_seconds": age,
        "routes": int(payload.get("routes") or 0),
    }
    if self_authored:
        result.update(
            {
                "seeded_routes": int(payload.get("seeded_routes") or 0),
                "deferred_routes": int(payload.get("deferred_routes") or 0),
                "last_error": str(payload.get("last_error") or "")[:240],
            }
        )
    else:
        result.update(
            {
                "listener_enabled": bool(payload.get("listener_enabled")),
                "listener_live": bool(payload.get("listener_live")),
            }
        )
    return result


def compact_health_payload(
    payload: dict[str, Any],
    *,
    notification_status: dict[str, Any] | None = None,
    self_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the small authoritative status packet used by backend agents."""

    transport = payload.get("transport_health")
    transport = transport if isinstance(transport, dict) else {}
    schedules = transport.get("schedules")
    schedules = schedules if isinstance(schedules, dict) else {}
    career = schedules.get("career_daily")
    career = career if isinstance(career, dict) else {}
    echomind = schedules.get("echomind")
    echomind = echomind if isinstance(echomind, dict) else {}
    labagent = schedules.get("labagent_idle_inspiration")
    labagent = labagent if isinstance(labagent, dict) else {}
    queues = transport.get("queues")
    queues = queues if isinstance(queues, dict) else {}
    monitors = transport.get("direct_monitors")
    monitors = monitors if isinstance(monitors, dict) else {}
    configured = int(monitors.get("configured") or 0)
    heartbeat_healthy = int(monitors.get("heartbeat_healthy") or 0)
    monitor_heartbeats_ok = configured > 0 and heartbeat_healthy == configured

    notification = compact_phone_lane(
        notification_status or phone_ingress_status("wechat_android_ingress.py"),
        self_authored=False,
    )
    self_lane = compact_phone_lane(
        self_status or phone_ingress_status("wechat_android_screen_ingress.py"),
        self_authored=True,
    )
    notification["reaches_agent"] = bool(notification["ok"] and monitor_heartbeats_ok)
    self_lane["reaches_agent"] = bool(self_lane["ok"] and monitor_heartbeats_ok)

    compact_queues: dict[str, Any] = {}
    for name in ("wechat", "wecom"):
        item = queues.get(name)
        item = item if isinstance(item, dict) else {}
        compact_queues[name] = {
            "ok": bool(item.get("ok")),
            "active": int(item.get("active") or 0),
            "pending": int(item.get("pending") or 0),
            "recent_failure_count": len(item.get("recent_failed_ids") or []),
            "stale_count": len(item.get("stale_ids") or []),
        }

    echo_retry = bool(echomind.get("daily_pdf_generation_retry_active"))
    schedule_summary = {
        "career_daily": {
            "running": bool(career.get("running")),
            "status": str(career.get("career_status") or "unknown"),
            "delivered": bool(career.get("career_complete")),
            "retry_pending": bool(career.get("career_retry_pending")),
        },
        "memo_daily": {
            "required": bool(career.get("organizer_required")),
            "status": str(career.get("organizer_status") or "unknown"),
            "delivered": bool(career.get("organizer_complete")),
            "retry_pending": bool(career.get("organizer_retry_pending")),
        },
        "echomind_periodic_lesson": {
            "running": bool(echomind.get("running")),
            "interval_seconds": int(echomind.get("interval_seconds") or 0),
            "pending": bool(echomind.get("pending_lesson")),
            "retry_pending": bool(echomind.get("pending_lesson_retry_pending")),
        },
        "echomind_daily_pdf": {
            "target_date": str(echomind.get("daily_pdf_target_date") or ""),
            "status": "quality_retry_pending" if echo_retry else "current_or_not_due",
            "retry_pending": echo_retry,
            "next_attempt_at": str(echomind.get("pending_daily_pdf_next_attempt_at") or ""),
        },
        "labagent_idle_inspiration": {
            "running": bool(labagent.get("ok")),
            "status": str(labagent.get("status") or "unknown"),
        },
    }
    issue_codes = [
        str(item.get("code") or "")
        for item in transport.get("issues") or []
        if isinstance(item, dict) and item.get("code")
    ]
    agent_failures = transport.get("agent_failures")
    agent_failures = agent_failures if isinstance(agent_failures, dict) else {}
    schedules_ok = bool(schedules.get("ok"))
    queues_ok = all(item["ok"] for item in compact_queues.values())
    phone_ok = bool(notification["reaches_agent"] and self_lane["reaches_agent"])
    client = transport.get("wechat_client")
    client = client if isinstance(client, dict) else {}
    desktop_ok = bool(
        client.get("available")
        and str(client.get("status") or "").casefold()
        in {"ok", "ready", "unlocked"}
        and monitor_heartbeats_ok
    )
    operational = bool(
        schedules_ok
        and queues_ok
        and (desktop_ok or phone_ok)
        and int(agent_failures.get("terminal_failures") or 0) == 0
    )
    return {
        "ok": operational,
        "operational": operational,
        "degraded": bool(issue_codes),
        "checked_at": str(transport.get("checked_at") or payload.get("checked_at") or ""),
        "issues": issue_codes,
        "desktop_wechat": {
            "available": bool((transport.get("wechat_client") or {}).get("available")),
            "status": str((transport.get("wechat_client") or {}).get("status") or "unknown"),
        },
        "phone_ingress": {
            "other_people": notification,
            "self_authored": self_lane,
        },
        "direct_monitor_heartbeats": {
            "ok": monitor_heartbeats_ok,
            "healthy": heartbeat_healthy,
            "configured": configured,
        },
        "schedules": schedule_summary,
        "queues": compact_queues,
        "agent_failures_last_hour": int(agent_failures.get("terminal_failures") or 0),
    }


def cmd_health(args: argparse.Namespace) -> int:
    payload = direct_monitor_health()
    transport = persistent_transport_health()
    payload["transport_health"] = transport
    if transport:
        payload["diagnostic_ok"] = payload.get("ok")
        queue_attention = (
            (payload.get("queue") or {}).get("attention")
            if isinstance(payload.get("queue"), dict)
            else {}
        )
        client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
        client_available = client.get("available") is not False
        payload["ok"] = (
            bool(transport.get("ok"))
            and client_available
            and not bool(
                isinstance(queue_attention, dict) and queue_attention.get("needs_attention")
            )
        )
        monitor_status = transport.get("direct_monitors") or {}
        summary = (
            "wechat health: "
            f"{monitor_status.get('healthy', 0)}/{monitor_status.get('configured', 0)} "
            "monitor heartbeats healthy"
        )
    else:
        summary = f"wechat health: {payload['ready_groups']}/{payload['group_count']} ready"
    if payload.get("stale_source_groups"):
        summary += f", {payload['stale_source_groups']} stale source"
    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    if client.get("available") is False:
        summary += f", client {client.get('status') or 'unavailable'}"
    queue_attention = (payload.get("queue") or {}).get("attention") if isinstance(payload.get("queue"), dict) else None
    if isinstance(queue_attention, dict) and queue_attention.get("needs_attention"):
        counts = queue_attention.get("counts") or {}
        summary += (
            f", queue attention blocked={counts.get('delivery_blocked', 0)}"
            f" waiting={counts.get('human_blocked', 0)} failed={counts.get('failed', 0)}"
        )
    if not payload["ok"]:
        summary += " (attention needed)"
    if getattr(args, "compact", False):
        compact = compact_health_payload(payload)
        compact_summary = (
            "wechat compact health: operational"
            if compact["operational"]
            else "wechat compact health: attention needed"
        )
        print_payload(compact, args.json, compact_summary)
        return 0 if compact["operational"] else 1
    print_payload(payload, args.json, summary)
    return 0 if payload["ok"] else 1


def persistent_transport_health(*, max_age_seconds: float = 90.0) -> dict[str, Any]:
    """Read the persistent guard snapshot, probing directly when it is stale."""

    cached = load_json_file(TRANSPORT_HEALTH_SNAPSHOT)
    checked_at = parse_health_timestamp(cached.get("checked_at"))
    age = (
        max(0.0, (datetime.now(timezone.utc) - checked_at).total_seconds())
        if checked_at is not None
        else None
    )
    if cached and age is not None and age <= max_age_seconds:
        return {**cached, "source": "persistent_guard", "snapshot_age_seconds": int(age)}
    if not TRANSPORT_HEALTH_GUARD.is_file():
        return {}
    with tempfile.TemporaryDirectory(prefix="labcanvas-health-") as tmp:
        proc = run_command(
            [
                sys.executable,
                str(TRANSPORT_HEALTH_GUARD),
                "--json",
                "--state-path",
                str(Path(tmp) / "state.json"),
                "--snapshot-path",
                str(Path(tmp) / "snapshot.json"),
            ],
            capture=True,
        )
    payload = parse_json_object(proc.stdout) or {}
    if payload:
        payload["source"] = "direct_probe"
    return payload


def parse_health_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc)


def cmd_doctor(args: argparse.Namespace) -> int:
    agent_commands = configured_agent_backends()
    commands = [
        "tmux",
        "Xvfb",
        "x11vnc",
        "websockify",
        "xdotool",
        "xclip",
        "import",
        "tesseract",
        "ffmpeg",
        "ffprobe",
    ]
    commands.extend("claude" if backend == "claude" else "aginti" if backend == "aginti" else "codex" for backend in agent_commands)
    commands = list(dict.fromkeys(commands))
    checks = {name: shutil.which(name) or "" for name in commands}
    scripts = {
        path.name: path.exists()
        for path in (
            SCRIPTS / "wechat_virtual_desktop.sh",
            SCRIPTS / "wechat_supervisor_tmux.sh",
            SCRIPTS / "wechat_agent_backend.py",
            SCRIPTS / "wechat_direct_chatops.py",
            SCRIPTS / "wechat_direct_backend.py",
            SCRIPTS / "wechat_task_worker.py",
            SCRIPTS / "wechat_worker_guarded_loop.sh",
            SCRIPTS / "wechat_routines.py",
            SCRIPTS / "wechat_chatops_bridge.py",
            SCRIPTS / "wechat_browser_assist.py",
            SCRIPTS / "wechat_media_sync.py",
            SCRIPTS / "wechat_audio_intake.py",
        )
    }
    missing = [name for name, found in checks.items() if not found]
    payload = {
        "ok": not missing and all(scripts.values()),
        "commands": checks,
        "agent_backends": agent_commands,
        "scripts": scripts,
        "missing": missing,
    }
    print_payload(payload, args.json, "wechat doctor: " + ("ok" if payload["ok"] else "missing " + ", ".join(missing)))
    return 0 if payload["ok"] else 1


def cmd_control_map(args: argparse.Namespace) -> int:
    payload = control_map_payload()
    print_payload(payload, args.json, f"wechat control-map: {payload['score']['label']} {payload['score']['ready_layers']}/{payload['score']['total_layers']} layers ready")
    return 0


def cmd_routines(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(SCRIPTS))
    from wechat_routines import list_routines

    routines = list_routines()
    payload = {"ok": True, "count": len(routines), "routines": routines}
    summary = "wechat routines: " + ", ".join(str(item.get("id") or "") for item in routines)
    print_payload(payload, args.json, summary)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    checks = selftest_checks_for_suite(args.suite)
    results = []
    for check in checks:
        proc = run_command(
            [sys.executable, "-m", "unittest", check["test"]],
            capture=True,
            env={**os.environ, "WECHAT_TINY11_DISABLE": "1", "PYTHONPATH": str(PACKAGE_ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")},
        )
        results.append(
            {
                "id": check["id"],
                "ok": proc.returncode == 0,
                "test": check["test"],
                "stdout_tail": proc.stdout[-1200:],
                "stderr_tail": proc.stderr[-1200:],
                "returncode": proc.returncode,
            }
        )
    payload = {
        "ok": all(item["ok"] for item in results),
        "suite": args.suite,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "results": results,
        "contract": selftest_contract_for_suite(args.suite),
    }
    print_payload(payload, args.json, f"wechat selftest {args.suite}: " + ("ok" if payload["ok"] else "failed"))
    return 0 if payload["ok"] else 1


def selftest_checks_for_suite(suite: str) -> list[dict[str, str]]:
    if suite == "publish-poststage":
        return publish_poststage_selftest_checks()
    if suite == "transport-resume":
        return transport_resume_selftest_checks()
    if suite == "all":
        return publish_poststage_selftest_checks() + transport_resume_selftest_checks()
    raise ValueError(f"Unsupported selftest suite: {suite}")


def selftest_contract_for_suite(suite: str) -> list[str]:
    contracts: dict[str, list[str]] = {
        "publish-poststage": [
            "missing LazyEdit publish job triggers the worker's real publish command",
            "existing LazyEdit/remote job is monitored and not duplicated",
            "remote platform login blockers deliver an exact job-scoped QR and keep poststage polling",
            "Android publication intake rejects player captures and requires a verified native export with phone cleanup",
        ],
        "transport-resume": [
            "WeChat queue rows carry a message-transport execution contract",
            "opaque card URLs and checksums cannot trigger authored-command danger filters",
            "every fresh coalesced source row enters one backend-independent message ledger",
            "Codex and AgInTi worker packets preserve identical authoritative source scope",
            "completion auditing checks each coalesced message-ledger identity",
            "nontrivial worker tasks resume the exact chat's Codex worker session",
            "recent safe dead-worker tasks receive one bounded restart recovery while unsafe or old rows remain abandoned",
            "worker progress writes cannot erase same-chat interruptions that arrived after claim",
            "GUI sender alarm is aligned with the worker send timeout",
            "GUI text sends fail on unconsumed clipboard data and verify the exact composer contents before Enter",
            "GUI navigation rejects preview-text false positives while tolerating only bounded separator OCR variants",
            "GUI navigation ignores unrelated avatar OCR grouped onto the exact chat-title line",
            "GUI file sends re-verify the exact chat after the native picker and before submission",
            "Android file delivery retries exact mixed-language titles with coordinate-safe enhanced OCR",
            "Android share search cannot mistake the typed query for a recipient row",
            "Android share delivery taps the matched recent-recipient tile instead of a neighboring chat",
            "a committed Android file share is not downgraded by post-send chat restoration",
            "send_retrying rows are not reclaimed before the active GUI sender timeout plus grace",
            "exact-task media resolution rejects files associated only by modification time",
            "exact media tokens reject unrelated readable files from the same chat",
            "publish-video tasks bypass the generic same-chat media resolver",
            "a bare video attachment is saved privately without ASR, LazyEdit, an agent turn, a reply, or publication",
            "a later explicit same-chat publication message promotes only that exact video source into one task",
            "direct publish questions do not invent a third-party confirmation gate",
            "the original requester can replace a consent wait without creating a second task",
            "operator approval refreshes a stale publish task and clears its obsolete denial state",
            "ingress keeps independent cursors and bounded context across rotated message shards",
            "video publication binds local message IDs to their exact rotated database shard",
            "attachment identity never falls through to a duplicate local ID in another shard",
            "restart transport recovery is recent, bounded, transport-scoped, and idempotent",
            "chat-sync dry-open alarm is long enough to refresh inactive groups",
            "chat-sync retryable failures back off per chat without blocking other groups",
            "plain story/script requests route to the same worker capability set in research and device chats",
            "book source discovery routes to the guarded sibling Books control plane without authorizing downloads",
            "multilingual book work routes to one durable PocketPolyglot project and remains interruptible",
            "explicit sibling-project feedback routes to an evidence-gated allowlisted local report",
            "unverified or transient feedback cannot become a repository report or chat attachment",
            "all monitored chats route explicit backend/tool/artifact requests through the shared routine skill surface",
            "EchoMind remains language-learning by default while explicit backend instructions enqueue worker routines",
            "backend runtime session metadata can never become a user-facing WeChat reply",
            "chat-only memo routes use the route agent's natural reply without a second agent turn or execution worker",
            "generic execution-evidence refusals can never become fast conversational replies",
            "AgInTi receives a bounded selected-routine prompt instead of the full Codex worker handbook",
            "AgInTi provider fallback retries only verified pre-inference failures",
            "the shared AgInTi backend honors transport-specific WeCom provider settings",
            "AgInTi worker packets preserve current intent and latest interruptions within LocalLLM context limits",
            "backend failure diagnostics remain private and cannot become a chat reply",
            "an interactive task gets one short source-scoped terminal receipt after all backends fail",
            "legacy WeCom tasks can never fall back to the personal-WeChat sender",
            "an exact-row backfill isolates one chat-local message and restores the live cursor",
            "route and worker Codex exec calls resume exact per-chat sessions by role",
            "route and worker prompts preserve every safe explicit current instruction instead of shrinking to keyword matches",
            "queued worker tasks persist a machine-readable instruction contract",
            "legacy queued worker tasks backfill the instruction contract before execution",
            "completion auditing cannot turn transport PDF policy into a user artifact request",
            "every coalesced source row keeps a hard numbered completion identity without a merge cap",
            "omitted numbered requirements receive one same-session repair and one independently queued supplement fallback",
            "explicit PDF requests require both a direct answer and a real PDF artifact",
            "generated-video contracts choose relatively cheaper Seedance models without blocking tasks on model selection",
            "generated-video generation is not public publication; reference-image upload does not authorize publish",
            "generated-video LazyEdit commands prefer worker-created correction context and metadata brief files",
            "generated-video LazyEdit context appends generated story/script and prompt material before publish",
            "mp.weixin read-only recovery extracts article content and reuses the private cache without GUI verification",
            "Android article fallback opens the exact same-chat card, copies its canonical link, and verifies the recovered title",
            "group voice transcripts cross the private queue boundary without raw voice secrets",
            "encoded WeChat card types still enter exact same-chat media resolution",
            "ordinary group audio/video becomes agent-readable transcript context before the resumed Codex turn",
            "Shipinhao comment discovery accepts only the exact current card export",
            "Shipinhao media transcription uses only the exact current card, allowlisted Tencent hosts, a content-verified public mirror, or a matching visual-identity/audio-hash manifest",
            "Shipinhao public-mirror recovery requires exact-duration content evidence or a bounded, independently corroborated longer-source excerpt",
            "Shipinhao related-speaker clips are rejected when they do not match the card paraphrase and topic evidence",
            "the backend receives a bounded task packet with readable context paths but no raw Finder XML or signed media tokens",
            "Shipinhao download failure is never mislabeled as verified silent media",
            "Shipinhao acquisition failure writes agent context and cannot alias to verified silence",
            "Shipinhao native fallback binds a play control only to source identity in the same card",
            "Shipinhao native fallback can bind the exact cached cover on the received side",
            "Android Shipinhao fallback returns to the exact chat after audio-helper prewarm before opening or capturing a card",
            "read-only source tasks cannot enter waiting_confirmation for a verification page",
            "ZIP, RAR, 7z, Word, PDF, and text attachments are safely extracted into agent-readable task context",
            "successful outbound files cannot re-enter the same chat as self-authored attachment tasks",
            "bare file intake never returns the uploaded source unless the request explicitly asks",
            "the tmux worker starts through the guarded self-test entrypoint",
        ],
    }
    if suite == "all":
        return contracts["publish-poststage"] + contracts["transport-resume"]
    if suite in contracts:
        return contracts[suite]
    raise ValueError(f"Unsupported selftest suite: {suite}")


def publish_poststage_selftest_checks() -> list[dict[str, str]]:
    prefix = "tests.test_wechat_task_worker.WeChatTaskWorkerTests."
    return [
        {
            "id": "missing_job_reissues_publish",
            "test": prefix + "test_publish_poststage_reissues_lazyedit_when_no_local_job_exists",
        },
        {
            "id": "existing_job_not_duplicated",
            "test": prefix + "test_publish_poststage_does_not_reissue_when_local_job_exists",
        },
        {
            "id": "login_blocker_sends_qr_and_keeps_polling",
            "test": prefix + "test_publish_poststage_login_blocker_sends_qr_and_keeps_polling",
        },
        {
            "id": "remote_login_log_detection",
            "test": prefix + "test_detect_remote_publish_login_blocker_from_log",
        },
        {
            "id": "android_native_source_guard",
            "test": (
                "tests.test_wechat_native_video_source.WeChatNativeVideoSourceTests."
                "test_automation_capture_is_rejected_at_autopublish_copy_boundary"
            ),
        },
    ]


def transport_resume_selftest_checks() -> list[dict[str, str]]:
    worker_prefix = "tests.test_wechat_task_worker.WeChatTaskWorkerTests."
    direct_prefix = "tests.test_wechat_direct_chatops.WeChatDirectChatopsPolicyTests."
    backend_prefix = "tests.test_wechat_agent_backend.WeChatAgentBackendTests."
    gui_prefix = "tests.test_wechat_gui_send.WeChatGuiSendTests."
    android_prefix = "tests.test_wechat_android_send.WechatAndroidSendTests."
    audit_prefix = "tests.test_wechat_completion_audit.WeChatCompletionAuditTests."
    return [
        {
            "id": "queued_task_has_transport_contract",
            "test": direct_prefix + "test_enqueue_worker_task_persists_routine_contract",
        },
        {
            "id": "opaque_card_metadata_not_danger",
            "test": direct_prefix + "test_opaque_card_url_does_not_trigger_danger_filter",
        },
        {
            "id": "coalesced_sources_have_message_ledger",
            "test": direct_prefix + "test_enqueue_worker_task_preserves_each_coalesced_source_in_ledger",
        },
        {
            "id": "backend_message_ledger_parity",
            "test": worker_prefix + "test_all_worker_backends_receive_the_same_authoritative_message_ledger",
        },
        {
            "id": "completion_audits_transport_ledger",
            "test": audit_prefix + "test_transport_message_ledger_is_authoritative_for_coalesced_sources",
        },
        {
            "id": "worker_resumes_codex_session",
            "test": worker_prefix + "test_orchestrator_resumes_codex_session_for_nontrivial_stage",
        },
        {
            "id": "dead_worker_claim_abandoned",
            "test": worker_prefix + "test_claim_next_pending_does_not_recover_publication_after_dead_worker",
        },
        {
            "id": "safe_dead_worker_claim_recovered_once",
            "test": worker_prefix + "test_claim_next_pending_recovers_recent_safe_dead_worker_once",
        },
        {
            "id": "expired_confirmation_leaves_live_queue",
            "test": worker_prefix + "test_claim_next_pending_expires_old_confirmation_without_running_it",
        },
        {
            "id": "concurrent_interruption_survives_worker_progress",
            "test": worker_prefix + "test_rewrite_task_preserves_interruptions_added_after_worker_claim",
        },
        {
            "id": "gui_send_alarm_aligned",
            "test": worker_prefix + "test_wechat_send_env_extends_gui_alarm_to_worker_timeout",
        },
        {
            "id": "gui_clipboard_timeout_fails",
            "test": gui_prefix + "test_paste_text_timeout_is_a_send_failure",
        },
        {
            "id": "gui_composer_readback_exact",
            "test": gui_prefix + "test_verify_composer_text_reads_exact_message_back",
        },
        {
            "id": "gui_preview_target_rejected",
            "test": gui_prefix + "test_visible_chat_list_match_rejects_preview_that_mentions_target",
        },
        {
            "id": "gui_separator_ocr_bounded",
            "test": gui_prefix + "test_visible_chat_list_match_accepts_separator_ocr_variant",
        },
        {
            "id": "gui_avatar_ocr_ignored",
            "test": gui_prefix + "test_visible_chat_list_match_ignores_avatar_ocr_on_title_line",
        },
        {
            "id": "gui_file_target_rechecked_after_picker",
            "test": gui_prefix + "test_guarded_file_helper_rejects_target_change_after_picker",
        },
        {
            "id": "android_mixed_title_enhanced_ocr",
            "test": android_prefix + "test_find_target_line_retries_with_enhanced_ocr",
        },
        {
            "id": "android_share_query_not_recipient",
            "test": android_prefix + "test_share_search_result_does_not_select_query_text",
        },
        {
            "id": "android_share_uses_matched_tile",
            "test": android_prefix + "test_share_recipient_tap_uses_matched_horizontal_tile",
        },
        {
            "id": "android_committed_file_not_downgraded",
            "test": android_prefix
            + "test_successful_file_send_is_not_failed_by_post_send_chat_restore",
        },
        {
            "id": "send_retry_stale_after_sender_timeout",
            "test": worker_prefix + "test_send_retrying_waits_longer_than_sender_timeout",
        },
        {
            "id": "mtime_only_cross_chat_media_rejected",
            "test": worker_prefix + "test_media_resolution_rejects_mtime_only_cross_chat_candidate",
        },
        {
            "id": "unrelated_readable_media_token_rejected",
            "test": worker_prefix + "test_media_resolution_rejects_readable_candidate_without_exact_media_token",
        },
        {
            "id": "publish_video_bypasses_generic_media_resolution",
            "test": worker_prefix + "test_publish_video_uses_exact_autopublish_preflight_not_generic_media_resolution",
        },
        {
            "id": "bare_video_route_is_passive",
            "test": direct_prefix + "test_bare_video_upload_is_passive_cache_even_when_agent_misroutes",
        },
        {
            "id": "bare_video_preflight_stops_after_save",
            "test": worker_prefix + "test_passive_video_preflight_stops_after_exact_source_save",
        },
        {
            "id": "explicit_text_promotes_exact_bare_video",
            "test": worker_prefix + "test_worker_reconciles_legacy_bare_video_and_explicit_publish_rows",
        },
        {
            "id": "direct_publish_has_no_third_party_wait",
            "test": direct_prefix + "test_direct_publish_question_is_authorization_without_third_party_wait",
        },
        {
            "id": "requester_can_override_consent_wait",
            "test": direct_prefix + "test_original_requester_can_replace_consent_wait_with_direct_publish_instruction",
        },
        {
            "id": "operator_publish_approval_resets_stale_state",
            "test": "tests.test_wechat_ops_health.WeChatOpsApprovalTests.test_approve_publish_consent_resets_stale_state_and_allows_same_task",
        },
        {
            "id": "message_shard_rollover_context_preserved",
            "test": direct_prefix + "test_message_db_rollover_reads_new_shard_with_independent_cursor",
        },
        {
            "id": "video_message_shard_identity_preserved",
            "test": worker_prefix + "test_video_message_ref_uses_newest_matching_shard_when_local_id_restarts",
        },
        {
            "id": "attachment_message_shard_identity_preserved",
            "test": worker_prefix + "test_exact_file_identity_never_falls_through_to_duplicate_id_in_older_shard",
        },
        {
            "id": "recent_transport_recovery_bounded",
            "test": worker_prefix + "test_recover_recent_expired_transport_delivery_is_bounded_and_scoped",
        },
        {
            "id": "recent_transport_recovery_idempotent",
            "test": worker_prefix + "test_recover_recent_expired_transport_delivery_does_not_requeue_twice",
        },
        {
            "id": "chat_sync_gui_alarm_aligned",
            "test": "tests.test_wechat_chat_sync_loop.WeChatChatSyncLoopTests.test_chat_sync_gui_send_env_uses_sync_timeout_for_dry_open",
        },
        {
            "id": "chat_sync_failure_backoff_isolated",
            "test": "tests.test_wechat_chat_sync_loop.WeChatChatSyncLoopTests.test_sync_once_backs_off_retryable_failure_without_blocking_other_chats",
        },
        {
            "id": "story_route_unified_research_device",
            "test": direct_prefix + "test_plain_story_generation_routes_to_story_worker_for_research_and_device_chats",
        },
        {
            "id": "book_search_guarded_route",
            "test": direct_prefix + "test_book_search_routes_to_guarded_books_worker",
        },
        {
            "id": "pocketpolyglot_durable_route",
            "test": direct_prefix + "test_pocketpolyglot_progress_routes_to_interruptible_project",
        },
        {
            "id": "cross_repo_feedback_allowlisted_route",
            "test": direct_prefix + "test_explicit_lazyedit_bug_report_routes_to_feedback_worker",
        },
        {
            "id": "cross_repo_feedback_transient_rejected",
            "test": worker_prefix + "test_worker_skips_unverified_or_transient_feedback",
        },
        {
            "id": "all_chats_shared_backend_routes",
            "test": direct_prefix + "test_all_monitored_chats_share_backend_route_skills_for_explicit_work",
        },
        {
            "id": "echomind_backend_instruction_route",
            "test": direct_prefix + "test_echomind_routes_explicit_backend_requests",
        },
        {
            "id": "backend_session_metadata_not_chat",
            "test": backend_prefix + "test_metadata_only_aginti_fallback_is_rejected",
        },
        {
            "id": "memo_chat_route_reply_direct",
            "test": direct_prefix + "test_run_once_uses_agent_route_chat_reply_without_second_agent_or_worker",
        },
        {
            "id": "fast_evidence_refusal_not_chat",
            "test": backend_prefix + "test_fast_role_rejects_generic_aginti_execution_evidence_refusal",
        },
        {
            "id": "aginti_backend_specific_prompt",
            "test": backend_prefix + "test_backend_specific_prompt_replaces_oversized_codex_prompt_for_aginti",
        },
        {
            "id": "aginti_provider_fallback_pre_inference_only",
            "test": backend_prefix + "test_aginti_retries_explicit_provider_only_for_pre_inference_failure",
        },
        {
            "id": "aginti_wecom_provider_environment",
            "test": backend_prefix + "test_aginti_shared_backend_reads_wecom_provider_environment",
        },
        {
            "id": "aginti_worker_packet_bounded",
            "test": worker_prefix + "test_aginti_worker_prompt_is_bounded_and_uses_one_selected_routine",
        },
        {
            "id": "backend_failure_diagnostics_not_chat",
            "test": worker_prefix + "test_backend_diagnostics_are_private_no_reply_results",
        },
        {
            "id": "backend_failure_safe_terminal_receipt",
            "test": worker_prefix + "test_process_one_sends_one_safe_terminal_feedback_for_interactive_failure",
        },
        {
            "id": "legacy_wecom_transport_not_personal_wechat",
            "test": "tests.test_wecom_agent_bridge.WeComAgentBridgeTests.test_legacy_wecom_chat_prefix_cannot_fall_back_to_personal_wechat",
        },
        {
            "id": "exact_local_id_backfill",
            "test": direct_prefix + "test_force_local_id_selects_only_exact_row_and_restores_cursor",
        },
        {
            "id": "route_agent_reuses_session",
            "test": direct_prefix + "test_agent_first_route_can_enqueue_without_keyword_prefilter",
        },
        {
            "id": "generation_upload_not_public_publish",
            "test": direct_prefix + "test_publish_route_false_positive_restores_generation_when_upload_is_reference_assets",
        },
        {
            "id": "generated_video_stage_boundary",
            "test": worker_prefix + "test_generated_video_stage_permissions_are_current_request_only",
        },
        {
            "id": "stored_result_contract_repair_no_rerun",
            "test": worker_prefix + "test_repair_stored_result_reapplies_contract_without_agent_and_sends_once",
        },
        {
            "id": "generated_video_lazyedit_uses_context_prompts",
            "test": worker_prefix + "test_generated_video_lazyedit_command_prefers_preflight_context_prompts",
        },
        {
            "id": "generated_video_lazyedit_appends_story_prompt",
            "test": worker_prefix + "test_generated_video_lazyedit_context_appends_generated_story_and_prompt",
        },
        {
            "id": "mp_weixin_read_only_recovery",
            "test": "tests.test_wechat_source_recovery.WeChatSourceRecoveryTests.test_recover_article_uses_wechat_fetch_and_private_cache",
        },
        {
            "id": "android_mp_weixin_exact_card_recovery",
            "test": "tests.test_wechat_android_source_recovery.WechatAndroidSourceRecoveryTests.test_article_recovery_uses_copied_canonical_link_and_exact_title",
        },
        {
            "id": "group_voice_queue_context",
            "test": direct_prefix + "test_voice_queue_fields_preserve_transcript_without_private_payload",
        },
        {
            "id": "encoded_card_media_resolution",
            "test": worker_prefix + "test_encoded_file_card_type_still_runs_media_resolution",
        },
        {
            "id": "group_media_audio_intake",
            "test": worker_prefix + "test_local_group_video_audio_is_handed_to_reusable_transcriber",
        },
        {
            "id": "local_audio_pipeline_context",
            "test": "tests.test_wechat_audio_intake.WeChatAudioIntakeTests.test_pipeline_writes_source_scoped_agent_context_and_reuses_cache",
        },
        {
            "id": "group_audio_reaches_resumed_agent",
            "test": worker_prefix + "test_resumed_worker_prompt_requires_audio_context_before_reasoning",
        },
        {
            "id": "shipinhao_exact_comment_discovery",
            "test": worker_prefix + "test_shipinhao_comment_preflight_auto_discovers_only_exact_export",
        },
        {
            "id": "shipinhao_exact_media_transcript",
            "test": worker_prefix + "test_shipinhao_media_transcript_preflight_uses_exact_card_context",
        },
        {
            "id": "shipinhao_capture_manifest_identity",
            "test": "tests.test_shipinhao_media_transcribe.ShipinhaoMediaTranscribeTests.test_verified_capture_manifest_is_source_scoped_and_hash_checked",
        },
        {
            "id": "shipinhao_pipewire_stream_scope",
            "test": "tests.test_shipinhao_gui_audio_capture.ShipinhaoGuiAudioCaptureTests.test_pipewire_stream_is_limited_to_wechat_and_display",
        },
        {
            "id": "shipinhao_download_failure_not_silent",
            "test": "tests.test_shipinhao_media_transcribe.ShipinhaoMediaTranscribeTests.test_download_failure_is_not_reported_as_no_audio",
        },
        {
            "id": "shipinhao_failure_context_not_silent",
            "test": worker_prefix + "test_failed_shipinhao_acquisition_creates_not_silent_agent_context",
        },
        {
            "id": "shipinhao_failure_alias_not_silent",
            "test": worker_prefix + "test_finder_audio_alias_cannot_turn_failed_download_into_silence",
        },
        {
            "id": "shipinhao_public_mirror_content_identity",
            "test": "tests.test_shipinhao_media_transcribe.ShipinhaoMediaTranscribeTests.test_public_mirror_match_requires_duration_and_content_evidence",
        },
        {
            "id": "shipinhao_public_excerpt_false_positive_guard",
            "test": "tests.test_shipinhao_media_transcribe.ShipinhaoMediaTranscribeTests.test_paraphrase_match_rejects_related_but_wrong_speaker_clip",
        },
        {
            "id": "shipinhao_worker_bounded_task_packet",
            "test": worker_prefix + "test_worker_agent_packet_keeps_context_paths_but_drops_raw_finder_secrets",
        },
        {
            "id": "shipinhao_card_local_identity_binding",
            "test": "tests.test_shipinhao_gui_audio_capture.ShipinhaoGuiAudioCaptureTests.test_play_control_is_bound_only_to_nearby_card_identity",
        },
        {
            "id": "shipinhao_exact_cover_identity_binding",
            "test": "tests.test_shipinhao_gui_audio_capture.ShipinhaoGuiAudioCaptureTests.test_exact_cover_match_returns_source_scoped_click_target",
        },
        {
            "id": "android_shipinhao_exact_chat_after_audio_prewarm",
            "test": "tests.test_wechat_android_source_recovery.WechatAndroidSourceRecoveryTests.test_shipinhao_reenters_exact_chat_after_audio_helper_prewarm",
        },
        {
            "id": "read_only_source_never_waits_for_verification",
            "test": worker_prefix + "test_read_only_source_recovery_drops_verification_confirmation",
        },
        {
            "id": "document_intake_reaches_agent_context",
            "test": worker_prefix + "test_file_intake_docx_falls_through_to_resumed_agent_with_readable_context",
        },
        {
            "id": "outbound_file_echo_suppressed",
            "test": direct_prefix + "test_recorded_outbound_file_is_not_reprocessed_as_a_self_attachment",
        },
        {
            "id": "bare_file_intake_source_not_resent",
            "test": worker_prefix + "test_bare_file_intake_does_not_send_the_uploaded_source_back",
        },
        {
            "id": "legacy_task_instruction_contract_backfill",
            "test": worker_prefix + "test_worker_backfills_instruction_contract_for_legacy_task",
        },
        {
            "id": "transport_pdf_policy_not_user_request",
            "test": audit_prefix + "test_completion_model_cannot_invent_pdf_artifact_requirement",
        },
        {
            "id": "numbered_interruption_ledger_unbounded",
            "test": worker_prefix + "test_merge_keeps_more_than_twenty_numbered_interruptions",
        },
        {
            "id": "numbered_completion_repairs_pdf",
            "test": worker_prefix + "test_completion_audit_repairs_missing_pdf_in_same_worker_session",
        },
        {
            "id": "numbered_completion_recovers_exact_task_pdf",
            "test": worker_prefix + "test_completion_audit_recovers_exact_task_pdf_before_agent_repair",
        },
        {
            "id": "legacy_numbered_row_recovered",
            "test": worker_prefix + "test_legacy_child_absent_from_parent_ledger_is_requeued",
        },
        {
            "id": "supervisor_uses_guarded_worker_entrypoint",
            "test": worker_prefix + "test_supervisor_worker_uses_guarded_selftest_entrypoint",
        },
    ]


def cmd_init_config(args: argparse.Namespace) -> int:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    chat_config = {
        "chat_name": args.chat,
        "display": args.display,
        "poll_seconds": 8,
        "reply_enabled": True,
        "respond_to_all": False,
        "trigger_prefixes": ["@lachchen", "＠lachchen", "@codex", "codex:"],
        "max_reply_chars": 1200,
        "state_path": str(PRIVATE / f"{safe_slug(args.chat)}-chatops.state.json"),
        "db_path": str(PRIVATE / "wechat_mirror.sqlite"),
        "output_dir": str(PACKAGE_ROOT / "output" / "wechat_gui_agent" / datetime.now().strftime("%F")),
        "agent_backend": args.agent_backend,
        "aginti": {
            "provider_chain": ["deepseek", "localllm"],
            "provider_models": {"deepseek": "deepseek-v4-flash", "localllm": "localllm-fast"},
            "provider_models_by_effort": {
                "localllm": {
                    "low": "localllm-fast",
                    "medium": "localllm-deep",
                    "high": "localllm-deep",
                    "xhigh": "localllm-deep",
                }
            },
            "machine_mode": True,
            "task_profile": "chatops",
            "timeout_seconds": 3600,
        },
        "claude": {"model": "", "timeout_seconds": 60, "permission_mode": "bypassPermissions"},
        "codex": {"model": "gpt-5.5", "reasoning_effort": "medium", "sandbox": "read-only", "workdir": str(PACKAGE_ROOT), "timeout_seconds": 60},
    }
    direct_config = {
        "chat_name": args.chat,
        "chatroom_id": args.chatroom_id,
        "message_table": args.message_table,
        "self_wxid": args.self_wxid,
        "trigger_prefixes": ["@lachchen", "＠lachchen", "@codex", "codex:"],
        "mirror_db": str(PRIVATE / "wechat_mirror.sqlite"),
        "max_reply_chars": 1200,
        "respond_to_all": False,
        "respond_to_self": False,
        "ignore_self_messages": True,
        "allow_human_self_messages": False,
        "self_message_policy": "ignore",
        "self_messages_text_only": True,
        "ignore_probable_bot_self_replies": True,
        "trigger_local_types": [1],
        "chat_purpose": "research",
        "analysis_mode": "",
        "silent_danger_enabled": True,
        "codex_session_reuse": True,
        "agent_route_enabled": True,
        "agent_route_prefilter": "agent_first",
        "agent_router": {
            "default_model": "gpt-5.3-codex-spark",
            "default_reasoning_effort": "medium",
            "risky_model": "gpt-5.5",
            "risky_reasoning_effort": "medium",
            "sandbox": "read-only",
            "timeout_seconds": 45,
            "reuse_session": True,
        },
        "auto_media_sync_on_task": True,
        "media_sync_since_minutes": 180,
        "media_sync_context_window_seconds": 300,
        "media_sync_timeout_seconds": 20,
        "immediate_route_enabled": True,
        "immediate_ack_enabled": True,
        "dynamic_ack_enabled": True,
        "dynamic_ack_max_chars": 96,
        "immediate_ack_text": "收到，我先处理，完成后把结果发回来。",
        "slow_task_keywords": ["download", "pdf", "paper", "论文", "下載", "下载", "render", "cad", "pcb", "aginti", "imagegen", "image generation", "kicad", "gerber", "step", "stl", "3d", "labcanvas", "overview", "figure", "figure grid", "icons", "file", "image", "presentation", "powerpoint", "ppt", "pptx", "slides", "slide deck", "演示文稿", "幻灯片"],
        "poll_seconds": 0.8,
        "catchup_poll_seconds": 0.1,
        "agent_backend": args.agent_backend,
        "aginti": {
            "provider_chain": ["deepseek", "localllm"],
            "provider_models": {"deepseek": "deepseek-v4-flash", "localllm": "localllm-fast"},
            "provider_models_by_effort": {
                "localllm": {
                    "low": "localllm-fast",
                    "medium": "localllm-deep",
                    "high": "localllm-deep",
                    "xhigh": "localllm-deep",
                }
            },
            "machine_mode": True,
            "task_profile": "chatops",
            "timeout_seconds": 3600,
        },
        "claude": {"model": "", "timeout_seconds": 60, "permission_mode": "bypassPermissions"},
        "codex": {"model": "gpt-5.5", "reasoning_effort": "medium", "sandbox": "read-only", "timeout_seconds": 60},
    }
    written = []
    for path, data in ((DEFAULT_CHAT_CONFIG, chat_config), (DEFAULT_DIRECT_CONFIG, direct_config)):
        if path.exists() and not args.force:
            continue
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        written.append(str(path))
    print_payload({"ok": True, "written": written}, args.json, "wechat init-config: " + (", ".join(written) or "kept existing files"))
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    if args.action == "start":
        proc = run_command([str(SCRIPTS / "wechat_virtual_desktop.sh")], capture=True)
        if getattr(args, "json", False):
            print(json.dumps({"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr, "desktop": desktop_status()}, indent=2))
        else:
            print(proc.stdout, end="")
            if proc.stderr:
                print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode
    if args.action == "keep-awake":
        root = PACKAGE_ROOT
        log_dir = root / "output" / "virtual_desktop" / datetime.now().strftime("%F")
        pid_file = root / "output" / "virtual_desktop" / "wechat_97_keep_awake.pid"
        log_file = log_dir / "wechat_keep_awake.log"
        proc = run_command(
            [
                str(root / "agentic_tools" / "virtual_desktop" / "start_keep_awake.sh"),
                "--display",
                DEFAULT_DISPLAY,
                "--interval",
                str(args.interval),
                "--pid-file",
                str(pid_file),
                "--log-file",
                str(log_file),
            ],
            capture=True,
        )
        payload = {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr, "desktop": desktop_status()}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(proc.stdout, end="")
            if proc.stderr:
                print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode
    payload = desktop_status()
    print_payload(payload, getattr(args, "json", False), f"wechat desktop: {payload['status']} {payload.get('novnc_url', '')}")
    return 0


def cmd_browser_assist(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_browser_assist.py"),
        "--url",
        args.url,
        "--display",
        args.display,
    ]
    if args.browser:
        command += ["--browser", args.browser]
    if getattr(args, "wait_seconds", 0.0):
        command += ["--wait-seconds", str(args.wait_seconds)]
    if getattr(args, "reuse_window", False):
        command.append("--reuse-window")
    if getattr(args, "capture", False):
        command.append("--capture")
    if getattr(args, "wait_readable_seconds", 0.0):
        command += ["--wait-readable-seconds", str(args.wait_readable_seconds)]
    if getattr(args, "poll_seconds", 3.0) != 3.0:
        command += ["--poll-seconds", str(args.poll_seconds)]
    if getattr(args, "close_after", False):
        command.append("--close-after")
    if getattr(args, "output_dir", None):
        command += ["--output-dir", str(args.output_dir)]
    if getattr(args, "allow_mp_weixin", False):
        command.append("--allow-mp-weixin")
    if args.dry_run:
        command.append("--dry-run")
    if args.json:
        command.append("--json")
    proc = run_command(command, capture=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return proc.returncode


def cmd_unlock_watchdog(args: argparse.Namespace) -> int:
    session = "labcanvas-wechat-unlock-watchdog"
    if args.action == "status":
        standalone = tmux_status(session)
        supervisor = tmux_window_status("labcanvas-wechat", "unlock-watchdog")
        payload = {
            "running": standalone["running"] or supervisor["running"],
            "status": "running" if standalone["running"] or supervisor["running"] else "not-running",
            "standalone": standalone,
            "supervisor": supervisor,
        }
        print_payload(payload, getattr(args, "json", False), f"wechat unlock-watchdog: {payload['status']}")
        return 0 if payload["running"] else 1
    if args.action == "stop":
        stopped = kill_tmux(session)
        supervisor_stopped = kill_tmux_window("labcanvas-wechat", "unlock-watchdog")
        payload = {"ok": True, "stopped": stopped, "supervisor_window_stopped": supervisor_stopped, "session": session}
        print_payload(payload, getattr(args, "json", False), "stopped" if stopped else "not running")
        return 0
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_desktop_unlock_watchdog.py"),
        "--display",
        args.display,
        "--interval",
        str(args.interval),
    ]
    if args.serial:
        command += ["--serial", args.serial]
    if args.flush_deferred:
        command.append("--flush-deferred")
    if args.dry_run:
        command.append("--dry-run")
    if args.action == "once":
        proc = run_command(command, capture=True)
        if getattr(args, "json", False):
            try:
                print(json.dumps(json.loads(proc.stdout), ensure_ascii=False, indent=2))
            except json.JSONDecodeError:
                print(json.dumps({"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr}, ensure_ascii=False, indent=2))
        else:
            print(proc.stdout, end="")
            if proc.stderr:
                print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode
    if args.action == "restart":
        kill_tmux(session)
    if tmux_status(session)["running"]:
        payload = tmux_status(session)
        print_payload(payload, getattr(args, "json", False), f"wechat unlock-watchdog: {payload['status']}")
        return 0
    command.append("--loop")
    command_text = " ".join(shlex.quote(part) for part in command)
    log_dir = PACKAGE_ROOT / "output" / "wechat_gui_agent" / datetime.now().strftime("%F")
    log_dir.mkdir(parents=True, exist_ok=True)
    tmux_command = f"cd {shlex.quote(str(PACKAGE_ROOT))} && {command_text} >> {shlex.quote(str(log_dir / 'unlock-watchdog.log'))} 2>&1"
    proc = run_command(["tmux", "new-session", "-d", "-s", session, tmux_command], capture=True)
    payload = {"ok": proc.returncode == 0, "session": session, "log": str(log_dir / "unlock-watchdog.log"), "status": tmux_status(session)}
    print_payload(payload, getattr(args, "json", False), f"started {session}")
    return proc.returncode


def cmd_monitor(args: argparse.Namespace) -> int:
    if args.action in {"start", "restart"}:
        if args.action == "restart":
            kill_tmux("labcanvas-wechat-direct-chatops")
        proc = run_command([str(SCRIPTS / "wechat_direct_chatops_tmux.sh"), str(args.config)], capture=True)
        print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode
    if args.action == "stop":
        stopped = kill_tmux("labcanvas-wechat-direct-chatops")
        print("stopped" if stopped else "not running")
        return 0
    if args.action == "once":
        command = [sys.executable, str(SCRIPTS / "wechat_direct_chatops.py"), "--config", str(args.config), "--worker-queue", str(args.queue)]
        if args.send:
            command.append("--send")
        return run_command(command, capture=False).returncode
    payload = tmux_status("labcanvas-wechat-direct-chatops")
    print_payload(payload, args.json, f"wechat monitor: {payload['status']}")
    return 0 if payload["running"] else 1


def cmd_hold(args: argparse.Namespace) -> int:
    proc = run_command([str(SCRIPTS / "wechat_supervisor_tmux.sh"), args.action], capture=True)
    if getattr(args, "json", False):
        print(json.dumps({"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr, "status": status_payload()}, indent=2))
    else:
        print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
    return proc.returncode


def cmd_stack(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    env["WECHAT_WEB_PORT"] = str(args.web_port)
    env["WECHAT_WEB_SESSION"] = args.web_session
    env["WECHAT_CAREER_SESSION"] = args.career_session
    env["WECHAT_CAREER_SEND_CHAT"] = args.career_send_chat
    env["WECHAT_CAREER_MORNING_TIME"] = args.career_morning_time
    env["WECHAT_STACK_START_CAREER"] = "0" if args.no_career else "1"
    proc = run_command([str(SCRIPTS / "wechat_stack_tmux.sh"), args.action], capture=True, env=env)
    web_status = labcanvas_web_status(args.web_session)
    career_status = tmux_status(args.career_session)
    payload = {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "wechat": status_payload(),
        "webapp": web_status,
        "career": career_status,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, file=sys.stderr, end="")
        if web_status.get("url"):
            print(f"webapp: {web_status['url']}")
        print(f"career: {career_status['status']} ({args.career_session})")
    return proc.returncode


def cmd_send(args: argparse.Namespace) -> int:
    if not args.message and not args.file:
        raise SystemExit("Use --message or --file")
    if args.file:
        if args.chat or args.target or args.dry_run or getattr(args, "json", False):
            raise SystemExit("Target-scoped file send is not exposed by this API yet; use worker artifact delivery or current-chat bridge.")
        command = [sys.executable, str(SCRIPTS / "wechat_chatops_bridge.py"), "--config", str(args.config), "--file", str(args.file)]
        return run_command(command, capture=False).returncode
    payload = send_wechat_message_api(
        message=str(args.message or ""),
        chat=args.chat,
        target_name=args.target,
        send_targets=args.send_targets,
        config=args.config,
        display=args.display,
        dry_run=args.dry_run,
        allow_search=True if args.allow_search else None,
    )
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if payload.get("ok"):
            status = "dry-run" if payload.get("dry_run") else "sent"
            print(f"wechat send: {status} to {payload.get('chat') or (payload.get('target') or {}).get('name')}")
        else:
            print(f"wechat send failed: {payload.get('error') or payload.get('stderr_tail')}", file=sys.stderr)
    return 0 if payload.get("ok") else 1


def cmd_worker(args: argparse.Namespace) -> int:
    command = [sys.executable, str(SCRIPTS / "wechat_task_worker.py"), "--queue", str(args.queue), "--chat", args.chat]
    if args.action == "enqueue":
        command += ["--enqueue", " ".join(args.request).strip()]
    elif args.action == "once":
        command.append("--once")
    elif args.action == "loop":
        command.append("--loop")
    elif args.action == "repair-artifacts":
        command.append("--repair-missing-artifacts")
    elif args.action == "repair-result":
        if not args.request:
            raise SystemExit("Use: labcanvas wechat worker repair-result <task_id> [--send]")
        command += ["--repair-stored-result", args.request[0]]
    elif args.action == "reprocess":
        if not args.request:
            raise SystemExit("Use: labcanvas wechat worker reprocess <task_id> [reason]")
        command += ["--reprocess", args.request[0]]
        reason = " ".join(args.request[1:]).strip()
        if reason:
            command += ["--reason", reason]
        if args.artifact_recovery_only:
            command.append("--artifact-recovery-only")
    if args.send:
        command.append("--send")
    return run_command(command, capture=False).returncode


def cmd_queue(args: argparse.Namespace) -> int:
    payload = queue_summary(args.queue, limit=args.limit)
    attention = payload.get("attention") or {}
    attention_counts = attention.get("counts") or {}
    summary = (
        f"queue: pending={payload['counts'].get('pending', 0)}"
        f" active={attention_counts.get('active', 0)}"
        f" blocked={attention_counts.get('delivery_blocked', 0)}"
        f" waiting={attention_counts.get('human_blocked', 0)}"
        f" failed={attention_counts.get('failed', 0)}"
        f" total={payload['total']}"
    )
    if attention.get("needs_attention"):
        summary += " (attention needed)"
    print_payload(payload, args.json, summary)
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    command = [sys.executable, str(SCRIPTS / "wechat_memory.py"), args.action, "--db", str(args.db)]
    if args.chat:
        command += ["--chat", args.chat]
    if getattr(args, "json", False):
        command.append("--json")
    proc = run_command(command, capture=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return proc.returncode


def cmd_career_agent(args: argparse.Namespace) -> int:
    session = str(args.session)
    if args.action == "status":
        payload = tmux_status(session)
        print_payload(payload, getattr(args, "json", False), f"{session}: {payload['status']}")
        return 0 if payload.get("running") else 1
    if args.action == "stop":
        stopped = kill_tmux(session)
        payload = {"ok": True, "session": session, "stopped": stopped, "status": tmux_status(session)}
        print_payload(payload, getattr(args, "json", False), f"stopped {session}" if stopped else f"{session} was not running")
        return 0
    start_action = args.action
    if args.action == "restart":
        kill_tmux(session)
        start_action = "start"
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_career_daily_agent.py"),
        (
            "loop"
            if start_action == "start"
            else (
                "organize"
                if start_action == "organize"
                else ("catch-up" if start_action == "catch-up" else "run")
            )
        ),
        "--send-chat",
        args.send_chat,
        "--morning-time",
        args.morning_time,
        "--model",
        args.model,
        "--reasoning-effort",
        args.reasoning_effort,
        "--organize-chat",
        args.organize_chat,
    ]
    for chat in args.chat:
        command += ["--chat", chat]
    if args.send:
        command.append("--send")
    if args.attach_report:
        command.append("--attach-report")
    if args.organize_report:
        command.append("--organize-report")
    if args.force_organize:
        command.append("--force-organize")
    if start_action in {"once", "organize", "catch-up"}:
        if getattr(args, "json", False):
            command.append("--json")
        return run_command(command, capture=False).returncode
    if tmux_status(session)["running"]:
        payload = tmux_status(session)
        print_payload(payload, getattr(args, "json", False), f"{session} already running")
        return 0
    log_dir = PRIVATE / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    quoted = " ".join(shlex.quote(part) for part in command)
    tmux_command = f"cd {shlex.quote(str(PACKAGE_ROOT))} && {quoted} >> {shlex.quote(str(log_dir / 'career-daily.log'))} 2>&1"
    proc = run_command(["tmux", "new-session", "-d", "-s", session, tmux_command], capture=True)
    payload = {
        "ok": proc.returncode == 0,
        "session": session,
        "log": str(log_dir / "career-daily.log"),
        "status": tmux_status(session),
        "stderr": proc.stderr,
    }
    print_payload(payload, getattr(args, "json", False), f"started {session}" if proc.returncode == 0 else f"failed to start {session}")
    return proc.returncode


def cmd_approve(args: argparse.Namespace) -> int:
    task = update_waiting_task(args.queue, args.task_id, decision="approve", note=args.note)
    print_payload({"ok": True, "task": task}, args.json, f"approved task: {task['id']}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    task = update_waiting_task(args.queue, args.task_id, decision="reject", note=args.note)
    print_payload({"ok": True, "task": task}, args.json, f"rejected task: {task['id']}")
    return 0


def cmd_media_sync(args: argparse.Namespace) -> int:
    command = [sys.executable, str(SCRIPTS / "wechat_media_sync.py"), "--chat", args.chat, "--since-minutes", str(args.since_minutes)]
    if args.since_epoch is not None:
        command += ["--since-epoch", str(args.since_epoch)]
    if args.until_epoch is not None:
        command += ["--until-epoch", str(args.until_epoch)]
    for token in args.match_token:
        command += ["--match-token", str(token)]
    for source in args.source:
        command += ["--source", str(source)]
    if args.auto_source:
        command.append("--auto-source")
    if args.dry_run:
        command.append("--dry-run")
    if args.summary_only:
        command.append("--summary-only")
    if args.record_empty:
        command.append("--record-empty")
    return run_command(command, capture=False).returncode


def cmd_voice_transcribe(args: argparse.Namespace) -> int:
    command = [
        str(args.python),
        str(SCRIPTS / "wechat_voice_transcribe.py"),
        "--config",
        str(args.config),
        "--local-id",
        str(args.local_id),
        "--model",
        str(args.model),
        "--backend",
        str(args.backend),
    ]
    if args.refresh:
        command.append("--refresh")
    if getattr(args, "json", False):
        command.append("--json")
    return run_command(command, capture=False).returncode


def cmd_audio_intake(args: argparse.Namespace) -> int:
    command = [
        str(args.python),
        str(SCRIPTS / "wechat_audio_intake.py"),
        "--input",
        str(args.input),
        "--output-dir",
        str(args.output_dir),
        "--model",
        str(args.model),
        "--backend",
        str(args.backend),
    ]
    if args.source_local_id is not None:
        command += ["--source-local-id", str(args.source_local_id)]
    if args.refresh:
        command.append("--refresh")
    if getattr(args, "json", False):
        command.append("--json")
    return run_command(command, capture=False).returncode


def cmd_shipinhao_transcribe(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "shipinhao_media_transcribe.py"),
        "--source-text-file",
        str(args.source_text_file),
        "--output-dir",
        str(args.output_dir),
        "--cache-root",
        str(args.cache_root),
        "--model",
        str(args.model),
        "--device",
        str(args.device),
        "--language",
        str(args.language),
        "--max-duration",
        str(max(1.0, float(args.max_duration))),
    ]
    if args.capture_manifest:
        command += ["--capture-manifest", str(args.capture_manifest)]
    for hint in args.search_hint:
        command += ["--search-hint", str(hint)]
    if args.no_public_mirror_recovery:
        command.append("--no-public-mirror-recovery")
    if getattr(args, "json", False):
        command.append("--json")
    return run_command(command, capture=False).returncode


def cmd_autopublish_video(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_autopublish_video.py"),
        "--db",
        str(args.db),
        "--dest",
        str(args.dest),
        "--since-minutes",
        str(args.since_minutes),
        "--limit",
        str(args.limit),
    ]
    for chat in args.chat:
        command += ["--chat", str(chat)]
    if args.source:
        command += ["--source", str(args.source)]
    if args.title:
        command += ["--title", args.title]
    for token in args.match_token:
        command += ["--match-token", str(token)]
    for local_id in args.message_local_id:
        command += ["--message-local-id", str(local_id)]
    for message_ref in args.message_ref:
        command += ["--message-ref", str(message_ref)]
    if args.sync:
        command.append("--sync")
    if args.fetch_gui:
        command.append("--fetch-gui")
        command += ["--fetch-timeout", str(args.fetch_timeout), "--display", args.display]
    if args.video_click:
        command += ["--video-click", args.video_click]
    if args.no_auto_source:
        command.append("--no-auto-source")
    if args.replace:
        command.append("--replace")
    if args.list:
        command.append("--list")
    if args.dry_run:
        command.append("--dry-run")
    if getattr(args, "json", False):
        command.append("--json")
    return run_command(command, capture=False).returncode


def cmd_native_save_video(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_android_native_video_save.py"),
        "--target",
        args.target,
        "--task-id",
        args.task_id,
        "--output-dir",
        str(args.output_dir),
        "--filename",
        args.filename,
        "--video-tap",
        args.video_tap,
        "--older-pages",
        str(args.older_pages),
        "--expected-duration-seconds",
        str(args.expected_duration_seconds),
        "--duration-tolerance-seconds",
        str(args.duration_tolerance_seconds),
        "--expected-original-size-mb",
        str(args.expected_original_size_mb),
        "--original-download-timeout",
        str(args.original_download_timeout),
        "--album-export-timeout",
        str(args.album_export_timeout),
    ]
    if args.serial:
        command += ["--serial", args.serial]
    if args.adb:
        command += ["--adb", args.adb]
    if args.targets_file:
        command += ["--targets-file", str(args.targets_file)]
    if getattr(args, "json", False):
        command.append("--json")
    return run_command(command, capture=False).returncode


def cmd_backend(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_direct_backend.py"),
        "--chat",
        args.chat,
        "--external",
        str(args.external),
    ]
    if getattr(args, "json", False):
        command.append("--json")
    if args.db_dir:
        command += ["--db-dir", str(args.db_dir)]
    if args.action == "install":
        command += ["install", "--repo", args.repo]
        if args.update:
            command.append("--update")
        if args.skip_deps:
            command.append("--skip-deps")
    elif args.action == "decrypt":
        command.append("decrypt")
        if args.incremental:
            command.append("--incremental")
        if args.dry_run:
            command.append("--dry-run")
    elif args.action == "monitor-web":
        command += ["monitor-web", "--host", args.host, "--port", str(args.port)]
    elif args.action == "api-history":
        command += ["api-history", "--port", str(args.port), "--limit", str(args.limit), "--since", str(args.since)]
        if args.filter_chat:
            command += ["--chat", args.filter_chat]
        if args.raw:
            command.append("--raw")
    else:
        command.append(args.action)
    proc = run_command(command, capture=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return proc.returncode


def cmd_rename(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_group_admin.py"),
        "--display",
        args.display,
        "--chat",
        args.chat,
        "--rename",
        args.name,
    ]
    if args.dry_run:
        command.append("--dry-run")
    return run_command(command, capture=False).returncode


def cmd_alias(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_group_admin.py"),
        "--display",
        args.display,
        "--chat",
        args.chat,
        "--my-alias",
        args.name,
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.skip_ocr_guard:
        command.append("--skip-ocr-guard")
    return run_command(command, capture=False).returncode


def cmd_create_group(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(SCRIPTS / "wechat_group_create.py"),
        "--display",
        args.display,
    ]
    if args.plan:
        command += ["--plan", str(args.plan)]
    for query in args.member_query:
        command += ["--member-query", query]
    if args.search_box:
        command += ["--search-box", args.search_box]
    if args.search_result_checkbox:
        command += ["--search-result-checkbox", args.search_result_checkbox]
    if args.create:
        command.append("--create")
    return run_command(command, capture=False).returncode


def cmd_install_user_scripts(args: argparse.Namespace) -> int:
    scripts_dir = Path.home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    wrapper = scripts_dir / "labcanvas-wechat-hold.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd " + shlex.quote(str(PACKAGE_ROOT)) + "\n"
        "if command -v labcanvas >/dev/null 2>&1; then\n"
        "  exec labcanvas wechat hold \"${1:-start}\"\n"
        "fi\n"
        "export PYTHONPATH=" + shlex.quote(str(PACKAGE_ROOT / "src")) + ":${PYTHONPATH:-}\n"
        "exec python3 -m agenticapp wechat hold \"${1:-start}\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    create_wrapper = scripts_dir / "create-labcanvas-wechat-tmux.sh"
    create_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd " + shlex.quote(str(PACKAGE_ROOT)) + "\n"
        "export WECHAT_SUPERVISOR_SESSION=${WECHAT_SUPERVISOR_SESSION:-labcanvas-wechat}\n"
        "export WECHAT_MEDIA_SYNC_INTERVAL=${WECHAT_MEDIA_SYNC_INTERVAL:-30}\n"
        "export PYTHONPATH=" + shlex.quote(str(PACKAGE_ROOT / "src")) + ":${PYTHONPATH:-}\n"
        "exec python3 -m agenticapp wechat hold start\n",
        encoding="utf-8",
    )
    create_wrapper.chmod(0o755)
    stack_wrapper = scripts_dir / "create-labcanvas-wechat-stack.sh"
    stack_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd " + shlex.quote(str(PACKAGE_ROOT)) + "\n"
        "export WECHAT_SUPERVISOR_SESSION=${WECHAT_SUPERVISOR_SESSION:-labcanvas-wechat}\n"
        "export WECHAT_WEB_SESSION=${WECHAT_WEB_SESSION:-labcanvas-web}\n"
        "export WECHAT_WEB_PORT=${WECHAT_WEB_PORT:-19474}\n"
        "export WECHAT_CAREER_SESSION=${WECHAT_CAREER_SESSION:-labcanvas-career-daily}\n"
        "export WECHAT_CAREER_SEND_CHAT=${WECHAT_CAREER_SEND_CHAT:-lachlanchan}\n"
        "export WECHAT_CAREER_ORGANIZE_CHAT=${WECHAT_CAREER_ORGANIZE_CHAT:-MEMO写作—外语—挣钱}\n"
        "export WECHAT_CAREER_MORNING_TIME=${WECHAT_CAREER_MORNING_TIME:-08:30}\n"
        "export WECHAT_CAREER_AGENT_MODEL=${WECHAT_CAREER_AGENT_MODEL:-gpt-5.5}\n"
        "export WECHAT_CAREER_AGENT_EFFORT=${WECHAT_CAREER_AGENT_EFFORT:-medium}\n"
        "export WECHAT_MARKDOWN_PDF_LANGUAGES=${WECHAT_MARKDOWN_PDF_LANGUAGES:-zh,en}\n"
        "export WECHAT_MARKDOWN_PDF_PANDOC=${WECHAT_MARKDOWN_PDF_PANDOC:-$HOME/miniconda3/bin/pandoc}\n"
        "export WECHAT_MARKDOWN_PDF_ENGINE=${WECHAT_MARKDOWN_PDF_ENGINE:-xelatex}\n"
        "export PYTHONPATH=" + shlex.quote(str(PACKAGE_ROOT / "src")) + ":${PYTHONPATH:-}\n"
        "exec python3 -m agenticapp wechat stack \"${1:-start}\" "
        "--web-port \"$WECHAT_WEB_PORT\" "
        "--web-session \"$WECHAT_WEB_SESSION\" "
        "--career-session \"$WECHAT_CAREER_SESSION\" "
        "--career-send-chat \"$WECHAT_CAREER_SEND_CHAT\" "
        "--career-morning-time \"$WECHAT_CAREER_MORNING_TIME\"\n",
        encoding="utf-8",
    )
    stack_wrapper.chmod(0o755)
    reboot_wrapper = scripts_dir / "create-labcanvas-wechat-after-reboot.sh"
    reboot_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd " + shlex.quote(str(PACKAGE_ROOT)) + "\n"
        "export WECHAT_SUPERVISOR_SESSION=${WECHAT_SUPERVISOR_SESSION:-labcanvas-wechat}\n"
        "export WECHAT_WEB_SESSION=${WECHAT_WEB_SESSION:-labcanvas-web}\n"
        "export WECHAT_WEB_PORT=${WECHAT_WEB_PORT:-19474}\n"
        "export WECHAT_CAREER_SESSION=${WECHAT_CAREER_SESSION:-labcanvas-career-daily}\n"
        "export WECHAT_CAREER_SEND_CHAT=${WECHAT_CAREER_SEND_CHAT:-lachlanchan}\n"
        "export WECHAT_CAREER_MORNING_TIME=${WECHAT_CAREER_MORNING_TIME:-08:30}\n"
        "export WECHAT_CAREER_AGENT_MODEL=${WECHAT_CAREER_AGENT_MODEL:-gpt-5.5}\n"
        "export WECHAT_CAREER_AGENT_EFFORT=${WECHAT_CAREER_AGENT_EFFORT:-medium}\n"
        "export WECHAT_MARKDOWN_PDF_LANGUAGES=${WECHAT_MARKDOWN_PDF_LANGUAGES:-zh,en}\n"
        "export WECHAT_MARKDOWN_PDF_PANDOC=${WECHAT_MARKDOWN_PDF_PANDOC:-$HOME/miniconda3/bin/pandoc}\n"
        "export WECHAT_MARKDOWN_PDF_ENGINE=${WECHAT_MARKDOWN_PDF_ENGINE:-xelatex}\n"
        "export PYTHONPATH=" + shlex.quote(str(PACKAGE_ROOT / "src")) + ":${PYTHONPATH:-}\n"
        "ACTION=${1:-start}\n"
        "python3 -m agenticapp wechat stack \"$ACTION\" "
        "--web-port \"$WECHAT_WEB_PORT\" "
        "--web-session \"$WECHAT_WEB_SESSION\" "
        "--career-session \"$WECHAT_CAREER_SESSION\" "
        "--career-send-chat \"$WECHAT_CAREER_SEND_CHAT\" "
        "--career-morning-time \"$WECHAT_CAREER_MORNING_TIME\"\n"
        "python3 -m agenticapp wechat status --json\n"
        "python3 -m agenticapp wechat career-agent status --session \"$WECHAT_CAREER_SESSION\" --json || true\n",
        encoding="utf-8",
    )
    reboot_wrapper.chmod(0o755)
    career_wrapper = scripts_dir / "create-labcanvas-career-daily-tmux.sh"
    career_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cd " + shlex.quote(str(PACKAGE_ROOT)) + "\n"
        "export WECHAT_MARKDOWN_PDF_LANGUAGES=${WECHAT_MARKDOWN_PDF_LANGUAGES:-zh,en}\n"
        "export WECHAT_MARKDOWN_PDF_PANDOC=${WECHAT_MARKDOWN_PDF_PANDOC:-$HOME/miniconda3/bin/pandoc}\n"
        "export WECHAT_MARKDOWN_PDF_ENGINE=${WECHAT_MARKDOWN_PDF_ENGINE:-xelatex}\n"
        "export PYTHONPATH=" + shlex.quote(str(PACKAGE_ROOT / "src")) + ":${PYTHONPATH:-}\n"
        "exec python3 -m agenticapp wechat career-agent start --send --attach-report "
        "--organize-report --organize-chat \"${WECHAT_CAREER_ORGANIZE_CHAT:-MEMO写作—外语—挣钱}\" "
        "--morning-time \"${WECHAT_CAREER_MORNING_TIME:-08:30}\" "
        "--model \"${WECHAT_CAREER_AGENT_MODEL:-gpt-5.5}\" "
        "--reasoning-effort \"${WECHAT_CAREER_AGENT_EFFORT:-medium}\"\n",
        encoding="utf-8",
    )
    career_wrapper.chmod(0o755)
    print_payload(
        {"ok": True, "installed": [str(wrapper), str(create_wrapper), str(stack_wrapper), str(reboot_wrapper), str(career_wrapper)]},
        args.json,
        f"installed {wrapper}, {create_wrapper}, {stack_wrapper}, {reboot_wrapper}, and {career_wrapper}",
    )
    return 0


def update_waiting_task(path: Path, task_id: str | None, *, decision: str, note: str = "") -> dict[str, Any]:
    tasks = read_jsonl(path)
    if not tasks:
        raise ValueError(f"No task queue found at {path}")
    target_index = None
    if task_id:
        for index, task in enumerate(tasks):
            if str(task.get("id")) == task_id:
                target_index = index
                break
    else:
        for index in range(len(tasks) - 1, -1, -1):
            if tasks[index].get("status") == "waiting_confirmation":
                target_index = index
                break
    if target_index is None:
        raise ValueError("No matching waiting_confirmation task found")
    task = tasks[target_index]
    if decision == "approve":
        promote_story = approval_promotes_story_to_generated_video(task, note)
        previous_status = str(task.get("status") or "")
        previous_waiting_reason = str(task.get("waiting_reason") or "")
        now = datetime.now()
        task["status"] = "pending"
        task["approved_at"] = now.isoformat(timespec="seconds")
        task["approval_note"] = note
        task["approval_previous_status"] = previous_status
        if previous_waiting_reason:
            task["approval_from_waiting_reason"] = previous_waiting_reason
        task["expires_at"] = (
            now
            + timedelta(
                seconds=max(
                    60,
                    int(
                        os.environ.get(
                            "WECHAT_WORKER_PENDING_TASK_TTL_SECONDS",
                            "900",
                        )
                    ),
                )
            )
        ).isoformat(timespec="seconds")
        if note:
            approval_line = f"User approval note: {note}"
            request = str(task.get("request") or "").strip()
            if approval_line not in request:
                task["request"] = f"{request}\n\n{approval_line}".strip()
        publish_override = apply_publish_approval_override(task)
        reset_approved_task_execution_state(
            task,
            preserve_result=promote_story or not publish_override,
        )
        if promote_story:
            promote_story_confirmation_to_generated_video(task, note)
    elif decision == "reject":
        task["status"] = "canceled"
        task["canceled_at"] = datetime.now().isoformat(timespec="seconds")
        task["cancel_note"] = note
    else:
        raise ValueError(f"Unsupported decision: {decision}")
    tasks[target_index] = task
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in tasks), encoding="utf-8")
    return task


def apply_publish_approval_override(task: dict[str, Any]) -> bool:
    route = task.get("route_decision") if isinstance(task.get("route_decision"), dict) else {}
    waiting_reason = str(task.get("waiting_reason") or "")
    if str(route.get("route_kind") or "") != "publish_video":
        return False
    if (
        waiting_reason != "third_party_publish_consent"
        and not bool(route.get("requires_third_party_publish_confirmation"))
    ):
        return False
    updated = dict(route)
    updated.update(
        {
            "public_publish_intent": True,
            "public_publish_allowed": True,
            "external_action_allowed": True,
            "requires_third_party_publish_confirmation": False,
            "third_party_publish_confirmed": False,
            "requester_publish_override": True,
            "confirmation_kind": "direct_requester_publish",
            "reason": (
                "The current requester directly authorized publication; "
                "no third-party confirmation is required."
            ),
            "ack": "Proceed with the exact requested publication.",
        }
    )
    task["route_decision"] = updated
    consent = (
        dict(task.get("third_party_publish_consent"))
        if isinstance(task.get("third_party_publish_consent"), dict)
        else {}
    )
    consent.update(
        {
            "status": "requester_override",
            "resolution_kind": "requester_override",
            "resolved_at": task.get("approved_at"),
            "resolved_text": task.get("approval_note") or "",
        }
    )
    task["third_party_publish_consent"] = consent
    return True


def reset_approved_task_execution_state(
    task: dict[str, Any],
    *,
    preserve_result: bool,
) -> None:
    stale_fields = {
        "canceled_at",
        "claimed_at",
        "codex_session",
        "completed_at",
        "completion_audit",
        "coverage_checked_at",
        "coverage_status",
        "existing_video_publish_poststage",
        "expire_reason",
        "expired_at",
        "expired_from_status",
        "last_live_status_at",
        "message_coverage",
        "next_publish_poststage_at",
        "next_publish_poststage_at_iso",
        "orchestrator",
        "preflight",
        "publish_poststage_history",
        "publish_poststage_last_outcome",
        "publish_poststage_last_status",
        "publish_poststage_queued_at",
        "publish_poststage_wait_count",
        "rejected_at",
        "routine_contract",
        "send_claimed_at",
        "send_errors",
        "send_expires_at",
        "send_retry_claimed_at",
        "send_suppressed_at",
        "send_suppressed_reason",
        "sent_at",
        "sent_file_paths",
        "agent_session",
        "worker_error",
        "worker_id",
        "worker_policy_attempts",
        "worker_result_exhausted",
        "worker_result_ready_at",
    }
    if not preserve_result:
        stale_fields.add("result")
    for key in stale_fields:
        task.pop(key, None)
    task.pop("waiting_reason", None)


def approval_promotes_story_to_generated_video(task: dict[str, Any], note: str = "") -> bool:
    route = task.get("route_decision") if isinstance(task.get("route_decision"), dict) else {}
    routine = task.get("routine") if isinstance(task.get("routine"), dict) else {}
    project = str(route.get("project") or "").lower()
    if project != "lalachan":
        return False
    if str(route.get("route_kind") or "") not in {"story_or_script", "generate_video"}:
        return False
    if str(routine.get("id") or "") not in {"story_script_generation", "generated_video"}:
        return False
    text = "\n".join(
        [
            note,
            str((task.get("result") or {}).get("confirmation") or "") if isinstance(task.get("result"), dict) else "",
            str(task.get("request") or ""),
        ]
    ).lower()
    if any(marker in text for marker in ("do not generate", "don't generate", "dont generate", "不要生成", "别生成", "先别", "等一下")):
        return False
    confirms_story = any(marker in text for marker in ("story ok", "故事可以", "故事 ok", "可以生成", "开始生成", "generate video"))
    asks_video = any(marker in text for marker in ("generate", "video", "小云雀", "xyq", "视频", "影片"))
    return bool((confirms_story or task.get("story_confirmation_required")) and asks_video)


def promote_story_confirmation_to_generated_video(task: dict[str, Any], note: str = "") -> None:
    route = dict(task.get("route_decision") or {})
    text = f"{task.get('request') or ''}\n{note}".lower()
    publish_allowed = bool(route.get("public_publish_allowed")) and any(
        marker in text
        for marker in ("publish", "post", "shipinhao", "youtube", "instagram", "视频号", "发布", "投稿")
    )
    route.update(
        {
            "route_kind": "generate_video",
            "project": "lalachan",
            "worker_needed": True,
            "needs_recent_media": False,
            "public_publish_allowed": publish_allowed,
            "public_publish_intent": publish_allowed,
            "source_policy": route.get("source_policy") or "current_plus_explicit_refs",
            "approval_promoted_from": "story_confirmation",
        }
    )
    task["route_decision"] = route
    task["story_confirmation_required"] = False
    task["generation_blocked_until_story_confirmed"] = False
    task["confirmed_story_for_generation_at"] = datetime.now().isoformat(timespec="seconds")
    if note:
        task["confirmed_story_for_generation_note"] = note
    story_result = task.get("result") if isinstance(task.get("result"), dict) else {}
    if story_result:
        task["story_confirmation_result"] = story_result
        task["approved_story_message"] = str(story_result.get("message") or "").strip()
        task["approved_story_files"] = [str(path) for path in story_result.get("files") or [] if str(path)]
        task["approved_story_confirmation"] = str(story_result.get("confirmation") or "").strip()
    task["stage_transition"] = {
        "from": "story_script_generation",
        "to": "generated_video",
        "at": task["confirmed_story_for_generation_at"],
        "reason": "story_confirmation_approved",
        "note": note,
    }
    task["routine"] = {
        "id": "generated_video",
        "title": "Generated Video Routine",
        "task_id": task.get("id"),
        "chat": task.get("chat"),
        "source": task.get("source") if isinstance(task.get("source"), dict) else {},
        "route_kind": "generate_video",
        "project": "lalachan",
        "purpose": "Create a new video from the approved story, monitor long generation, send MP4 back, then run optional poststages.",
        "selected_at": datetime.now().isoformat(timespec="seconds"),
        "selected_by": "wechat_ops.promote_story_confirmation_to_generated_video",
    }
    for key in (
        "completed_at",
        "result",
        "send_errors",
        "sent_file_paths",
        "preflight",
        "routine_contract",
        "orchestrator",
        "worker_policy_attempts",
        "story_confirmation_gate",
    ):
        task.pop(key, None)


def parse_queue_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return datetime.fromtimestamp(float(text))
        except (OSError, OverflowError, ValueError):
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def queue_task_time(task: dict[str, Any]) -> datetime | None:
    for key in (
        "updated_at",
        "send_retry_claimed_at",
        "last_attempt_at",
        "last_checked_at",
        "claimed_at",
        "created_at",
    ):
        parsed = parse_queue_datetime(task.get(key))
        if parsed:
            return parsed
    return None


def queue_task_age_seconds(task: dict[str, Any], now: datetime) -> int | None:
    parsed = queue_task_time(task)
    if not parsed:
        return None
    return max(0, int((now - parsed).total_seconds()))


def queue_send_retry_timeout_seconds() -> int:
    raw_timeout = os.environ.get("WECHAT_WORKER_STALE_SEND_RETRY_SECONDS")
    if raw_timeout is not None:
        try:
            return max(0, int(raw_timeout))
        except ValueError:
            return 0
    try:
        send_timeout = int(os.environ.get("WECHAT_WORKER_SEND_TIMEOUT_SECONDS", "120"))
    except ValueError:
        send_timeout = 120
    return max(send_timeout + 30, 150)


def queue_status_category(status: str) -> str:
    if status in QUEUE_DELIVERY_BLOCKED_STATUSES:
        return "delivery_blocked"
    if status in QUEUE_HUMAN_BLOCKED_STATUSES:
        return "human_blocked"
    if status in QUEUE_FAILED_STATUSES:
        return "failed"
    if status in QUEUE_ACTIVE_STATUSES:
        return "active"
    if status in QUEUE_TERMINAL_STATUSES or status.startswith("canceled"):
        return "terminal"
    return "unknown"


def queue_attention_max_age_seconds() -> int:
    try:
        return max(
            0,
            int(
                os.environ.get(
                    "WECHAT_QUEUE_ATTENTION_MAX_AGE_SECONDS",
                    str(DEFAULT_QUEUE_ATTENTION_MAX_AGE_SECONDS),
                )
            ),
        )
    except ValueError:
        return DEFAULT_QUEUE_ATTENTION_MAX_AGE_SECONDS


def queue_stale_reason(task: dict[str, Any], now: datetime) -> str:
    status = str(task.get("status") or "unknown")
    age = queue_task_age_seconds(task, now)
    if status == "send_retrying":
        timeout = queue_send_retry_timeout_seconds()
        claimed = parse_queue_datetime(task.get("send_retry_claimed_at"))
        if timeout > 0 and (not claimed or (now - claimed).total_seconds() > timeout):
            return f"send_retrying older than {timeout}s sender grace"
    if status == "pending" and age is not None and age > 1800:
        return "pending older than 30m"
    if status == "in_progress" and age is not None and age > 7200:
        return "in_progress older than 2h"
    if status == "generation_waiting":
        next_poll = parse_queue_datetime(task.get("next_poll_at"))
        if next_poll and (now - next_poll).total_seconds() > 900:
            return "generation poll overdue by more than 15m"
        if not next_poll and age is not None and age > 21600:
            return "generation_waiting has no next_poll_at and is older than 6h"
    if status == "generation_poststage_pending":
        next_poststage = parse_queue_datetime(task.get("next_poststage_at"))
        if next_poststage and (now - next_poststage).total_seconds() > 900:
            return "generation poststage overdue by more than 15m"
    if status == "publish_poststage_pending":
        next_poststage = parse_queue_datetime(task.get("next_publish_poststage_at"))
        if next_poststage and (now - next_poststage).total_seconds() > 900:
            return "publish poststage overdue by more than 15m"
    return ""


def queue_task_brief(task: dict[str, Any], now: datetime) -> dict[str, Any]:
    status = str(task.get("status") or "unknown")
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    brief: dict[str, Any] = {
        "id": task.get("id", ""),
        "chat": task.get("chat", ""),
        "status": status,
        "category": queue_status_category(status),
        "routine": (task.get("routine") or {}).get("id", "") if isinstance(task.get("routine"), dict) else "",
        "created_at": task.get("created_at", ""),
        "updated_at": task.get("updated_at", ""),
        "age_seconds": queue_task_age_seconds(task, now),
        "request": str(task.get("request") or "")[:240],
    }
    reason = str(task.get("send_deferred_reason") or "")
    if reason:
        brief["send_deferred_reason"] = reason
    retry_count = task.get("send_retry_count")
    if retry_count not in (None, ""):
        brief["send_retry_count"] = retry_count
    for key in ("next_poll_at", "next_poststage_at", "next_publish_poststage_at", "waiting_reason"):
        if task.get(key):
            brief[key] = task.get(key)
    files = result.get("files") if isinstance(result, dict) else None
    if isinstance(files, list):
        brief["result_file_count"] = len(files)
    sent_files = task.get("sent_file_paths")
    if isinstance(sent_files, list):
        brief["sent_file_count"] = len(sent_files)
    stale_reason = queue_stale_reason(task, now)
    if stale_reason:
        brief["stale_reason"] = stale_reason
    return brief


def queue_attention(tasks: list[dict[str, Any]], *, limit: int = 8, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    attention_max_age_seconds = queue_attention_max_age_seconds()
    counts = {
        "active": 0,
        "delivery_blocked": 0,
        "human_blocked": 0,
        "failed": 0,
        "stale": 0,
        "unknown": 0,
        "historical": 0,
    }
    active: list[dict[str, Any]] = []
    delivery_blocked: list[dict[str, Any]] = []
    human_blocked: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    by_chat: dict[str, dict[str, int]] = {}

    for task in tasks:
        status = str(task.get("status") or "unknown")
        category = queue_status_category(status)
        chat = str(task.get("chat") or "(unknown)")
        by_chat.setdefault(
            chat,
            {
                "total": 0,
                "active": 0,
                "delivery_blocked": 0,
                "human_blocked": 0,
                "failed": 0,
                "historical": 0,
            },
        )
        by_chat[chat]["total"] += 1

        brief = queue_task_brief(task, now)
        age_seconds = brief.get("age_seconds")
        historical = bool(
            category in {"delivery_blocked", "human_blocked", "failed", "unknown"}
            and attention_max_age_seconds > 0
            and isinstance(age_seconds, int)
            and age_seconds > attention_max_age_seconds
        )
        if historical:
            counts["historical"] += 1
            by_chat[chat]["historical"] += 1
            continue
        if category in by_chat[chat]:
            by_chat[chat][category] += 1
        if category == "active":
            counts["active"] += 1
            active.append(brief)
        elif category == "delivery_blocked":
            counts["delivery_blocked"] += 1
            delivery_blocked.append(brief)
        elif category == "human_blocked":
            counts["human_blocked"] += 1
            human_blocked.append(brief)
        elif category == "failed":
            counts["failed"] += 1
            failed.append(brief)
        elif category == "unknown":
            counts["unknown"] += 1

        if brief.get("stale_reason"):
            counts["stale"] += 1
            stale.append(brief)

    summary = []
    if counts["delivery_blocked"]:
        summary.append(f"{counts['delivery_blocked']} task(s) blocked on WeChat/artifact delivery")
    if counts["human_blocked"]:
        summary.append(f"{counts['human_blocked']} task(s) waiting for human confirmation")
    if counts["failed"]:
        summary.append(f"{counts['failed']} task(s) failed and need repair/reprocess")
    if counts["stale"]:
        summary.append(f"{counts['stale']} task(s) stale or overdue")
    if counts["unknown"]:
        summary.append(f"{counts['unknown']} task(s) have an unknown status")

    recommended_commands = []
    if counts["delivery_blocked"]:
        recommended_commands.extend(
            [
                "labcanvas wechat hold status --json",
                "labcanvas wechat worker once --send",
                "labcanvas wechat unlock-watchdog once --flush-deferred",
            ]
        )
    if counts["human_blocked"]:
        recommended_commands.append("labcanvas wechat approve <task_id> --note '<reason>'")
    if counts["failed"] or counts["stale"]:
        recommended_commands.extend(
            [
                "labcanvas wechat worker reprocess <task_id> '<reason>' --send",
                "labcanvas wechat selftest --suite all --json",
            ]
        )

    needs_attention = bool(counts["delivery_blocked"] or counts["human_blocked"] or counts["failed"] or counts["stale"] or counts["unknown"])
    return {
        "ok": not needs_attention,
        "needs_attention": needs_attention,
        "counts": counts,
        "attention_horizon_seconds": attention_max_age_seconds,
        "summary": summary,
        "active": active[-limit:],
        "delivery_blocked": delivery_blocked[-limit:],
        "human_blocked": human_blocked[-limit:],
        "failed": failed[-limit:],
        "stale": stale[-limit:],
        "by_chat": by_chat,
        "recommended_commands": list(dict.fromkeys(recommended_commands)),
    }


def queue_summary(path: Path, *, limit: int = 8) -> dict[str, Any]:
    tasks = read_jsonl(path)
    counts: dict[str, int] = {}
    now = datetime.now()
    for task in tasks:
        status = str(task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    recent = []
    for task in tasks[-limit:]:
        brief = queue_task_brief(task, now)
        brief["completed_at"] = task.get("completed_at", "")
        recent.append(brief)
    return {
        "ok": True,
        "path": str(path),
        "exists": path.exists(),
        "total": len(tasks),
        "counts": counts,
        "recent": recent,
        "attention": queue_attention(tasks, limit=limit, now=now),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def mirror_summary(path: Path, *, limit: int = 8) -> dict[str, Any]:
    if not path.exists():
        return {"ok": True, "path": str(path), "exists": False, "message_count": 0, "event_count": 0, "recent": []}
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            message_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            event_count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
            recent = [
                {
                    "id": row["id"],
                    "observed_at": row["observed_at"],
                    "chat": row["chat"],
                    "direction": row["direction"],
                    "status": row["status"],
                    "body": row["body"][:240],
                }
                for row in conn.execute(
                    """
                    SELECT messages.id, messages.observed_at, chats.name AS chat, messages.direction,
                           messages.status, replace(messages.body, char(10), ' ') AS body
                    FROM messages
                    JOIN chats ON chats.id = messages.chat_id
                    ORDER BY messages.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ]
    except sqlite3.Error as exc:
        return {"ok": False, "path": str(path), "exists": True, "error": str(exc), "recent": []}
    return {
        "ok": True,
        "path": str(path),
        "exists": True,
        "message_count": int(message_count),
        "event_count": int(event_count),
        "recent": recent,
    }


def direct_monitor_health() -> dict[str, Any]:
    configs = discover_direct_monitor_configs()
    client = wechat_client_availability()
    groups = [direct_config_health(path, client=client) for path in configs]
    separation = direct_config_separation_summary(configs)
    backend = external_backend_summary()
    queue = queue_summary(configured_runtime_paths()["queue"], limit=5)
    caught_up = sum(1 for item in groups if item.get("caught_up"))
    ready = sum(1 for item in groups if item.get("ready"))
    stale = sum(1 for item in groups if item.get("source_stale"))
    ok = bool(groups) and all(item.get("ok") for item in groups) and bool(backend.get("ok")) and bool(separation.get("ok"))
    return {
        "ok": ok,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "group_count": len(groups),
        "caught_up_groups": caught_up,
        "ready_groups": ready,
        "stale_source_groups": stale,
        "client": client,
        "separation": separation,
        "external_backend": backend,
        "queue": queue,
        "groups": groups,
        "notes": [
            "private chatroom ids, wxids, message-table names, and DB paths are intentionally omitted",
            "set WECHAT_DIRECT_CONFIGS in .private/wechat_supervisor.local.env to control monitored groups",
            "caught_up means state reached the latest decrypted row; ready also requires a fresh monitor heartbeat and an available logged-in client",
            "chat_quiet and last_message_old are informational and do not make a healthy idle monitor stale",
        ],
    }


def direct_config_separation_summary(paths: list[Path]) -> dict[str, Any]:
    records: list[dict[str, str]] = []
    missing_send_title_guard: list[str] = []
    unreadable: list[str] = []
    for path in paths:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            unreadable.append(path.name)
            continue
        config_name = path.name
        if not has_send_title_guard(config.get("send_target")):
            missing_send_title_guard.append(config_name)
        records.append(
            {
                "config_name": config_name,
                "chat_name": str(config.get("chat_name") or ""),
                "message_table": str(config.get("message_table") or ""),
                "state_path": normalized_private_path_key(str(config.get("state_path") or "")),
            }
        )
    duplicate_state = duplicate_config_groups(records, "state_path")
    duplicate_table = duplicate_config_groups(records, "message_table")
    duplicate_chat = duplicate_config_groups(records, "chat_name")
    ok = not unreadable and not missing_send_title_guard and not duplicate_state and not duplicate_table and not duplicate_chat
    return {
        "ok": ok,
        "private_values_redacted": True,
        "config_count": len(paths),
        "checked_config_count": len(records),
        "unreadable_config_count": len(unreadable),
        "missing_send_title_guard_count": len(missing_send_title_guard),
        "duplicate_state_path_count": len(duplicate_state),
        "duplicate_message_table_count": len(duplicate_table),
        "duplicate_chat_name_count": len(duplicate_chat),
        "unreadable_configs": unreadable,
        "missing_send_title_guard_configs": missing_send_title_guard,
        "duplicate_state_path_configs": duplicate_state,
        "duplicate_message_table_configs": duplicate_table,
        "duplicate_chat_name_configs": duplicate_chat,
    }


def normalized_private_path_key(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = PACKAGE_ROOT / path
    return str(path)


def duplicate_config_groups(records: list[dict[str, str]], field: str) -> list[list[str]]:
    grouped: dict[str, list[str]] = {}
    for record in records:
        value = record.get(field, "")
        if not value:
            continue
        grouped.setdefault(value, []).append(record["config_name"])
    return [sorted(names) for names in grouped.values() if len(names) > 1]


def external_backend_summary() -> dict[str, Any]:
    proc = run_command([sys.executable, str(SCRIPTS / "wechat_direct_backend.py"), "--json", "status"], capture=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": "status-unreadable",
            "returncode": proc.returncode,
            "private_paths_redacted": True,
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "status": "status-invalid",
            "returncode": proc.returncode,
            "private_paths_redacted": True,
        }
    allowed = {
        "ok",
        "status",
        "external_exists",
        "external_git",
        "venv_python_exists",
        "db_dir_exists",
        "message_db_exists",
        "source_db_count",
        "config_exists",
        "keys_file_exists",
        "keys_file_mtime",
        "decrypted_dir_exists",
        "decrypted_db_count",
        "decrypted_message_db_exists",
        "scripts",
        "monitor_web",
        "private_paths_redacted",
        "checked_at",
    }
    summary = {key: payload[key] for key in allowed if key in payload}
    summary["returncode"] = proc.returncode
    return summary


def codex_session_summary() -> dict[str, Any]:
    registry = PRIVATE / "codex_sessions" / "sessions.local.json"
    if not registry.exists():
        return {"ok": True, "path_exists": False, "count": 0, "items": []}
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "path_exists": True, "count": 0, "items": []}
    items = []
    thread_counts: dict[str, int] = {}
    if isinstance(data, dict):
        for key, value in sorted(data.items()):
            if not isinstance(value, dict):
                continue
            thread_id = str(value.get("thread_id") or "")
            if thread_id:
                thread_counts[thread_id] = thread_counts.get(thread_id, 0) + 1
            items.append(
                {
                    "key": key,
                    "legacy_key": not bool(CODEX_SESSION_KEY_RE.fullmatch(key)),
                    "chat_name": value.get("chat_name", ""),
                    "role": value.get("role", ""),
                    "thread_id_short": thread_id[:8] if thread_id else "",
                    "model": value.get("model", ""),
                    "reasoning_effort": value.get("reasoning_effort", ""),
                    "turn_count": int(value.get("turn_count") or 0),
                    "last_used_at": value.get("last_used_at", ""),
                    "last_resumed": bool(value.get("last_resumed")),
                    "last_fallback_started": bool(value.get("last_fallback_started")),
                }
            )
    legacy_count = sum(1 for item in items if item["legacy_key"])
    duplicate_thread_count = 0
    for item in items:
        record = data.get(item["key"], {}) if isinstance(data, dict) else {}
        thread_id = str(record.get("thread_id") or "") if isinstance(record, dict) else ""
        duplicate = bool(thread_id and thread_counts.get(thread_id, 0) > 1)
        item["thread_id_duplicate"] = duplicate
        if duplicate:
            duplicate_thread_count += 1
    return {
        "ok": legacy_count == 0 and duplicate_thread_count == 0,
        "path_exists": True,
        "count": len(items),
        "legacy_count": legacy_count,
        "duplicate_thread_entry_count": duplicate_thread_count,
        "items": items,
    }


def discover_direct_monitor_configs() -> list[Path]:
    env_path = PRIVATE / "wechat_supervisor.local.env"
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            key, raw_value = stripped.split("=", 1)
            key = key.strip()
            try:
                parsed = shlex.split(raw_value, comments=False, posix=True)
                values[key] = parsed[0] if parsed else ""
            except ValueError:
                values[key] = raw_value.strip().strip("'\"")
    raw_configs = values.get("WECHAT_DIRECT_CONFIGS") or os.environ.get("WECHAT_DIRECT_CONFIGS", "")
    paths: list[Path] = []
    if raw_configs:
        paths = [Path(item.strip()) for item in raw_configs.split(",") if item.strip()]
    else:
        raw_config = values.get("WECHAT_DIRECT_CONFIG") or os.environ.get("WECHAT_DIRECT_CONFIG", "")
        if raw_config:
            paths = [Path(raw_config)]
        elif DEFAULT_DIRECT_CONFIG.exists():
            paths = [DEFAULT_DIRECT_CONFIG]
    resolved = []
    seen: set[Path] = set()
    for path in paths:
        candidate = path if path.is_absolute() else PACKAGE_ROOT / path
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(candidate)
    return resolved


def direct_config_health(
    path: Path,
    *,
    client: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = {
        "config_name": path.name,
        "config_exists": path.exists(),
        "ok": False,
        "caught_up": False,
    }
    if not path.exists():
        return {**base, "error": "config-missing"}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {**base, "error": str(exc)}

    raw_state_path = str(config.get("state_path") or "").strip()
    state_path = Path(raw_state_path) if raw_state_path else None
    if state_path and not state_path.is_absolute():
        state_path = PACKAGE_ROOT / state_path
    state_last = 0
    state: dict[str, Any] = {}
    state_exists = state_path.exists() if state_path else False
    if state_exists:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state_last = int(state.get("last_local_id") or 0)
        except (OSError, json.JSONDecodeError, ValueError):
            state_last = 0

    latest = latest_direct_db_local_id(str(config.get("message_table") or ""))
    cursors = state.get("message_db_cursors")
    if isinstance(cursors, dict) and latest.get("message_db") in cursors:
        try:
            state_last = int(cursors[latest["message_db"]])
        except (TypeError, ValueError):
            state_last = 0
    has_guarded_target = has_send_title_guard(config.get("send_target"))
    codex = config.get("codex") if isinstance(config.get("codex"), dict) else {}
    organizer = config.get("organizer") if isinstance(config.get("organizer"), dict) else {}
    poll_seconds = float(config.get("poll_seconds", 0.8))
    stale_warning_seconds = int(config.get("stale_warning_seconds", 1800))
    latest_age_seconds = float(latest.get("age_seconds") or 0)
    chat_quiet = bool(latest.get("ok") and latest_age_seconds > stale_warning_seconds)
    heartbeat_at = parse_health_timestamp(state.get("last_loop_at"))
    heartbeat_age_seconds = (
        max(0.0, (datetime.now(timezone.utc) - heartbeat_at).total_seconds())
        if heartbeat_at is not None
        else None
    )
    monitor_stale_seconds = max(
        30,
        int(config.get("monitor_stale_seconds") or max(30.0, poll_seconds * 20)),
    )
    monitor_stale = bool(
        not state_exists
        or heartbeat_at is None
        or heartbeat_age_seconds is None
        or heartbeat_age_seconds > monitor_stale_seconds
    )
    caught_up = latest.get("ok") and state_last >= int(latest.get("latest_local_id") or 0)
    client_blocked = bool(client is not None and client.get("available") is False)
    ready = bool(caught_up and not monitor_stale and not client_blocked)
    ok = bool(
        state_exists
        and latest.get("ok")
        and caught_up
        and not monitor_stale
        and not client_blocked
        and bool(config.get("ignore_self_messages", True))
        and not bool(config.get("respond_to_self", False))
        and has_guarded_target
    )
    return {
        **base,
        "ok": ok,
        "chat_name": str(config.get("chat_name") or path.stem),
        "state_exists": state_exists,
        "state_last_local_id": state_last,
        "db_latest_local_id": latest.get("latest_local_id"),
        "db_latest_at": latest.get("latest_at", ""),
        "db_latest_age_seconds": latest_age_seconds,
        "last_message_old": chat_quiet,
        "chat_quiet": chat_quiet,
        "monitor_heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else "",
        "monitor_heartbeat_age_seconds": heartbeat_age_seconds,
        "monitor_stale_seconds": monitor_stale_seconds,
        "monitor_stale": monitor_stale,
        # Compatibility fields now describe monitor freshness, not chat activity.
        "db_stale": bool(monitor_stale or client_blocked),
        "source_stale": bool(monitor_stale or client_blocked),
        "client_blocked": client_blocked,
        "client_status": str((client or {}).get("status") or "not_checked"),
        "ready": ready,
        "caught_up": bool(caught_up),
        "respond_to_all": bool(config.get("respond_to_all", False)),
        "respond_to_self": bool(config.get("respond_to_self", False)),
        "ignore_self_messages": bool(config.get("ignore_self_messages", True)),
        "allow_human_self_messages": bool(config.get("allow_human_self_messages", False)),
        "self_message_policy": str(config.get("self_message_policy") or "ignore"),
        "self_messages_text_only": bool(config.get("self_messages_text_only", True)),
        "ignore_probable_bot_self_replies": bool(config.get("ignore_probable_bot_self_replies", True)),
        "chat_purpose": str(config.get("chat_purpose") or ""),
        "analysis_mode": str(config.get("analysis_mode") or ""),
        "organizer_enabled": bool(organizer.get("enabled", False)),
        "organizer_capture_unclassified": bool(organizer.get("capture_unclassified", True)),
        "poll_seconds": poll_seconds,
        "catchup_poll_seconds": float(config.get("catchup_poll_seconds", 0.1)),
        "codex_model": str(codex.get("model") or "gpt-5.5"),
        "codex_reasoning_effort": str(codex.get("reasoning_effort") or "medium"),
        "codex_timeout_seconds": int(codex.get("timeout_seconds") or 60),
        "last_seen_at": str(state.get("last_seen_at") or ""),
        "last_loop_at": str(state.get("last_loop_at") or ""),
        "last_loop_metrics": sanitize_loop_metrics(state.get("last_loop_metrics")),
        "send_target_has_title_guard": has_guarded_target,
        "db_status": latest.get("status", "ok"),
    }


def sanitize_loop_metrics(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = {
        "started_at",
        "decrypt_ms",
        "read_ms",
        "context_ms",
        "codex_ms",
        "send_ms",
        "total_ms",
        "organizer_ms",
        "organizer_status",
        "organizer_messages",
        "organizer_items",
        "organizer_error",
        "trigger_candidates",
        "focus_rows",
        "coalesced_trigger_rows",
        "skip_reasons",
        "send_error",
        "voice_rows",
        "voice_transcribed",
        "voice_cached",
        "voice_failed",
        "voice_enrich_ms",
        "voice_pending_retried",
        "voice_pending_ready",
        "voice_pending_backlog",
        "voice_pending_added",
        "voice_pending_local_id",
    }
    return {key: raw[key] for key in allowed if key in raw}


def latest_direct_db_local_id(table: str) -> dict[str, Any]:
    sys.path.insert(0, str(SCRIPTS))
    from wechat_transport_selection import STORE, tiny11_enabled

    # Match the monitor's selected transport; old Linux caches remain on disk
    # for fallback but must not be compared with a Windows ingestion cursor.
    if tiny11_enabled():
        db_paths = [STORE] if STORE.is_file() else []
    else:
        db_paths = sorted(
            (PRIVATE / "wechat_decrypt" / "decrypted" / "message").glob("message_*.db"),
            key=lambda path: int(path.stem.rsplit("_", 1)[-1]) if path.stem.rsplit("_", 1)[-1].isdigit() else -1,
            reverse=True,
        )
    if not db_paths:
        return {"ok": False, "status": "decrypted-db-missing", "latest_local_id": None, "latest_at": "", "age_seconds": None}
    if not is_safe_sql_identifier(table):
        return {"ok": False, "status": "message-table-invalid", "latest_local_id": None, "latest_at": "", "age_seconds": None}
    rows: list[tuple[int, int, str]] = []
    for db_path in db_paths:
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(f'SELECT MAX(local_id), MAX(create_time) FROM "{table}"').fetchone()
        except sqlite3.Error:
            continue
        if row:
            rows.append((int(row[0] or 0), int(row[1] or 0), db_path.name))
    if not rows:
        return {"ok": False, "status": "message-table-unreadable", "latest_local_id": None, "latest_at": "", "age_seconds": None}
    latest_local_id, latest_time, message_db = max(rows, key=lambda item: (item[1], item[0]))
    latest_at = datetime.fromtimestamp(latest_time).isoformat(timespec="seconds") if latest_time else ""
    age_seconds = int(datetime.now().timestamp() - latest_time) if latest_time else None
    return {
        "ok": True,
        "status": "ok",
        "latest_local_id": latest_local_id,
        "latest_at": latest_at,
        "age_seconds": age_seconds,
        "message_db": message_db,
    }


def is_safe_sql_identifier(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch == "_" for ch in value)


def has_send_title_guard(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    return bool(str(raw.get("expected_title") or raw.get("title") or raw.get("name") or "").strip())


def control_map_payload() -> dict[str, Any]:
    status = status_payload()
    health = direct_monitor_health()
    layer_specs = [
        {
            "id": "isolated_gui",
            "name": "Isolated official GUI control",
            "goal": "Drive the real Linux WeChat client through Xvfb/noVNC, xdotool, xclip, screenshots, OCR, and title guards.",
            "ready": status["desktop"]["status"] == "ready",
            "commands": [
                "labcanvas wechat desktop start",
                "labcanvas wechat send --message 'ping'",
                "labcanvas wechat browser-assist --url https://example.com --json",
            ],
            "failure_mode": "If OCR/title guard is ambiguous, fail closed and ask for human confirmation.",
        },
        {
            "id": "direct_receive",
            "name": "Direct receive and state mirror",
            "goal": "Poll the user's own local message cache/decrypted mirror, coalesce bursts, ignore self messages, and maintain per-chat state.",
            "ready": bool(health.get("ok")) and int(health.get("group_count") or 0) > 0,
            "commands": [
                "labcanvas wechat health --json",
                "labcanvas wechat hold start",
            ],
            "failure_mode": "If a chat config is stale or duplicated, do not route messages until health is green.",
        },
        {
            "id": "media_sync",
            "name": "Attachment and media resolver",
            "goal": "Copy same-chat files/images/videos from xwechat_files into ignored private storage and bind them to source local_id rows.",
            "ready": bool(status["mirror"].get("exists")) and len(status.get("media_sources") or []) > 0,
            "commands": [
                "labcanvas wechat media-sync --chat '<chat>' --auto-source --since-minutes 60",
                "labcanvas wechat autopublish-video --chat '<chat>' --message-local-id 14 --sync --fetch-gui --json",
            ],
            "failure_mode": "If exact media is missing, ask for resend/opening the source; never borrow nearby media.",
        },
        {
            "id": "agent_queue",
            "name": "Agent queue and deterministic workers",
            "goal": "Route simple replies fast, queue slow work, atomically claim tasks, reuse per-chat sessions, and run deterministic exact-video publishing.",
            "ready": status["sessions"]["supervisor"]["running"] and status["queue"]["exists"],
            "commands": [
                "labcanvas wechat queue --json",
                "labcanvas wechat worker once --send",
            ],
            "failure_mode": "If a worker crashes, stale in-progress claims are reclaimed; irreversible actions still require explicit approval.",
        },
        {
            "id": "observability",
            "name": "Event log, screenshots, and replay evidence",
            "goal": "Record mirrored messages, outbound sends, task state, GUI screenshots, and queue transitions without committing private data.",
            "ready": bool(status["mirror"].get("exists")) and int(status["mirror"].get("event_count") or 0) > 0,
            "commands": [
                "labcanvas wechat status --json",
                "labcanvas wechat memory summary --chat '<chat>' --json",
            ],
            "failure_mode": "Logs are local/private; public docs must use placeholders and summaries only.",
        },
        {
            "id": "official_or_bridge_apis",
            "name": "Optional bridge APIs",
            "goal": "Evaluate Wechaty, MCP, ACP, Docker GUI, or platform-specific accessibility bridges when they are more stable than screen control.",
            "ready": False,
            "commands": [
                "Review the reference_projects list before adding a new backend.",
            ],
            "failure_mode": "Use only consented accounts and maintained projects; keep the GUI path as the fallback.",
        },
    ]
    blocked = [
        {
            "id": "packet_mitm",
            "name": "Wireshark/TLS MITM/private protocol replay",
            "reason": "Unreliable against encrypted apps and not acceptable for automation that could expose credentials, session tokens, or private traffic.",
            "allowed_alternative": "Use GUI state, local owned-data mirrors, official APIs, or human-assisted browser sessions.",
        },
        {
            "id": "session_or_key_theft",
            "name": "Session extraction, credential theft, or bypassing login/CAPTCHA",
            "reason": "This crosses from automation into unauthorized access. The tool must ask the user to log in or confirm manually.",
            "allowed_alternative": "Open noVNC/browser-assist and wait for the account owner.",
        },
        {
            "id": "cross_chat_media_guessing",
            "name": "Using nearby files or another chat's media",
            "reason": "It causes crosstalk and wrong actions.",
            "allowed_alternative": "Exact local_id/server_id/media-token matching with fail-closed behavior.",
        },
    ]
    ready_layers = sum(1 for layer in layer_specs if layer["ready"])
    return {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "score": {
            "ready_layers": ready_layers,
            "total_layers": len(layer_specs),
            "label": "high-control" if ready_layers >= 5 else "partial-control" if ready_layers >= 3 else "bootstrap-needed",
        },
        "principle": "Make WeChat observable and controllable through owned, consented surfaces; do not bypass encryption, authentication, or platform abuse protections.",
        "layers": layer_specs,
        "blocked_methods": blocked,
        "reference_projects": wechat_reference_projects(),
        "next_hardening_steps": [
            "Add per-action screenshot before/after diff assertions for every GUI sender action.",
            "Add a UI state machine for compose box, attachment modal, chat search, and group settings pages.",
            "Keep one send lock across all GUI actions and expose lock owner/age in health output.",
            "Add media manifest rows keyed by chat_name, local_id, server_id, md5, size, and path.",
            "Add optional bridge backends behind capability flags, never as a replacement for the safe GUI fallback.",
        ],
    }


def wechat_reference_projects() -> list[dict[str, str]]:
    return [
        {
            "name": "wechaty/wechaty",
            "url": "https://github.com/wechaty/wechaty",
            "use": "Cross-platform conversational RPA SDK; useful as a design reference for bot event APIs and adapters.",
            "risk": "May depend on puppet/provider availability and login constraints.",
        },
        {
            "name": "BiboyQG/WeChat-MCP",
            "url": "https://github.com/BiboyQG/WeChat-MCP",
            "use": "MCP/accessibility/screen-capture pattern for exposing WeChat actions as agent tools.",
            "risk": "Platform-specific; keep title guards and GUI fallback.",
        },
        {
            "name": "thisnick/agent-wechat",
            "url": "https://github.com/thisnick/agent-wechat",
            "use": "Dockerized RPA controller pattern with API/CLI surfaces.",
            "risk": "Container GUI reliability and account login state still need supervision.",
        },
        {
            "name": "huohuoer/wechat-cli",
            "url": "https://github.com/huohuoer/wechat-cli",
            "use": "Local WeChat data query pattern for LLM integration.",
            "risk": "Use only with the owner's local data and private configs.",
        },
        {
            "name": "ylytdeng/wechat-decrypt",
            "url": "https://github.com/ylytdeng/wechat-decrypt",
            "use": "Owned-data decrypt/monitor backend pattern already used as an optional receive backend.",
            "risk": "Sensitive private data; never commit keys, DBs, or raw chat logs.",
        },
        {
            "name": "formulahendry/wechat-acp",
            "url": "https://github.com/formulahendry/wechat-acp",
            "use": "ACP bridge pattern for forwarding messages to agent runtimes.",
            "risk": "Validate protocol, auth, and account-safety assumptions before integration.",
        },
        {
            "name": "leeguooooo/wechat-use",
            "url": "https://github.com/leeguooooo/wechat-use",
            "use": "macOS background send/agent skill pattern; useful if adding a macOS backend.",
            "risk": "Not a Linux replacement; treat as separate backend.",
        },
    ]


def discover_media_sources() -> list[Path]:
    base = Path.home() / "Documents" / "xwechat_files"
    if not base.exists():
        return []
    sources = []
    seen = set()
    for profile in base.iterdir():
        if not profile.is_dir():
            continue
        for relative in ("msg/file", "msg/video", "msg/attach", "cache", "temp/ImageTemp", "temp/ImageUtils"):
            path = profile / relative
            if not path.is_dir():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            sources.append(resolved)
    return sorted(sources)


def desktop_status() -> dict[str, Any]:
    sys.path.insert(0, str(SCRIPTS))
    from wechat_transport_selection import tiny11_enabled, tiny11_health
    if tiny11_enabled():
        client = tiny11_health()
        return {**client, 'client': client, 'display': 'tiny11-console',
                'ports': {'6143': port_listening(6143)}}
    display_ok = run_command(["xdpyinfo"], capture=True, env=display_env(DEFAULT_DISPLAY)).returncode == 0
    wechat_window = run_command(["xdotool", "search", "--onlyvisible", "--class", "wechat"], capture=True, env=display_env(DEFAULT_DISPLAY))
    ports = {str(port): port_listening(port) for port in (DEFAULT_VNC_PORT, DEFAULT_NOVNC_PORT, DEFAULT_PREVIEW_PORT)}
    watchdog = fresh_unlock_watchdog_state()
    client = wechat_client_availability(watchdog)
    live_state = str(client.get("status") or "")
    base_ready = bool(
        display_ok
        and wechat_window.returncode == 0
        and ports[str(DEFAULT_NOVNC_PORT)]
    )
    if not display_ok:
        status = "offline"
    elif not base_ready:
        status = "partial"
    elif client.get("available") is False:
        status = live_state
    else:
        status = "ready"
    return {
        "status": status,
        "display": DEFAULT_DISPLAY,
        "display_ok": display_ok,
        "wechat_window": wechat_window.stdout.split(),
        "ports": ports,
        "client": client,
        "watchdog": watchdog,
        "novnc_url": f"http://127.0.0.1:{DEFAULT_PREVIEW_PORT}/",
        "direct_novnc_url": f"http://127.0.0.1:{DEFAULT_NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale",
    }


def fresh_unlock_watchdog_state(
    path: Path = UNLOCK_WATCHDOG_STATE,
    *,
    max_age_seconds: float = 90.0,
) -> dict[str, Any]:
    try:
        age_seconds = max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        retry_after_seconds = max(0.0, float(payload.get("retry_after_seconds") or 0.0))
    except (TypeError, ValueError):
        retry_after_seconds = 0.0
    valid_for_seconds = max(float(max_age_seconds), retry_after_seconds + 30.0)
    if age_seconds > valid_for_seconds:
        return {}
    return {
        **payload,
        "age_seconds": round(age_seconds, 3),
        "valid_for_seconds": round(valid_for_seconds, 3),
    }


def effective_watchdog_desktop_state(watchdog: dict[str, Any]) -> dict[str, Any]:
    """Return the terminal desktop observation from one watchdog cycle."""

    after = watchdog.get("after")
    if isinstance(after, dict) and str(after.get("status") or ""):
        return after
    desktop = watchdog.get("desktop")
    return desktop if isinstance(desktop, dict) else {}


def wechat_client_availability(
    watchdog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify whether the official client can currently ingest and send."""

    sys.path.insert(0, str(SCRIPTS))
    from wechat_transport_selection import tiny11_enabled, tiny11_health
    if tiny11_enabled():
        return tiny11_health()

    observed = fresh_unlock_watchdog_state() if watchdog is None else watchdog
    state = effective_watchdog_desktop_state(observed)
    status = str(state.get("status") or "watchdog_unavailable")
    known = status in WECHAT_CLIENT_KNOWN_STATES
    available = bool(known and status == "unlocked")
    reasons = {
        "detect_failed": "desktop_state_detection_failed",
        "entry_required": "login_required",
        "locked": "desktop_locked",
        "no_window": "client_window_missing",
        "unlocked": "ready",
        "watchdog_unavailable": "watchdog_stale_or_missing",
    }
    return {
        "ok": available,
        "available": available,
        "known": known,
        "status": status,
        "reason": reasons.get(status, "desktop_state_unknown"),
        "human_action_required": status == "entry_required",
        "watchdog_age_seconds": observed.get("age_seconds"),
        "watchdog_valid_for_seconds": observed.get("valid_for_seconds"),
    }


def tmux_status(session: str) -> dict[str, Any]:
    if shutil.which("tmux") is None:
        return {"session": session, "running": False, "status": "missing-tmux"}
    check = run_command(["tmux", "has-session", "-t", session], capture=True)
    if check.returncode != 0:
        return {"session": session, "running": False, "status": "not-running"}
    panes = tmux_session_panes(session)
    return {"session": session, "running": True, "status": "running", "panes": panes}


def tmux_window_status(session: str, window_name: str) -> dict[str, Any]:
    if shutil.which("tmux") is None:
        return {"session": session, "window": window_name, "running": False, "status": "missing-tmux"}
    check = run_command(["tmux", "has-session", "-t", session], capture=True)
    if check.returncode != 0:
        return {"session": session, "window": window_name, "running": False, "status": "session-not-running"}
    windows = run_command(["tmux", "list-windows", "-t", session, "-F", "#{window_id}\t#{window_name}"], capture=True)
    for line in windows.stdout.splitlines():
        if "\t" not in line:
            continue
        window_id, name = line.split("\t", 1)
        if name != window_name:
            continue
        proc = run_command(["tmux", "list-panes", "-t", window_id, "-F", "#{pane_index}: #{pane_current_command} #{pane_pid}"], capture=True)
        return {"session": session, "window": window_name, "running": True, "status": "running", "panes": proc.stdout.splitlines()}
    return {"session": session, "window": window_name, "running": False, "status": "window-not-running"}


def kill_tmux_window(session: str, window_name: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    check = tmux_window_status(session, window_name)
    if not check["running"]:
        return False
    proc = run_command(["tmux", "kill-window", "-t", f"{session}:{window_name}"], capture=True)
    return proc.returncode == 0


def tmux_session_panes(session: str) -> list[str]:
    windows = run_command(["tmux", "list-windows", "-t", session, "-F", "#{window_id}\t#{window_name}"], capture=True)
    panes: list[str] = []
    for line in windows.stdout.splitlines():
        if "\t" not in line:
            continue
        window_id, window_name = line.split("\t", 1)
        proc = run_command(["tmux", "list-panes", "-t", window_id, "-F", "#{pane_index}: #{pane_current_command} #{pane_pid}"], capture=True)
        for pane in proc.stdout.splitlines():
            panes.append(f"{window_name}.{pane}")
    return panes


def labcanvas_web_status(session: str) -> dict[str, Any]:
    proc = run_command([sys.executable, "-m", "agenticapp", "webapp", "status", "--session", session, "--json"], capture=True)
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "status": "not-running", "session": session, "url": "", "stderr": proc.stderr.strip()}
    try:
        payload = json.loads(proc.stdout)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    return {"ok": proc.returncode == 0, "status": "unknown", "session": session, "url": "", "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def kill_tmux(session: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    if run_command(["tmux", "has-session", "-t", session], capture=True).returncode != 0:
        return False
    descendants = tmux_session_descendant_pids(session)
    run_command(["tmux", "kill-session", "-t", session], capture=True)
    terminate_process_ids(descendants)
    return True


def tmux_session_descendant_pids(session: str) -> list[int]:
    """Capture pane descendants so detached agent subprocesses cannot survive stop."""

    panes = run_command(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
        capture=True,
    )
    roots: list[int] = []
    for line in panes.stdout.splitlines():
        name, separator, raw_pid = line.partition("\t")
        if separator and name == session:
            try:
                roots.append(int(raw_pid))
            except ValueError:
                continue
    descendants: list[int] = []
    seen = set(roots)
    stack = list(roots)
    while stack:
        parent = stack.pop()
        children_path = Path(f"/proc/{parent}/task/{parent}/children")
        try:
            children = [int(value) for value in children_path.read_text().split()]
        except (OSError, ValueError):
            continue
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            stack.append(child)
    return descendants


def terminate_process_ids(process_ids: list[int], *, grace_seconds: float = 1.0) -> None:
    """Terminate only PIDs captured from the project-owned tmux session."""

    remaining = {pid for pid in process_ids if pid > 1}
    for pid in reversed(process_ids):
        try:
            os.kill(pid, 15)
        except (ProcessLookupError, PermissionError):
            remaining.discard(pid)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            if not Path(f"/proc/{pid}").exists():
                remaining.discard(pid)
        if remaining:
            time.sleep(0.05)
    for pid in remaining:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def port_listening(port: int) -> bool:
    proc = run_command(["ss", "-ltn"], capture=True)
    if proc.returncode != 0:
        return False
    return any(line.split()[3].endswith(f":{port}") for line in proc.stdout.splitlines() if len(line.split()) >= 4)


def display_env(display: str) -> dict[str, str]:
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["XAUTHORITY"] = env.get("XAUTHORITY", "")
    return env


def run_command(command: list[str], *, capture: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"cwd": PACKAGE_ROOT, "env": env or os.environ.copy(), "text": True, "check": False}
    if capture:
        kwargs.update({"capture_output": True})
    try:
        return subprocess.run(command, **kwargs)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def print_payload(payload: dict[str, Any], as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(summary)


def safe_slug(value: str) -> str:
    keep = [char.lower() if char.isalnum() else "-" for char in value]
    return "-".join("".join(keep).strip("-").split("-")) or "wechat"
