#!/usr/bin/env python3
"""Private wrapper for direct WeChat local-store monitoring/decryption backends."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib import parse, request

from wechat_message_shards import list_message_db_paths
from wechat_mirror import DEFAULT_DB, record_event


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve()
EXTERNAL = PRIVATE / "external" / "wechat-decrypt"
DEFAULT_REPO = "https://github.com/ylytdeng/wechat-decrypt.git"
DEFAULT_WEB_PORT = 5678


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", default="wechat-chat")
    parser.add_argument("--external", type=Path, default=EXTERNAL)
    parser.add_argument("--db-dir", type=Path, default=None, help="Path to xwechat_files/<WXID>/db_storage. Auto-discovers when omitted.")
    parser.add_argument("--mirror-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true", help="Print JSON payloads for commands that support it.")
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="Clone/update the private wechat-decrypt checkout and prepare the venv.")
    install.add_argument("--repo", default=DEFAULT_REPO)
    install.add_argument("--update", action="store_true", help="Pull the existing private checkout with --ff-only.")
    install.add_argument("--skip-deps", action="store_true", help="Create the venv but do not pip install requirements.")
    sub.add_parser("init-config", help="Write wechat-decrypt config.json pointing at private paths.")
    sub.add_parser("status", help="Show sanitized external-backend status.")
    sub.add_parser("probe", help="Validate external backend readiness without reading messages.")
    sub.add_parser("source-dir", help="Print the selected private source directory for the refresh supervisor.")
    sub.add_parser("find-keys", help="Run Linux memory key scan. Requires root or CAP_SYS_PTRACE.")
    decrypt = sub.add_parser("decrypt", help="Decrypt WeChat DBs into the private decrypted cache.")
    decrypt.add_argument("--incremental", action="store_true", help="Skip unchanged source DBs.")
    decrypt.add_argument("--dry-run", action="store_true", help="Preview external decrypt plan.")
    sub.add_parser("monitor", help="Run the external CLI monitor in the foreground.")
    web = sub.add_parser("monitor-web", help="Run the external Web UI/SSE monitor in the foreground.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="Local-only web monitor port.")
    history = sub.add_parser("api-history", help="Read external monitor_web /api/history.")
    history.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--chat", default="")
    history.add_argument("--since", type=int, default=0)
    history.add_argument("--raw", action="store_true", help="Print raw private message payloads from the local monitor API.")
    sub.add_parser("mcp-server", help="Run the external MCP server in the foreground.")
    sub.add_parser("mcp-config", help="Emit MCP client config for the private external server.")
    args = parser.parse_args()

    if args.command == "install":
        payload = install_external(args.external, args.repo, update=args.update, install_deps=not args.skip_deps)
        print_json_or_text(payload, args.json, "external decryptor: " + payload["status"])
        return 0 if payload["ok"] else 1

    db_dir = resolve_db_dir(args.db_dir, required=args.command not in {"status", "mcp-config"})

    if args.command == "source-dir":
        print(db_dir)
        return 0

    if args.command == "init-config":
        if db_dir is None:
            raise SystemExit("No local WeChat db_storage found; pass --db-dir explicitly.")
        config_path = write_external_config(args.external, db_dir)
        payload = {"ok": True, "config_path": public_path(config_path), "external": public_path(args.external)}
        print_json_or_text(payload, args.json, str(config_path))
        record_event(
            chat_name=args.chat,
            action="direct_backend",
            status="config-written",
            db_path=args.mirror_db,
            metadata={"config_path": str(config_path), "db_dir": str(db_dir), "external": str(args.external)},
        )
        return 0
    if args.command == "status":
        payload = status(args.external, db_dir)
        print_json_or_text(payload, args.json, "external decryptor: " + payload["status"])
        return 0 if payload["ok"] else 1
    if args.command == "probe":
        payload = probe(args.external, db_dir)
        print_json_or_text(payload, args.json, "external decryptor probe: " + ("ok" if payload["ok"] else "attention needed"))
        return 0 if payload["ok"] else 1
    if args.command == "find-keys":
        if db_dir is None:
            raise SystemExit("No local WeChat db_storage found; pass --db-dir explicitly.")
        config_path = write_external_config(args.external, db_dir)
        return run_external(args.external, [str(venv_python()), "find_all_keys_linux.py"], config_path)
    if args.command == "decrypt":
        if db_dir is None:
            raise SystemExit("No local WeChat db_storage found; pass --db-dir explicitly.")
        config_path = write_external_config(args.external, db_dir)
        command = [str(venv_python()), "decrypt_db.py"]
        if args.incremental:
            command.append("--incremental")
        if args.dry_run:
            command.append("--dry-run")
        return run_decrypt(args.external, command, config_path, dry_run=args.dry_run)
    if args.command == "monitor":
        if db_dir is None:
            raise SystemExit("No local WeChat db_storage found; pass --db-dir explicitly.")
        config_path = write_external_config(args.external, db_dir)
        return run_external(args.external, [str(venv_python()), "monitor.py"], config_path)
    if args.command == "monitor-web":
        if db_dir is None:
            raise SystemExit("No local WeChat db_storage found; pass --db-dir explicitly.")
        config_path = write_external_config(args.external, db_dir)
        launcher = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_decrypt_monitor_web_local.py"
        return run_external(
            args.external,
            [str(venv_python()), str(launcher), "--external", str(args.external), "--host", args.host, "--port", str(args.port)],
            config_path,
        )
    if args.command == "api-history":
        payload = external_api_history(args.port, limit=args.limit, chat=args.chat, since=args.since, raw=args.raw)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    if args.command == "mcp-server":
        if db_dir is None:
            raise SystemExit("No local WeChat db_storage found; pass --db-dir explicitly.")
        config_path = write_external_config(args.external, db_dir)
        return run_external(args.external, [str(venv_python()), "mcp_server.py"], config_path)
    if args.command == "mcp-config":
        payload = {
            "ok": True,
            "mcpServers": {
                "wechat-decrypt": {
                    "command": str(venv_python()),
                    "args": [str(args.external / "mcp_server.py")],
                    "cwd": str(args.external),
                }
            },
            "notes": ["private paths are local-only; do not commit generated client config with account data"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    return 0


def resolve_db_dir(raw: Path | None, *, required: bool) -> Path | None:
    if raw:
        return raw
    try:
        return discover_xwechat_db()
    except SystemExit:
        if required:
            raise
        return None


def discover_xwechat_db() -> Path:
    root = Path.home() / "Documents" / "xwechat_files"
    candidates = [path for path in root.glob("*/db_storage") if path.is_dir()]
    if not candidates:
        raise SystemExit(f"No local WeChat db_storage found under {root}; pass --db-dir explicitly.")
    active = active_wechat_db_dirs(candidates)
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise SystemExit("Multiple live WeChat account stores; specify --db-dir instead of guessing.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def active_wechat_db_dirs(candidates: list[Path], proc_root: Path = Path("/proc")) -> list[Path]:
    """Identify the open account store without controlling or stopping WeChat."""
    known = {path.resolve() for path in candidates}
    active: set[Path] = set()
    for process in proc_root.glob("[0-9]*"):
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if (process / "comm").read_text().strip().casefold() not in {"wechat", "weixin"}:
                continue
            for fd in (process / "fd").iterdir():
                try:
                    target = fd.resolve(strict=True)
                except OSError:
                    continue
                active.update(parent for parent in target.parents if parent in known)
        except OSError:
            continue
    return sorted(active)


def decrypt_summary(output: str) -> dict:
    """The external CLI has no JSON result and exits zero even on HMAC failure."""
    match = re.search(r"结果:\s*(\d+)\s*成功,\s*(\d+)\s*失败,\s*(\d+)\s*跳过", output)
    if not match:
        return {"ok": False, "status": "decrypt_summary_missing"}
    success, failed, skipped = map(int, match.groups())
    missing_paths = re.findall(r"(?m)^SKIP:\s*(.+?)\s*\(无密钥", output)
    required_missing = [
        path for path in missing_paths
        if re.fullmatch(r"message/(?:message_\d+|media_\d+)\.db|contact/contact\.db|session/session\.db", path)
    ]
    # Optional payment/search/tip databases are not part of the chat intake.
    ok = failed == 0 and not required_missing and len(missing_paths) == skipped
    return {
        "ok": ok,
        "status": ("ready_partial_optional" if skipped else "ready") if ok else "decrypt_failed",
        "success_count": success, "failed_count": failed, "missing_key_count": skipped,
        "required_missing_count": len(required_missing),
    }


def run_decrypt(external: Path, command: list[str], config_path: Path, *, dry_run: bool = False) -> int:
    env = os.environ.copy()
    env["WECHAT_DECRYPT_APP_DIR"] = str(external)
    with tempfile.TemporaryFile() as output:
        proc = subprocess.run(command, cwd=external, env=env, stdout=output, stderr=output, check=False)
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 65536))
        tail = output.read().decode("utf-8", errors="replace")
    if dry_run:
        print(tail)
        return proc.returncode
    summary = decrypt_summary(tail)
    summary["ok"] = summary["ok"] and proc.returncode == 0
    summary["returncode"] = proc.returncode
    summary["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    state_path = PRIVATE / "wechat_decrypt.refresh.status.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(state_path)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["ok"] else (proc.returncode or 1)


def install_external(external: Path, repo: str, *, update: bool, install_deps: bool) -> dict:
    external = external.resolve()
    external.parent.mkdir(parents=True, exist_ok=True)
    actions: list[dict[str, object]] = []
    if not external.exists():
        proc = run_capture(["git", "clone", repo, str(external)], cwd=external.parent)
        actions.append(command_record("git clone", proc))
        if proc.returncode != 0:
            return {"ok": False, "status": "clone-failed", "external": public_path(external), "actions": actions}
    elif update:
        if not (external / ".git").exists():
            return {"ok": False, "status": "external-exists-without-git", "external": public_path(external), "actions": actions}
        proc = run_capture(["git", "-C", str(external), "pull", "--ff-only"], cwd=external)
        actions.append(command_record("git pull", proc))
        if proc.returncode != 0:
            return {"ok": False, "status": "update-failed", "external": public_path(external), "actions": actions}

    venv_dir = PRIVATE / "wechat_decrypt" / ".venv"
    if not (venv_dir / "bin" / "python").exists():
        proc = run_capture([sys.executable, "-m", "venv", str(venv_dir)], cwd=ROOT)
        actions.append(command_record("python -m venv", proc))
        if proc.returncode != 0:
            return {"ok": False, "status": "venv-failed", "external": public_path(external), "actions": actions}

    if install_deps and (external / "requirements.txt").exists():
        proc = run_capture([str(venv_dir / "bin" / "python"), "-m", "pip", "install", "-r", str(external / "requirements.txt")], cwd=external)
        actions.append(command_record("pip install requirements", proc))
        if proc.returncode != 0:
            return {"ok": False, "status": "deps-failed", "external": public_path(external), "actions": actions}

    return {**status(external, resolve_db_dir(None, required=False)), "actions": actions, "status": "installed"}


def write_external_config(external: Path, db_dir: Path) -> Path:
    if not external.exists():
        raise SystemExit(f"Missing private external decryptor: {external}")
    config = {
        "db_dir": str(db_dir),
        "keys_file": str((PRIVATE / "wechat_decrypt" / "all_keys.json").resolve()),
        "decrypted_dir": str((PRIVATE / "wechat_decrypt" / "decrypted").resolve()),
        "decoded_image_dir": str((PRIVATE / "wechat_decrypt" / "decoded_images").resolve()),
        "wechat_process": "wechat",
        "wxwork_db_dir": "",
        "wxwork_keys_file": str((PRIVATE / "wechat_decrypt" / "wxwork_keys.json").resolve()),
        "wxwork_decrypted_dir": str((PRIVATE / "wechat_decrypt" / "wxwork_decrypted").resolve()),
        "wxwork_export_dir": str((PRIVATE / "wechat_decrypt" / "wxwork_export").resolve()),
        "wxwork_process": "WXWork.exe",
        "transcription_backend": "local",
        "local_whisper_model": "base",
        "openai_api_key": "",
    }
    (PRIVATE / "wechat_decrypt").mkdir(parents=True, exist_ok=True)
    config_path = external / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path.chmod(0o600)
    return config_path


def status(external: Path, db_dir: Path | None) -> dict:
    keys_file = PRIVATE / "wechat_decrypt" / "all_keys.json"
    decrypted_dir = PRIVATE / "wechat_decrypt" / "decrypted"
    config_path = external / "config.json"
    scripts = {
        name: (external / name).exists()
        for name in ("decrypt_db.py", "find_all_keys_linux.py", "monitor.py", "monitor_web.py", "mcp_server.py", "requirements.txt")
    }
    source_db_count = count_source_dbs(db_dir) if db_dir else 0
    decrypted_db_count = count_source_dbs(decrypted_dir) if decrypted_dir.exists() else 0
    source_message_dbs = list_message_db_paths(db_dir / "message") if db_dir else []
    decrypted_message_dbs = list_message_db_paths(decrypted_dir / "message")
    git = git_info(external) if external.exists() else {}
    web_port = DEFAULT_WEB_PORT
    return {
        "ok": external.exists() and bool(db_dir and db_dir.exists()) and bool(scripts.get("decrypt_db.py")),
        "status": "ready" if external.exists() and bool(db_dir and db_dir.exists()) else "not-ready",
        "external_exists": external.exists(),
        "external": public_path(external),
        "external_git": git,
        "venv_python_exists": (PRIVATE / "wechat_decrypt" / ".venv" / "bin" / "python").exists(),
        "db_dir": public_path(db_dir) if db_dir else "",
        "db_dir_exists": bool(db_dir and db_dir.exists()),
        "message_db_exists": bool(source_message_dbs),
        "message_db_count": len(source_message_dbs),
        "source_db_count": source_db_count,
        "config_path": public_path(config_path),
        "config_exists": config_path.exists(),
        "keys_file_exists": keys_file.exists(),
        "keys_file_mtime": file_mtime(keys_file),
        "decrypted_dir_exists": decrypted_dir.exists(),
        "decrypted_db_count": decrypted_db_count,
        "decrypted_message_db_exists": bool(decrypted_message_dbs),
        "decrypted_message_db_count": len(decrypted_message_dbs),
        "scripts": scripts,
        "monitor_web": {"port": web_port, "listening": port_listening(web_port)},
        "private_paths_redacted": True,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def probe(external: Path, db_dir: Path | None) -> dict:
    payload = status(external, db_dir)
    checks = {
        "external_exists": payload["external_exists"],
        "venv_python_exists": payload["venv_python_exists"],
        "db_dir_exists": payload["db_dir_exists"],
        "decrypt_script_exists": payload["scripts"].get("decrypt_db.py", False),
        "key_scanner_exists": payload["scripts"].get("find_all_keys_linux.py", False),
        "keys_file_exists": payload["keys_file_exists"],
        "decrypted_message_db_exists": payload["decrypted_message_db_exists"],
    }
    required = ["external_exists", "venv_python_exists", "db_dir_exists", "decrypt_script_exists", "key_scanner_exists"]
    ok = all(checks[name] for name in required)
    return {**payload, "ok": ok, "checks": checks, "status": "probe-ok" if ok else "probe-attention"}


def run_external(external: Path, command: list[str], config_path: Path) -> int:
    env = os.environ.copy()
    env["WECHAT_DECRYPT_APP_DIR"] = str(external)
    proc = subprocess.run(command, cwd=external, env=env, check=False)
    if proc.returncode != 0:
        print(f"{command} failed with code {proc.returncode}; config={config_path}", file=sys.stderr)
    return proc.returncode


def venv_python() -> Path:
    candidate = PRIVATE / "wechat_decrypt" / ".venv" / "bin" / "python"
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def external_api_history(port: int, *, limit: int, chat: str = "", since: int = 0, raw: bool = False) -> dict:
    query = {"limit": str(limit)}
    if chat:
        query["chat"] = chat
    if since:
        query["since"] = str(since)
    url = f"http://127.0.0.1:{port}/api/history?{parse.urlencode(query)}"
    try:
        with request.urlopen(url, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "status": "api-unreachable", "port": port, "error": str(exc)}
    if raw:
        return {"ok": True, "status": "ok", "port": port, "count": len(data) if isinstance(data, list) else None, "messages": data}
    messages = data if isinstance(data, list) else []
    recent = []
    for item in messages[-min(limit, 10) :]:
        if not isinstance(item, dict):
            continue
        recent.append(
            {
                "chat": str(item.get("chat") or item.get("username") or "")[:120],
                "timestamp": item.get("timestamp", ""),
                "type": item.get("type", item.get("msg_type", "")),
                "has_content": bool(item.get("content") or item.get("text")),
                "keys": sorted(str(key) for key in item.keys())[:20],
            }
        )
    return {"ok": True, "status": "ok", "port": port, "count": len(messages), "recent": recent, "raw_redacted": True}


def count_source_dbs(path: Path | None) -> int:
    if not path or not path.exists():
        return 0
    return sum(1 for item in path.rglob("*.db") if item.is_file() and not item.name.endswith(("-wal", "-shm")))


def file_mtime(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def git_info(path: Path) -> dict[str, object]:
    if not (path / ".git").exists():
        return {"exists": False}
    rev = run_capture(["git", "rev-parse", "--short", "HEAD"], cwd=path)
    remote = run_capture(["git", "remote", "get-url", "origin"], cwd=path)
    dirty = run_capture(["git", "status", "--short"], cwd=path)
    return {
        "exists": True,
        "head": rev.stdout.strip(),
        "origin": remote.stdout.strip(),
        "dirty": bool(dirty.stdout.strip()),
    }


def port_listening(port: int) -> bool:
    if not shutil.which("ss"):
        return False
    proc = run_capture(["ss", "-ltn"], cwd=ROOT)
    if proc.returncode != 0:
        return False
    return any(line.split()[3].endswith(f":{port}") for line in proc.stdout.splitlines() if len(line.split()) >= 4)


def public_path(path: Path | None) -> str:
    if path is None:
        return ""
    text = str(path)
    home = str(Path.home())
    if text.startswith(home):
        text = "~" + text[len(home) :]
    root = str(ROOT)
    if text.startswith(root):
        text = "<repo>" + text[len(root) :]
    text = re.sub(r"(xwechat_files/)[^/]+", r"\1<wechat-profile>", text)
    return text


def run_capture(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def command_record(label: str, proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "label": label,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def print_json_or_text(payload: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(text)


if __name__ == "__main__":
    raise SystemExit(main())
