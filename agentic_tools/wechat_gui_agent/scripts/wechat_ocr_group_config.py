#!/usr/bin/env python3
"""Create a private OCR-only personal-WeChat group Cloud config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-name", required=True, help="Exact visible WeChat group title.")
    parser.add_argument("--trigger", default="@OverTree", help="Exact visible mention prefix, including @.")
    parser.add_argument("--config-name", default="")
    parser.add_argument("--display", default=os.environ.get("WECHAT_DISPLAY", ":97"))
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--chat-left", type=int, default=390)
    parser.add_argument("--chat-top", type=int, default=100)
    parser.add_argument("--chat-bottom", type=int, default=170)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    name = args.group_name.strip()
    trigger = args.trigger.strip()
    if not name or not trigger.startswith(("@", "＠")):
        raise SystemExit("group name and an @ trigger are required")
    private = Path(os.environ.get("WECHAT_AGENT_PRIVATE_DIR", str(Path.home() / ".local/share/overtree-wechat/labcanvas-private"))).expanduser().resolve()
    env_file = (args.env_file or Path(os.environ.get("WECHAT_AGENT_ENV_FILE", str(private / "wechat_supervisor.local.env")))).expanduser().resolve()
    values = read_env(env_file)
    if not values.get("WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID") or not values.get("WECHAT_PERSONAL_GROUP_CLOUD_URL") or len(values.get("WECHAT_PERSONAL_GROUP_SHARED_SECRET", "")) < 32:
        raise SystemExit("Configure the personal-group Cloud URL, Agent account ID, and 32+ character secret first.")
    stem = slug(args.config_name or name)
    # Several Chinese titles can have the same ASCII fallback. Add a stable
    # suffix so each private config and state file remains distinct.
    if not args.config_name and stem == "wechat-group":
        stem = f"wechat-group-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]}"
    state_path = (args.state_path or (private / f"{stem}-ocr.state.json")).expanduser().resolve()
    try:
        state_path.relative_to(private)
    except ValueError as exc:
        raise SystemExit("state path must stay inside the private Agent-WeChat directory") from exc
    # The title-derived ID is stable for this controlled GUI route. It is not a
    # claimed WeChat internal identifier and is never used to read local data.
    group_id = "ocr-title-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    path = private / f"{stem}-ocr-group.json"
    config = {
        "enabled": bool(args.enable),
        "ingress": "ocr_gui",
        "chat_name": name,
        "chatroom_id": group_id,
        "display": args.display,
        "state_path": str(state_path),
        "send_target": {"name": name, "query": name, "expected_title": name},
        "personal_group_cloud": {"enabled": bool(args.enable), "trigger_prefixes": [trigger, trigger.replace("@", "＠", 1)], "artifact_base_url": values.get("WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL", "https://cloud.overtree.top")},
        "chat_region": {"left": args.chat_left, "top": args.chat_top, "right": 0, "bottom": args.chat_bottom},
        "ocr_psm": 11,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    configs = [item.strip() for item in values.get("WECHAT_OCR_GROUP_CONFIGS", "").split(",") if item.strip()]
    if str(path) not in configs:
        configs.append(str(path))
    allowlist = [item.strip() for item in values.get("WECHAT_PERSONAL_GROUP_ALLOWLIST", "").split(",") if item.strip()]
    if group_id not in allowlist:
        allowlist.append(group_id)
    updates = {
        "WECHAT_OCR_GROUP_CONFIGS": ",".join(configs),
        "WECHAT_PERSONAL_GROUP_ALLOWLIST": ",".join(allowlist),
    }
    if args.enable:
        updates["WECHAT_OCR_GROUP_ENABLED"] = "true"
    write_env(env_file, updates)
    print(f"created={path}")
    print(f"group_id={group_id}")
    print(f"enabled={'true' if args.enable else 'false'}")
    print("The group_id is derived from the exact title for the OCR transport; it is not a WeChat database ID.")
    return 0


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")[:80] or "wechat-group"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    lines, written = [], set()
    for raw in existing:
        key = raw.split("=", 1)[0].strip() if "=" in raw else ""
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            lines.append(raw)
    lines.extend(f"{key}={value}" for key, value in updates.items() if key not in written)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
