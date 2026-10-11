from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_media_sync.py"


def make_test_jpeg(*, truncate_eoi: bool = False) -> bytes:
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - exercised only when Pillow is unavailable.
        raise unittest.SkipTest("Pillow is not installed") from exc
    buffer = BytesIO()
    Image.new("RGB", (32, 24), (245, 245, 245)).save(buffer, format="JPEG")
    payload = buffer.getvalue()
    if truncate_eoi and payload.endswith(b"\xff\xd9"):
        return payload[:-2]
    return payload


def make_v2_container(payload: bytes, *, key: bytes, xor_key: int) -> bytes:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Padding
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised only when pycryptodome is unavailable.
        raise unittest.SkipTest("pycryptodome is not installed") from exc

    aes_payload = payload[: min(32, len(payload))]
    raw_payload = payload[len(aes_payload) : len(aes_payload) + 13]
    xor_payload = payload[len(aes_payload) + len(raw_payload) :]
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(Padding.pad(aes_payload, AES.block_size))
    return (
        b"\x07\x08V2\x08\x07"
        + struct.pack("<LL", len(aes_payload), len(xor_payload))
        + b"\x00"
        + encrypted
        + raw_payload
        + bytes(byte ^ xor_key for byte in xor_payload)
    )


class WeChatMediaSyncTests(unittest.TestCase):
    def test_default_media_paths_use_isolated_agent_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source-cache"
            source.mkdir()
            image = source / "agent-photo.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nagent-photo")
            private = base / "agent-private"
            environment = {**os.environ, "WECHAT_AGENT_PRIVATE_DIR": str(private)}
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "Agent group",
                    "--source",
                    str(source),
                    "--since-minutes",
                    "999",
                    "--summary-only",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "copied")
            self.assertTrue((private / "downloads").is_dir())
            self.assertTrue(list((private / "downloads").rglob("agent-photo.png")))

    def test_decode_keys_read_stable_private_media_config(self) -> None:
        sys.path.insert(0, str(SCRIPT.parent))
        import wechat_media_sync

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "agentic_tools" / "wechat_gui_agent" / ".private" / "wechat_image_keys.local.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"image_aes_key": "1234567890abcdef", "image_xor_key": "0x44"}),
                encoding="utf-8",
            )
            original_private = wechat_media_sync.PRIVATE
            try:
                wechat_media_sync.PRIVATE = base / "agentic_tools" / "wechat_gui_agent" / ".private"
                with mock.patch.dict("os.environ", {"WECHAT_IMAGE_AES_KEY": "", "WECHAT_IMAGE_XOR_KEY": ""}, clear=False):
                    key, xor_key = wechat_media_sync.image_decode_keys("", "")
            finally:
                wechat_media_sync.PRIVATE = original_private

        self.assertEqual(key, b"1234567890abcdef")
        self.assertEqual(xor_key, 0x44)

    def test_sync_detects_extension_and_records_media_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source-cache"
            source.mkdir()
            image = source / "quoted_image_blob"
            image.write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg-demo")
            dest = base / "downloads"
            db = base / "mirror.sqlite"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "懒人科研",
                    "--source",
                    str(source),
                    "--dest",
                    str(dest),
                    "--db",
                    str(db),
                    "--since-minutes",
                    "999",
                    "--summary-only",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "copied")
            self.assertEqual(payload["file_count"], 1)
            self.assertEqual(payload["recorded_files"], 1)
            copied = list(dest.rglob("quoted_image_blob.jpg"))
            self.assertEqual(len(copied), 1)

            with sqlite3.connect(db) as conn:
                row = conn.execute(
                    """
                    SELECT chats.name, media_files.mirror_path, media_files.suffix,
                           media_files.status, media_files.matched_by
                    FROM media_files
                    JOIN chats ON chats.id = media_files.chat_id
                    """
                ).fetchone()

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0], "懒人科研")
            self.assertTrue(row[1].endswith("quoted_image_blob.jpg"))
            self.assertEqual(row[2], ".jpg")
            self.assertEqual(row[3], "copied")
            self.assertEqual(row[4], "mtime")

    def test_exact_chat_sync_rejects_fresh_mtime_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source-cache"
            source.mkdir()
            (source / "outgoing-report.pdf").write_bytes(b"%PDF-1.4\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "鏈接",
                    "--source",
                    str(source),
                    "--dest",
                    str(base / "downloads"),
                    "--db",
                    str(base / "mirror.sqlite"),
                    "--since-minutes",
                    "999",
                    "--require-token-match",
                    "--summary-only",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "no-changes")
        self.assertEqual(payload["file_count"], 0)

    def test_exact_chat_sync_accepts_matching_source_token(self) -> None:
        token = "abc123abc123abc123abc123abc123ab"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source-cache"
            source.mkdir()
            (source / f"{token}.pdf").write_bytes(b"%PDF-1.4\n")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "鏈接",
                    "--source",
                    str(source),
                    "--dest",
                    str(base / "downloads"),
                    "--db",
                    str(base / "mirror.sqlite"),
                    "--since-minutes",
                    "0",
                    "--match-token",
                    token,
                    "--require-token-match",
                    "--summary-only",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["file_count"], 1)

    def test_sync_decodes_xor_dat_image_and_records_decoded_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source-cache"
            source.mkdir()
            jpg = b"\xff\xd8\xff\xe0" + b"jpeg-demo"
            encoded = bytes(byte ^ 0x88 for byte in jpg)
            dat = source / "abc123quotedimage.dat"
            dat.write_bytes(encoded)
            dest = base / "downloads"
            db = base / "mirror.sqlite"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "lachlanchan",
                    "--source",
                    str(source),
                    "--dest",
                    str(dest),
                    "--db",
                    str(db),
                    "--since-minutes",
                    "0",
                    "--match-token",
                    "abc123quotedimage",
                    "--summary-only",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "copied")
            self.assertEqual(payload["file_count"], 1)
            decoded = list(dest.rglob("abc123quotedimage.jpg"))
            self.assertEqual(len(decoded), 1)
            self.assertEqual(decoded[0].read_bytes(), jpg)

            with sqlite3.connect(db) as conn:
                row = conn.execute(
                    """
                    SELECT chats.name, media_files.mirror_path, media_files.suffix,
                           media_files.status, media_files.matched_by,
                           media_files.metadata_json
                    FROM media_files
                    JOIN chats ON chats.id = media_files.chat_id
                    """
                ).fetchone()

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0], "lachlanchan")
            self.assertTrue(row[1].endswith("abc123quotedimage.jpg"))
            self.assertEqual(row[2], ".jpg")
            self.assertEqual(row[3], "decoded")
            self.assertEqual(row[4], "token:abc123quotedimage")
            metadata = json.loads(row[5])
            self.assertEqual(metadata["decode_status"], "decoded-xor")

    def test_decodes_v2_dat_container_with_image_key(self) -> None:
        sys.path.insert(0, str(SCRIPT.parent))
        import wechat_media_sync

        key = b"1234567890abcdef"
        xor_key = 0x44
        payload = make_test_jpeg(truncate_eoi=True)
        container = make_v2_container(payload, key=key, xor_key=xor_key)
        with tempfile.TemporaryDirectory() as tmp:
            dat = Path(tmp) / "quoted_v2.dat"
            dat.write_bytes(container)
            decoded = wechat_media_sync.decode_wechat_dat(dat, image_aes_key=key, image_xor_key=xor_key)

        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded["status"], "decoded-v-container")
        self.assertEqual(decoded["format"], "jpg")
        self.assertEqual(decoded["data"], payload)

    def test_sync_overwrites_stale_decoded_file_with_same_length(self) -> None:
        key = b"1234567890abcdef"
        xor_key = 0x44
        payload = make_test_jpeg(truncate_eoi=True)
        container = make_v2_container(payload, key=key, xor_key=xor_key)
        token = "cafecafecafecafecafecafecafecafe"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source-cache"
            source.mkdir()
            dat = source / f"{token}.dat"
            dat.write_bytes(container)
            dest = base / "downloads"
            stale = dest / "testchat" / "source-cache" / f"{token}.jpg"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"x" * len(payload))
            db = base / "mirror.sqlite"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "testchat",
                    "--source",
                    str(source),
                    "--dest",
                    str(dest),
                    "--db",
                    str(db),
                    "--since-minutes",
                    "0",
                    "--match-token",
                    token,
                    "--image-aes-key",
                    key.decode("ascii"),
                    "--image-xor-key",
                    hex(xor_key),
                    "--summary-only",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(stale.read_bytes(), payload)

    def test_sync_adds_associated_thumb_for_attach_token_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = base / "xwechat_files" / "wxid_demo"
            attach = profile / "msg" / "attach" / "chat_hash" / "2026-06" / "Img"
            thumb = profile / "cache" / "2026-06" / "Message" / "chat_hash" / "Thumb"
            attach.mkdir(parents=True)
            thumb.mkdir(parents=True)
            token = "feedfacecafebeef0011223344556677"
            (attach / f"{token}.dat").write_bytes(b"\x07\x08V2\x08\x07not-decodable")
            readable = thumb / "7_1781783136_thumb.jpg"
            readable.write_bytes(b"\xff\xd8\xff\xe0" + b"thumb")
            dest = base / "downloads"
            db = base / "mirror.sqlite"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "lachlanchan",
                    "--source",
                    str(profile / "msg" / "attach"),
                    "--dest",
                    str(dest),
                    "--db",
                    str(db),
                    "--since-minutes",
                    "0",
                    "--match-token",
                    token,
                    "--summary-only",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["file_count"], 2)
            self.assertEqual(len(list(dest.rglob(f"{token}.dat"))), 1)
            self.assertEqual(len(list(dest.rglob("7_1781783136_thumb.jpg"))), 1)

            with sqlite3.connect(db) as conn:
                matched = conn.execute(
                    "SELECT matched_by FROM media_files WHERE mirror_path LIKE ?",
                    ("%7_1781783136_thumb.jpg",),
                ).fetchone()

            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual(matched[0], f"associated:{token}")

    def test_sync_adds_associated_thumb_for_cache_bubble_token_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = base / "xwechat_files" / "wxid_demo"
            bubble = profile / "cache" / "2026-06" / "Message" / "chat_hash" / "Bubble"
            thumb = profile / "cache" / "2026-06" / "Message" / "chat_hash" / "Thumb"
            bubble.mkdir(parents=True)
            thumb.mkdir(parents=True)
            token = "cafed00d1234567890abcdef12345678"
            (bubble / f"{token}_b.dat").write_bytes(b"\x07\x08V2\x08\x07not-decodable")
            readable = thumb / "8_1781788117_thumb.jpg"
            readable.write_bytes(b"\xff\xd8\xff\xe0" + b"thumb")
            dest = base / "downloads"
            db = base / "mirror.sqlite"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--chat",
                    "lachlanchan",
                    "--source",
                    str(profile / "cache"),
                    "--dest",
                    str(dest),
                    "--db",
                    str(db),
                    "--since-minutes",
                    "0",
                    "--match-token",
                    token,
                    "--summary-only",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["file_count"], 2)
            self.assertEqual(len(list(dest.rglob(f"{token}_b.dat"))), 1)
            self.assertEqual(len(list(dest.rglob("8_1781788117_thumb.jpg"))), 1)

            with sqlite3.connect(db) as conn:
                matched = conn.execute(
                    "SELECT matched_by FROM media_files WHERE mirror_path LIKE ?",
                    ("%8_1781788117_thumb.jpg",),
                ).fetchone()

            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual(matched[0], f"associated:{token}")


if __name__ == "__main__":
    unittest.main()
