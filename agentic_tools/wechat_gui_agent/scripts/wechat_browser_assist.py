#!/usr/bin/env python3
"""Open a human-assist browser inside the isolated WeChat virtual desktop."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"
DEFAULT_DISPLAY = ":97"
DEFAULT_PREVIEW_PORT = 6108
VERIFICATION_TEXT_MARKERS = (
    "环境异常",
    "完成验证后继续访问",
    "请完成验证",
    "安全验证",
    "访问环境异常",
    "verify you are human",
    "captcha",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="about:blank", help="URL to open for manual login/download help.")
    parser.add_argument("--display", default=DEFAULT_DISPLAY, help="X display for the isolated virtual desktop.")
    parser.add_argument("--browser", help="Browser command. Defaults to WECHAT_BROWSER_COMMAND or an installed browser.")
    parser.add_argument("--wait-seconds", type=float, default=0.0, help="Seconds to wait after launch before capture/close.")
    parser.add_argument("--reuse-window", action="store_true", help="Reuse the largest visible browser window and navigate it to the URL instead of opening another window.")
    parser.add_argument("--capture", action="store_true", help="Capture visible browser text and screenshot into the private assist folder.")
    parser.add_argument("--wait-readable-seconds", type=float, default=0.0, help="Keep the visible browser open and retry capture until the page text looks readable or this timeout expires.")
    parser.add_argument("--poll-seconds", type=float, default=3.0, help="Polling interval for --wait-readable-seconds.")
    parser.add_argument("--close-after", action="store_true", help="Close the browser window after the optional wait/capture.")
    parser.add_argument("--output-dir", type=Path, help="Private output directory for captures. Defaults under .private/browser_assist/captures.")
    parser.add_argument("--allow-mp-weixin", action="store_true", help="Explicitly allow external browser use for mp.weixin.qq.com. Disabled by default to avoid locking/focus issues with WeChat.")
    parser.add_argument("--dry-run", action="store_true", help="Print the launch plan without opening a browser.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if is_mp_weixin_url(args.url) and not mp_weixin_external_browser_allowed(args.allow_mp_weixin):
        payload = {
            "ok": False,
            "status": "blocked-mp-weixin-external-browser",
            "display": args.display,
            "url": args.url,
            "novnc_url": novnc_url(),
            "manual_required": True,
            "message": (
                "Refusing to open mp.weixin.qq.com in an external browser by default. "
                "Use wechat_source_recovery.py for read-only article extraction/reconstruction without GUI verification. "
                "External browser use for mp.weixin requires --allow-mp-weixin or WECHAT_ALLOW_EXTERNAL_BROWSER_FOR_MP_WEIXIN=1."
            ),
        }
        print_payload(payload, args.json)
        return 2

    browser = resolve_browser(args.browser)
    if not browser:
        payload = {
            "ok": False,
            "status": "browser-missing",
            "display": args.display,
            "url": args.url,
            "novnc_url": novnc_url(),
            "message": "Install firefox, chromium, or set WECHAT_BROWSER_COMMAND.",
        }
        print_payload(payload, args.json)
        return 1

    profile_dir = PRIVATE / "browser_assist" / browser_profile_name(browser)
    command = browser_command(browser, args.url, profile_dir)
    payload = {
        "ok": True,
        "status": "dry-run" if args.dry_run else "launched",
        "display": args.display,
        "url": args.url,
        "browser": browser,
        "profile_dir": redacted_path(profile_dir),
        "command": " ".join(shlex.quote(part) for part in command),
        "wait_seconds": args.wait_seconds,
        "reuse_window": bool(args.reuse_window),
        "capture": bool(args.capture),
        "wait_readable_seconds": args.wait_readable_seconds,
        "poll_seconds": args.poll_seconds,
        "close_after": bool(args.close_after),
        "novnc_url": novnc_url(),
        "manual_required": True,
        "message": "Open the noVNC URL and complete login, CAPTCHA, article verification, download confirmation, or file save manually.",
    }
    if args.dry_run:
        print_payload(payload, args.json)
        return 0

    profile_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DISPLAY"] = args.display
    env["XAUTHORITY"] = env.get("XAUTHORITY", "")
    proc: subprocess.Popen | None = None
    window: dict[str, int | str] | None = None
    if args.reuse_window:
        window = find_browser_window(browser, env)
        if window:
            payload["status"] = "reused"
            payload["window"] = {"id": window["id"], "x": window["x"], "y": window["y"], "width": window["width"], "height": window["height"]}
            payload["navigation"] = navigate_browser_window(window, env, args.url)
    if not window:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        payload["pid"] = proc.pid
        payload["launched_at"] = datetime.now().isoformat(timespec="seconds")
    if args.wait_seconds > 0 or args.capture or args.wait_readable_seconds > 0 or args.close_after:
        time.sleep(max(0.0, args.wait_seconds))
        env_window = dict(env)
        window = window or find_browser_window(browser, env_window)
        if window:
            payload["window"] = {"id": window["id"], "x": window["x"], "y": window["y"], "width": window["width"], "height": window["height"]}
            if args.url and args.url != "about:blank" and payload.get("status") != "reused":
                payload["load_probe"] = ensure_browser_url_loaded(window, env_window)
        else:
            payload["window"] = None
            payload["window_warning"] = "No visible browser window found for capture/close."
        if (args.capture or args.wait_readable_seconds > 0) and window:
            capture_dir = args.output_dir or (PRIVATE / "browser_assist" / "captures")
            payload["artifacts"] = capture_browser_window(window, env_window, capture_dir, url=args.url)
            payload["readability"] = payload["artifacts"].get("readability")
        if args.wait_readable_seconds > 0 and window and not page_text_is_readable(payload.get("artifacts", {}).get("readability")):
            payload["readability_attempts"] = wait_for_readable_capture(
                window,
                env_window,
                args.output_dir or (PRIVATE / "browser_assist" / "captures"),
                url=args.url,
                timeout_seconds=args.wait_readable_seconds,
                poll_seconds=args.poll_seconds,
            )
            if payload["readability_attempts"]:
                latest = payload["readability_attempts"][-1]
                payload["artifacts"] = latest.get("artifacts", payload.get("artifacts"))
                payload["readability"] = latest.get("readability", payload.get("readability"))
        if args.close_after:
            payload["close"] = close_browser_window(window, proc, env_window)
    print_payload(payload, args.json)
    return 0


def resolve_browser(raw: str | None) -> str:
    if raw:
        return raw
    if os.environ.get("WECHAT_BROWSER_COMMAND"):
        return os.environ["WECHAT_BROWSER_COMMAND"]
    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser", "firefox"):
        path = shutil.which(name)
        if path:
            return path
    return ""


def is_mp_weixin_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return host == "mp.weixin.qq.com" or host.endswith(".mp.weixin.qq.com")


def mp_weixin_external_browser_allowed(flag: bool) -> bool:
    return bool(flag) or os.environ.get("WECHAT_ALLOW_EXTERNAL_BROWSER_FOR_MP_WEIXIN", "").strip().lower() in {"1", "true", "yes", "on"}


def browser_profile_name(browser: str) -> str:
    name = Path(shlex.split(browser)[0]).name.lower()
    if "firefox" in name:
        return "firefox-profile"
    if "chrome" in name or "chromium" in name or "brave" in name:
        return "chromium-profile"
    return "browser-profile"


def browser_command(browser: str, url: str, profile_dir: Path) -> list[str]:
    parts = shlex.split(browser)
    name = Path(parts[0]).name.lower()
    if "firefox" in name:
        return parts + ["--no-remote", "--profile", str(profile_dir), "--new-window", url]
    if "chrome" in name or "chromium" in name or "brave" in name:
        return parts + [f"--user-data-dir={profile_dir}", "--new-window", url]
    return parts + [url]


def navigate_browser_window(window: dict[str, int | str], env: dict[str, str], url: str) -> dict[str, Any]:
    wid = str(window["id"])
    payload: dict[str, Any] = {"attempted": True, "url": url}
    try:
        run(["xdotool", "windowactivate", "--sync", wid], env=env, check=False)
        time.sleep(0.1)
        run(["xdotool", "key", "--clearmodifiers", "ctrl+l"], env=env, check=False)
        time.sleep(0.1)
        if shutil.which("xclip"):
            set_clipboard(url, env)
            run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], env=env, check=False)
        else:
            run(["xdotool", "type", "--clearmodifiers", "--delay", "1", url], env=env, check=False)
        run(["xdotool", "key", "--clearmodifiers", "Return"], env=env, check=False)
        payload["ok"] = True
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return payload


def browser_window_classes(browser: str) -> list[str]:
    name = Path(shlex.split(browser)[0]).name.lower()
    if "firefox" in name:
        return ["firefox"]
    if "chrome" in name:
        return ["google-chrome", "chrome"]
    if "chromium" in name:
        return ["chromium", "chromium-browser"]
    if "brave" in name:
        return ["brave-browser", "brave"]
    return [name]


def find_browser_window(browser: str, env: dict[str, str]) -> dict[str, int | str] | None:
    best: dict[str, int | str] | None = None
    best_area = 0
    seen: set[str] = set()
    for window_class in browser_window_classes(browser):
        proc = run(["xdotool", "search", "--onlyvisible", "--class", window_class], env=env, check=False)
        for wid in proc.stdout.split():
            if wid in seen:
                continue
            seen.add(wid)
            window = window_geometry(wid, env)
            if not window:
                continue
            area = int(window["width"]) * int(window["height"])
            if area > best_area:
                best = window
                best_area = area
    return best


def window_geometry(wid: str, env: dict[str, str]) -> dict[str, int | str] | None:
    title = run(["xdotool", "getwindowname", wid], env=env, check=False).stdout.strip()
    if title.lower().startswith("close "):
        return None
    geom = run(["xdotool", "getwindowgeometry", "--shell", wid], env=env, check=False).stdout
    values = dict(line.split("=", 1) for line in geom.splitlines() if "=" in line)
    try:
        width = int(values.get("WIDTH", "0"))
        height = int(values.get("HEIGHT", "0"))
        if width < 200 or height < 200:
            return None
        return {
            "id": wid,
            "title": title,
            "x": int(values.get("X", "0")),
            "y": int(values.get("Y", "0")),
            "width": width,
            "height": height,
        }
    except ValueError:
        return None


def capture_browser_window(window: dict[str, int | str], env: dict[str, str], output_dir: Path, *, url: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = output_dir / f"browser_assist_{stamp}_{safe_file_token(url)}"
    screenshot = base.with_suffix(".png")
    text_path = base.with_suffix(".txt")
    metadata_path = base.with_suffix(".json")
    artifacts: dict[str, Any] = {"output_dir": redacted_path(output_dir)}
    wid = str(window["id"])
    try:
        run(["xdotool", "windowactivate", "--sync", wid], env=env, check=False)
        run(["xdotool", "key", "--clearmodifiers", "Escape"], env=env, check=False)
        time.sleep(0.2)
        click_x = int(window["x"]) + max(10, int(window["width"]) // 2)
        click_y = int(window["y"]) + max(80, int(window["height"]) // 2)
        run(["xdotool", "mousemove", str(click_x), str(click_y), "click", "1"], env=env, check=False)
        time.sleep(0.2)
        if shutil.which("import"):
            proc = run(["import", "-window", wid, str(screenshot)], env=env, check=False)
            if proc.returncode == 0 and screenshot.is_file():
                artifacts["screenshot"] = str(screenshot)
            else:
                artifacts["screenshot_error"] = (proc.stderr or proc.stdout or "").strip()[:500]
        run(["xdotool", "key", "--clearmodifiers", "ctrl+a"], env=env, check=False)
        time.sleep(0.1)
        run(["xdotool", "key", "--clearmodifiers", "ctrl+c"], env=env, check=False)
        time.sleep(0.2)
        clip = run(["xclip", "-selection", "clipboard", "-o"], env=env, check=False)
        text = (clip.stdout or "").strip()
        if text:
            text_path.write_text(text[:200000], encoding="utf-8", errors="replace")
            artifacts["text"] = str(text_path)
            artifacts["text_chars"] = len(text)
            artifacts["readability"] = browser_text_readability(text)
        else:
            artifacts["text_warning"] = "No selectable browser text was captured."
            artifacts["readability"] = browser_text_readability("")
    except Exception as exc:
        artifacts["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    metadata = {
        "url": url,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "window": window,
        "artifacts": artifacts,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifacts["metadata"] = str(metadata_path)
    return artifacts


def wait_for_readable_capture(
    window: dict[str, int | str],
    env: dict[str, str],
    output_dir: Path,
    *,
    url: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    deadline = time.time() + max(0.0, timeout_seconds)
    interval = max(0.5, poll_seconds)
    while time.time() < deadline:
        time.sleep(interval)
        artifacts = capture_browser_window(window, env, output_dir, url=url)
        readability = artifacts.get("readability", {})
        attempt = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "readability": readability,
            "artifacts": artifacts,
        }
        attempts.append(attempt)
        if page_text_is_readable(readability):
            break
    return attempts


def browser_text_readability(text: str) -> dict[str, Any]:
    collapsed = " ".join(str(text or "").split())
    lowered = collapsed.lower()
    matched = [marker for marker in VERIFICATION_TEXT_MARKERS if marker.lower() in lowered]
    chars = len(collapsed)
    return {
        "text_chars": chars,
        "verification_blocked": bool(matched),
        "verification_markers": matched[:5],
        "readable": chars >= 500 and not matched,
        "summary": "readable" if chars >= 500 and not matched else ("verification-blocked" if matched else "no-readable-text"),
    }


def page_text_is_readable(readability: Any) -> bool:
    return isinstance(readability, dict) and bool(readability.get("readable"))


def ensure_browser_url_loaded(window: dict[str, int | str], env: dict[str, str]) -> dict[str, Any]:
    wid = str(window["id"])
    payload: dict[str, Any] = {"attempted": True}
    try:
        run(["xdotool", "windowactivate", "--sync", wid], env=env, check=False)
        run(["xdotool", "key", "--clearmodifiers", "Return"], env=env, check=False)
        time.sleep(float(os.environ.get("WECHAT_BROWSER_ASSIST_LOAD_AFTER_ENTER_SECONDS", "2")))
        run(["xdotool", "key", "--clearmodifiers", "Escape"], env=env, check=False)
        payload["ok"] = True
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return payload


def close_browser_window(window: dict[str, int | str] | None, proc: subprocess.Popen | None, env: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"requested": True}
    if window:
        wid = str(window["id"])
        result = run(["xdotool", "windowclose", wid], env=env, check=False)
        payload["window_id"] = wid
        payload["windowclose_returncode"] = result.returncode
    time.sleep(0.5)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        payload["process_terminated"] = True
    elif proc is not None:
        payload["process_returncode"] = proc.returncode
    return payload


def set_clipboard(text: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["xclip", "-selection", "clipboard"],
        env=env,
        input=text,
        capture_output=True,
        text=True,
        check=False,
        timeout=float(os.environ.get("WECHAT_BROWSER_ASSIST_COMMAND_TIMEOUT", "10")),
    )


def run(command: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=float(os.environ.get("WECHAT_BROWSER_ASSIST_COMMAND_TIMEOUT", "10")),
    )


def safe_file_token(value: str) -> str:
    token = "".join(char if char.isalnum() else "-" for char in value.lower())
    token = "-".join(part for part in token.split("-") if part)
    return token[:64] or "page"


def novnc_url() -> str:
    return f"http://127.0.0.1:{DEFAULT_PREVIEW_PORT}/"


def redacted_path(path: Path) -> str:
    text = str(path)
    root = str(ROOT)
    if text.startswith(root):
        return "<repo>" + text[len(root) :]
    home = str(Path.home())
    if text.startswith(home):
        return "~" + text[len(home) :]
    return text


def print_payload(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(payload["message"])
    print(f"noVNC: {payload['novnc_url']}")
    if payload.get("command"):
        print(f"command: {payload['command']}")


if __name__ == "__main__":
    raise SystemExit(main())
