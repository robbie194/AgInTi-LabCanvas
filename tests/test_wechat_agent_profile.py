from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_agent_profile.sh"


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


if __name__ == "__main__":
    unittest.main()
