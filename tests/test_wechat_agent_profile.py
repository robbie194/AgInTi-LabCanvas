from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_agent_profile.sh"
TUNNEL = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_cloud_tunnel.sh"
GROUP_CONFIG = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_agent_group_config.py"


class WeChatAgentProfileTests(unittest.TestCase):
    def test_init_creates_private_profile_without_touching_normal_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-wechat-profile-") as directory:
            normal_home = Path(directory) / "normal-home"
            home = normal_home / ".local" / "share" / "overtree-wechat"
            private = home / "private"
            env = os.environ.copy()
            env.update({
                "HOME": str(normal_home),
                "WECHAT_AGENT_HOME": str(home),
                "WECHAT_AGENT_PRIVATE_DIR": str(private),
            })
            result = subprocess.run([str(PROFILE), "init"], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            env_file = private / "wechat_supervisor.local.env"
            self.assertTrue(env_file.is_file())
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            content = env_file.read_text(encoding="utf-8")
            self.assertIn(f"WECHAT_AGENT_HOME={home}", content)
            self.assertIn(f"WECHAT_AGENT_PRIVATE_DIR={private}", content)
            self.assertIn("WECHAT_DISPLAY=:97", content)
            self.assertIn("WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL=https://cloud.overtree.top", content)
            self.assertIn("WECHAT_PERSONAL_GROUP_ALLOWLIST=", content)
            self.assertNotIn("WECHAT_AGENT_ALLOW_NORMAL_HOME", content)

    def test_profile_refuses_normal_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-wechat-profile-") as directory:
            env = os.environ.copy()
            env["HOME"] = directory
            env["WECHAT_AGENT_HOME"] = directory
            result = subprocess.run([str(PROFILE), "init"], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("normal user HOME", result.stderr)

    def test_profile_refuses_to_start_under_the_life_wechat_linux_user(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-wechat-profile-") as directory:
            env = os.environ.copy()
            env.update({"HOME": directory, "WECHAT_AGENT_HOME": str(Path(directory) / "agent-home")})
            result = subprocess.run([str(PROFILE), "desktop"], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must run as Linux user overtree-wechat", result.stderr)

    def test_cloud_tunnel_script_is_shell_valid_and_loopback_only_by_default(self) -> None:
        result = subprocess.run(["bash", "-n", str(TUNNEL)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = subprocess.run([str(TUNNEL), "status"], env={**os.environ, "WECHAT_CLOUD_TUNNEL_STATE_DIR": str(Path(tempfile.mkdtemp()))}, capture_output=True, text=True, check=False)
        self.assertNotEqual(status.returncode, 0)
        self.assertIn("127.0.0.1:33220", status.stdout)

    def test_group_config_requires_cloud_secret_without_printing_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-wechat-profile-") as directory:
            private = Path(directory) / "private"
            env_file = private / "wechat_supervisor.local.env"
            env_file.parent.mkdir()
            env_file.write_text("WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID=personal-agent-main\nWECHAT_PERSONAL_GROUP_CLOUD_URL=http://127.0.0.1:33220\nWECHAT_PERSONAL_GROUP_SHARED_SECRET=short\n", encoding="utf-8")
            result = subprocess.run([str(GROUP_CONFIG), "--group-name", "Test", "--group-id", "g@chatroom", "--self-wxid", "wxid", "--message-table", "Msg_test", "--env-file", str(env_file)], env={**os.environ, "WECHAT_AGENT_PRIVATE_DIR": str(private)}, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Configure", result.stderr)
            self.assertNotIn("short", result.stderr)


if __name__ == "__main__":
    unittest.main()
