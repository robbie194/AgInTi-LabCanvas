#!/usr/bin/env python3
"""Copy recently downloaded WeChat media/files into the local private mirror."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import re
import struct
from io import BytesIO

from wechat_mirror import DEFAULT_DB, record_event, record_media_files


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = Path(os.environ.get(
    "WECHAT_AGENT_PRIVATE_DIR",
    str(ROOT / "agentic_tools" / "wechat_gui_agent" / ".private"),
)).expanduser().resolve()
DEFAULT_DEST = PRIVATE / "downloads"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--source", type=Path, action="append", default=[], help="WeChat download/cache directory. Repeatable.")
    parser.add_argument("--auto-source", action="store_true", help="Auto-discover local xwechat_files media folders.")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--since-minutes", type=float, default=60)
    parser.add_argument("--since-epoch", type=float, default=None, help="Copy files modified at or after this Unix timestamp.")
    parser.add_argument("--until-epoch", type=float, default=None, help="Copy files modified at or before this Unix timestamp.")
    parser.add_argument("--match-token", action="append", default=[], help="Also copy files whose path/name contains this token, regardless of age. Repeatable.")
    parser.add_argument(
        "--require-token-match",
        action="store_true",
        help="Reject mtime-only matches. Use for exact-chat task source resolution.",
    )
    parser.add_argument("--decode-dat", action=argparse.BooleanOptionalAction, default=True, help="Decode WeChat .dat image blobs when possible.")
    parser.add_argument("--image-aes-key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--image-xor-key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-only", action="store_true", help="Print counts and errors instead of every copied file.")
    parser.add_argument("--record-empty", action="store_true", help="Record mirror events even when no files matched.")
    args = parser.parse_args()

    sources = list(args.source)
    if args.auto_source:
        sources.extend(discover_sources())
    sources = unique_existing_dirs(sources)
    if not sources:
        raise SystemExit("No media source directories. Pass --source or --auto-source.")

    cutoff_epoch = args.since_epoch if args.since_epoch is not None else (datetime.now() - timedelta(minutes=args.since_minutes)).timestamp()
    until_epoch = args.until_epoch
    match_tokens = [token.lower() for token in args.match_token if token.strip()]
    image_aes_key, image_xor_key = image_decode_keys(args.image_aes_key, args.image_xor_key)
    copied = []
    errors = []
    seen_paths: set[Path] = set()
    associated: dict[Path, tuple[Path, str]] = {}
    for source in sources:
        if not source.exists():
            continue
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            matched_by = media_match_reason(
                path,
                stat.st_mtime,
                cutoff_epoch,
                until_epoch,
                match_tokens,
                require_token_match=bool(args.require_token_match),
            )
            if not matched_by:
                continue
            seen_paths.add(resolved)
            sync_candidate(path, source, matched_by, args, image_aes_key, image_xor_key, copied, errors)
            for fallback, reason in associated_media_paths(path, match_tokens):
                if fallback.resolve() not in seen_paths:
                    associated.setdefault(fallback.resolve(), (source, reason))
    for fallback, (source, reason) in associated.items():
        if fallback in seen_paths or not fallback.is_file():
            continue
        seen_paths.add(fallback)
        sync_candidate(fallback, source, reason, args, image_aes_key, image_xor_key, copied, errors)

    changed = [item for item in copied if item.get("status") in {"copied", "decoded", "error"}]
    status = "dry-run" if args.dry_run else "copied-with-errors" if errors else "copied"
    if not (changed if not args.dry_run else copied) and not args.record_empty:
        payload = {"event_id": None, "status": "no-changes", "file_count": 0, "error_count": 0, "errors": []}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    files_for_event = copied if args.dry_run else (changed or copied)
    event_id = record_event(
        chat_name=args.chat,
        action="media_sync",
        direction="inbound",
        status=status,
        db_path=args.db,
        message=json.dumps(files_for_event, ensure_ascii=False),
        metadata={
            "source_count": len(sources),
            "sources": [str(path) for path in sources],
            "file_count": len(files_for_event),
            "error_count": len(errors),
            "dest": str(args.dest),
            "layout": "<dest>/<chat>/<wechat-profile>/<category>/<relative-file>",
            "match_tokens": match_tokens,
            "require_token_match": bool(args.require_token_match),
            "since_epoch": cutoff_epoch,
            "until_epoch": until_epoch,
            "decode_dat": bool(args.decode_dat),
            "image_aes_key_available": bool(image_aes_key),
        },
    )
    recorded_files = record_media_files(chat_name=args.chat, event_id=event_id, files=files_for_event, db_path=args.db)
    if args.summary_only:
        payload = {
            "event_id": event_id,
            "status": status,
            "file_count": len(files_for_event),
            "error_count": len(errors),
            "recorded_files": recorded_files,
            "errors": errors,
        }
    else:
        payload = {"event_id": event_id, "status": status, "files": files_for_event, "recorded_files": recorded_files, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def sync_candidate(
    path: Path,
    source: Path,
    matched_by: str,
    args: argparse.Namespace,
    image_aes_key: bytes | None,
    image_xor_key: int,
    copied: list[dict],
    errors: list[dict],
) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    decode_result = decode_wechat_dat(path, image_aes_key=image_aes_key, image_xor_key=image_xor_key) if args.decode_dat else None
    decode_status = decode_result["status"] if decode_result else "not-needed"
    if args.decode_dat and path.suffix.lower() == ".dat" and decode_result is None:
        decode_status = "decode-unavailable"
    rel = decoded_relative_path(source, path, decode_result["format"]) if decode_result else target_relative_path(source, path)
    target = args.dest / safe_component(args.chat) / source_bucket(source) / rel
    item = {
        "source": str(path),
        "target": str(target),
        "bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "suffix": target.suffix.lower(),
        "matched_by": matched_by,
        "decode_status": decode_status,
    }
    copied.append(item)
    if args.dry_run:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = decode_result["data"] if decode_result else None
        if target.exists() and payload is None:
            item["status"] = "exists"
            return
        if target.exists() and payload is not None and target.read_bytes() == payload:
            item["status"] = "exists"
            return
        if payload is not None:
            target.write_bytes(payload)
            item["status"] = "decoded"
        else:
            shutil.copy2(path, target)
            item["status"] = "copied"
    except OSError as exc:
        item["status"] = "error"
        item["error"] = str(exc)
        errors.append(item)


def associated_media_paths(path: Path, match_tokens: list[str]) -> list[tuple[Path, str]]:
    parts = path.expanduser().resolve().parts
    if "xwechat_files" not in parts:
        return []
    index = parts.index("xwechat_files")
    if len(parts) <= index + 6:
        return []
    profile_root = Path(*parts[: index + 2])
    relative = parts[index + 2 :]
    # xwechat_files/<profile>/msg/attach/<chat-hash>/<YYYY-MM>/Img/<md5>.dat
    if len(relative) >= 6 and relative[0:2] == ("msg", "attach") and relative[4] == "Img":
        chat_hash = relative[2]
        month = relative[3]
        name = relative[5]
    # xwechat_files/<profile>/cache/<YYYY-MM>/Message/<chat-hash>/Bubble/<md5>_b.dat
    elif len(relative) >= 6 and relative[0] == "cache" and relative[2] == "Message" and relative[4] in {"Bubble", "Thumb", "ImageTemp"}:
        month = relative[1]
        chat_hash = relative[3]
        name = relative[5]
    else:
        return []
    stem = name[:-4] if name.lower().endswith(".dat") else name
    base_token = re.sub(r"_(?:t|h|b|w)$", "", stem, flags=re.IGNORECASE).lower()
    reason_token = next((token for token in match_tokens if token and token in base_token), base_token)
    cache_root = profile_root / "cache" / month / "Message" / chat_hash
    if not cache_root.is_dir():
        return []
    try:
        source_mtime = path.stat().st_mtime
    except OSError:
        source_mtime = 0.0
    results: list[tuple[Path, str]] = []
    for child in cache_root.rglob("*"):
        if not child.is_file():
            continue
        lower = child.name.lower()
        if child.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            try:
                child_mtime = child.stat().st_mtime
            except OSError:
                continue
            if source_mtime and abs(child_mtime - source_mtime) <= 30:
                results.append((child, f"associated:{reason_token}"))
        elif reason_token and reason_token in lower:
            results.append((child, f"associated:{reason_token}"))
    return results


def image_decode_keys(aes_arg: str, xor_arg: str) -> tuple[bytes | None, int]:
    aes_key = aes_arg or os.environ.get("WECHAT_IMAGE_AES_KEY", "")
    xor_value = xor_arg or os.environ.get("WECHAT_IMAGE_XOR_KEY", "")
    private_configs = [
        PRIVATE / "wechat_image_keys.local.json",
        PRIVATE / "external" / "wechat-decrypt" / "config.json",
    ]
    for private_config in private_configs:
        if not private_config.exists():
            continue
        try:
            payload = json.loads(private_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        aes_key = aes_key or str(payload.get("image_aes_key") or "")
        if not xor_value and "image_xor_key" in payload:
            xor_value = str(payload.get("image_xor_key"))
    if not xor_value:
        xor_value = latest_image_xor_key_from_logs()
    aes_bytes = aes_key.encode("ascii", errors="ignore")[:16] if aes_key else None
    try:
        xor_key = int(str(xor_value), 0) if xor_value else 0x88
    except ValueError:
        xor_key = 0x88
    return aes_bytes if aes_bytes and len(aes_bytes) == 16 else None, xor_key & 0xFF


def latest_image_xor_key_from_logs() -> str:
    log_dir = PRIVATE / "logs"
    if not log_dir.is_dir():
        return ""
    logs = sorted(log_dir.glob("find_image_key*.log"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    for log_path in logs[:5]:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches = re.findall(r"(?:XOR key|xor_key|image_xor_key)\s*[:=]\s*(0x[0-9A-Fa-f]{1,2}|\d{1,3})", text)
        if matches:
            return matches[-1]
    return ""


def decode_wechat_dat(path: Path, *, image_aes_key: bytes | None, image_xor_key: int) -> dict[str, object] | None:
    if path.suffix.lower() != ".dat":
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 6:
        return None
    if data.startswith(b"\x07\x08V1\x08\x07") or data.startswith(b"\x07\x08V2\x08\x07"):
        decoded = decode_wechat_v_container(data, image_aes_key=image_aes_key, image_xor_key=image_xor_key)
        if decoded:
            fmt, payload = decoded
            return {"status": "decoded-v-container", "format": fmt, "data": payload}
        return None
    decoded = decode_xor_dat(data)
    if decoded:
        fmt, payload = decoded
        return {"status": "decoded-xor", "format": fmt, "data": payload}
    return None


def decode_wechat_v_container(data: bytes, *, image_aes_key: bytes | None, image_xor_key: int) -> tuple[str, bytes] | None:
    if data.startswith(b"\x07\x08V1\x08\x07"):
        aes_key = b"cfcd208495d565ef"
    else:
        aes_key = image_aes_key
    if not aes_key or len(aes_key) < 16 or len(data) < 15:
        return None
    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Padding
    except ModuleNotFoundError:
        return None
    try:
        aes_size, xor_size = struct.unpack_from("<LL", data, 6)
        aligned = aes_size + (16 - (aes_size % 16))
        offset = 15
        if offset + aligned > len(data):
            return None
        cipher = AES.new(aes_key[:16], AES.MODE_ECB)
        dec_aes = Padding.unpad(cipher.decrypt(data[offset : offset + aligned]), AES.block_size)
        offset += aligned
        raw_end = len(data) - xor_size
        raw_data = data[offset:raw_end] if offset < raw_end else b""
        xor_data = data[raw_end:]
        payload = dec_aes + raw_data + bytes(byte ^ image_xor_key for byte in xor_data)
    except Exception:
        return None
    fmt = detect_media_extension_from_bytes(payload).lstrip(".")
    if fmt == "jpg" and not decoded_jpeg_is_readable(payload):
        return None
    if fmt == "png" and xor_size >= 2 and b"IEND" not in payload[-12:]:
        return None
    return (fmt, payload) if fmt else None


def decoded_jpeg_is_readable(payload: bytes) -> bool:
    try:
        from PIL import Image, ImageFile
    except Exception:
        return payload.startswith(b"\xff\xd8\xff") and len(payload) > 1024
    try:
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(BytesIO(payload)) as image:
            image.load()
            return image.format == "JPEG" and image.width > 0 and image.height > 0
    except Exception:
        return False
    finally:
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous
        except Exception:
            pass


def decode_xor_dat(data: bytes) -> tuple[str, bytes] | None:
    magics = {
        "jpg": b"\xff\xd8\xff",
        "png": b"\x89PNG",
        "gif": b"GIF8",
        "webp": b"RIFF",
        "bmp": b"BM",
        "tif": b"II*\x00",
    }
    for fmt, magic in magics.items():
        key = data[0] ^ magic[0]
        if all(index < len(data) and (data[index] ^ key) == magic[index] for index in range(len(magic))):
            payload = bytes(byte ^ key for byte in data)
            detected = detect_media_extension_from_bytes(payload).lstrip(".")
            return detected or fmt, payload
    return None


def media_match_reason(
    path: Path,
    mtime: float,
    cutoff_epoch: float,
    until_epoch: float | None,
    match_tokens: list[str],
    *,
    require_token_match: bool = False,
) -> str:
    normalized = str(path).lower()
    for token in match_tokens:
        if token and token in normalized:
            return f"token:{token[:32]}"
    if require_token_match:
        return ""
    if mtime >= cutoff_epoch and (until_epoch is None or mtime <= until_epoch):
        return "mtime"
    return ""


def target_relative_path(root: Path, path: Path) -> Path:
    rel = safe_relative(root, path)
    detected = detect_media_extension(path)
    if detected and rel.suffix.lower() not in {detected, ".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".mp4", ".zip"}:
        return rel.with_name(rel.name + detected)
    if detected and not rel.suffix:
        return rel.with_name(rel.name + detected)
    return rel


def decoded_relative_path(root: Path, path: Path, fmt: object) -> Path:
    rel = safe_relative(root, path)
    suffix = "." + str(fmt or "bin").lower().lstrip(".")
    stem = rel.name[:-4] if rel.name.lower().endswith(".dat") else rel.name
    return rel.with_name(stem + suffix)


def detect_media_extension(path: Path) -> str:
    try:
        head = path.read_bytes()[:32]
    except OSError:
        return ""
    return detect_media_extension_from_bytes(head)


def detect_media_extension_from_bytes(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return ".gif"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        return ".zip"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return ".mp4"
    if head.startswith(b"BM"):
        return ".bmp"
    if head.startswith(b"II*\x00"):
        return ".tif"
    return ""


def safe_relative(root: Path, path: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path(path.name)


def discover_sources() -> list[Path]:
    base = Path.home() / "Documents" / "xwechat_files"
    candidates: list[Path] = []
    if not base.exists():
        return candidates
    for profile in base.iterdir():
        if not profile.is_dir():
            continue
        for relative in ("msg/file", "msg/video", "msg/attach", "cache", "temp/ImageTemp", "temp/ImageUtils"):
            path = profile / relative
            if path.is_dir():
                candidates.append(path)
    return candidates


def source_bucket(source: Path) -> Path:
    parts = source.expanduser().resolve().parts
    if "xwechat_files" in parts:
        index = parts.index("xwechat_files")
        profile = parts[index + 1] if len(parts) > index + 1 else "profile"
        relative = Path(*parts[index + 2 :]) if len(parts) > index + 2 else Path(source.name)
        return Path(safe_component(profile)) / Path(*[safe_component(part) for part in relative.parts])
    return Path(safe_component(source.name))


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", value.strip())
    return cleaned.strip("-") or "wechat"


def unique_existing_dirs(paths: list[Path]) -> list[Path]:
    seen = set()
    result = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir() or resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
