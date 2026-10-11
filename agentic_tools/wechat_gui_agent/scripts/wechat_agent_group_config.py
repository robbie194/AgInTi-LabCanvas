#!/usr/bin/env python3
"""Create one private, exact-group Agent-WeChat Cloud monitor config."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re


def slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return result[:80] or "wechat-group"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = []
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written: set[str] = set()
    for raw in existing:
        key = raw.split("=", 1)[0].strip() if "=" in raw else ""
        if key in values:
            lines.append(f"{key}={values[key]}")
            written.add(key)
        else:
            lines.append(raw)
    for key, value in values.items():
        if key not in written:
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-name", required=True, help="Exact visible WeChat group title.")
    parser.add_argument("--group-id", required=True, help="Stable decrypted WeChat chatroom ID.")
    parser.add_argument("--self-wxid", required=True, help="Agent account's exact wxid.")
    parser.add_argument("--message-table", required=True, help="Exact decrypted message table, for example Msg_xxx.")
    parser.add_argument("--trigger", default="@OverTree", help="Exact in-group Agent mention prefix, including @.")
    parser.add_argument("--config-name", default="", help="Private config filename stem; defaults to the group name.")
    parser.add_argument("--enable", action="store_true", help="Enable this local Cloud group route and its local feature flag.")
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args()
    group_name = args.group_name.strip()
    group_id = args.group_id.strip()
    self_wxid = args.self_wxid.strip()
    message_table = args.message_table.strip()
    trigger = args.trigger.strip()
    if not group_name or not group_id or not self_wxid or not message_table:
        raise SystemExit("group name, group ID, self wxid, and message table are required")
    if not trigger.startswith(("@", "＠")) or len(trigger) < 2:
        raise SystemExit("--trigger must be an exact @Agent prefix")
    private = Path(os.environ.get("WECHAT_AGENT_PRIVATE_DIR", str(Path.home() / ".local/share/overtree-wechat/labcanvas-private"))).expanduser().resolve()
    env_file = (args.env_file or Path(os.environ.get("WECHAT_AGENT_ENV_FILE", str(private / "wechat_supervisor.local.env")))).expanduser().resolve()
    env = read_env(env_file)
    account_id = env.get("WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID", "").strip()
    cloud_url = env.get("WECHAT_PERSONAL_GROUP_CLOUD_URL", "").strip()
    secret = env.get("WECHAT_PERSONAL_GROUP_SHARED_SECRET", "").strip()
    artifact_url = env.get("WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL", "https://cloud.overtree.top").strip()
    if not account_id or not cloud_url or len(secret) < 32:
        raise SystemExit("Configure WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID, CLOUD_URL, and a 32+ character SHARED_SECRET first; the secret is never printed.")
    config_path = private / f"{slug(args.config_name or group_name)}-personal-group.json"
    state_path = private / f"{slug(args.config_name or group_name)}-personal-group.state.json"
    config = {
        "chat_name": group_name,
        "chatroom_id": group_id,
        "message_table": message_table,
        "self_wxid": self_wxid,
        "display": env.get("WECHAT_DISPLAY", ":97"),
        "trigger_prefixes": [trigger],
        "mirror_db": str(private / "wechat_mirror.sqlite"),
        "state_path": str(state_path),
        "respond_to_all": False,
        "respond_to_self": False,
        "ignore_self_messages": True,
        "chat_purpose": "research",
        "agent_backend": "aginti",
        "personal_group_cloud": {"enabled": bool(args.enable), "trigger_prefixes": [trigger], "artifact_base_url": artifact_url},
        "send_target": {"name": group_name, "query": group_name, "expected_title": group_name},
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path.chmod(0o600)
    configs = [item for item in env.get("WECHAT_DIRECT_CONFIGS", "").split(",") if item]
    if str(config_path) not in configs:
        configs.append(str(config_path))
    allowlist = [item for item in env.get("WECHAT_PERSONAL_GROUP_ALLOWLIST", "").split(",") if item]
    if group_id not in allowlist:
        allowlist.append(group_id)
    updates = {"WECHAT_DIRECT_CONFIGS": ",".join(configs), "WECHAT_PERSONAL_GROUP_ALLOWLIST": ",".join(allowlist), "WECHAT_PERSONAL_GROUP_CLOUD_URL": cloud_url, "WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL": artifact_url}
    if args.enable:
        updates["WECHAT_PERSONAL_GROUP_ENABLED"] = "true"
    write_env(env_file, updates)
    print(f"created={config_path}")
    print(f"group_id={group_id}")
    print(f"enabled={'true' if args.enable else 'false'}")
    print("secret=<redacted>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
