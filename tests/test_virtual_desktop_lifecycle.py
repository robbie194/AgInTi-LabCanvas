from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "agentic_tools" / "virtual_desktop" / "launch_virtual_desktop.sh"
WECHAT = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_virtual_desktop.sh"
PROFILE = ROOT / "agentic_tools" / "wechat_gui_agent" / "scripts" / "wechat_agent_profile.sh"
ANDROID = ROOT / "agentic_tools" / "android_device_agent" / "scripts" / "android_device_desktop.sh"


class VirtualDesktopLifecycleTests(unittest.TestCase):
    def test_scripts_are_valid_bash(self) -> None:
        for script in (LAUNCHER, WECHAT, PROFILE, ANDROID):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_phone_mirror_is_silent_and_frame_limited(self) -> None:
        source = ANDROID.read_text(encoding="utf-8").split("start_session() {", 1)[1]
        command = source.split("primary_loop_command=", 1)[0]
        self.assertIn("--no-audio", command)
        self.assertIn("--max-fps 15", command)
        self.assertIn("--render-driver software", command)
        self.assertNotIn("--start-app", command)

    def test_shared_launcher_requires_explicit_stale_recovery(self) -> None:
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('--recover-stale-display) RECOVER_STALE_DISPLAY="1"', source)
        self.assertIn('[[ "$RECOVER_STALE_DISPLAY" == "1" ]]', source)
        self.assertIn('stop_exact_processes "Xvfb display $DISPLAY_ID"', source)
        self.assertIn('stop_exact_processes "x11vnc relay for $DISPLAY_ID"', source)

    def test_all_status_probes_are_bounded(self) -> None:
        launcher = LAUNCHER.read_text(encoding="utf-8")
        wechat = WECHAT.read_text(encoding="utf-8")
        self.assertIn('timeout "${PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID"', launcher)
        self.assertIsNone(
            re.search(
                r'^\s*DISPLAY="\$DISPLAY_ID" XAUTHORITY= xwininfo -root -tree',
                launcher,
                re.MULTILINE,
            )
        )
        self.assertIn('timeout "${X11_PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID"', wechat)
        self.assertIsNone(
            re.search(
                r'^\s*DISPLAY="\$DISPLAY_ID" XAUTHORITY= xdotool (search|getwindowgeometry)',
                wechat,
                re.MULTILINE,
            )
        )

    def test_wechat_stops_only_its_client_before_opted_in_display_recovery(self) -> None:
        source = WECHAT.read_text(encoding="utf-8")
        stale_probe_index = source.index(
            'if ! timeout "${X11_PROBE_TIMEOUT_SECONDS}s" env DISPLAY="$DISPLAY_ID"'
        )
        stop_index = source.index("if ! stop_stale_wechat", stale_probe_index)
        launch_index = source.index('"$ROOT/agentic_tools/virtual_desktop/launch_virtual_desktop.sh"')
        self.assertLess(stale_probe_index, stop_index)
        self.assertLess(stop_index, launch_index)
        self.assertIn("--recover-stale-display", source)
        self.assertIn("wechat_pids_on_display", source)

    def test_wechat_does_not_leak_lifecycle_lock_to_display_children(self) -> None:
        source = WECHAT.read_text(encoding="utf-8")
        self.assertIn('-- /bin/true 9>&- >"$LAUNCH_LOG"', source)

    def test_profile_can_restart_only_the_isolated_desktop_client(self) -> None:
        source = PROFILE.read_text(encoding="utf-8")
        self.assertIn('WECHAT_BINARY=/usr/bin/wechat', source)
        self.assertIn('DESKTOP_ACTION="${2:-ensure}"', source)
        self.assertIn('ensure|restart-client)', source)
        self.assertIn('wechat_virtual_desktop.sh" "$DESKTOP_ACTION"', source)

    def test_wechat_preserves_small_visible_login_window(self) -> None:
        source = WECHAT.read_text(encoding="utf-8")
        self.assertIn("wechat_visible_window()", source)
        self.assertIn(
            '[[ -z "$main_window" && -z "$visible_window" && "$AUTO_RECOVER_UNMAPPED" == "1" ]]',
            source,
        )
        self.assertIn("WeChat entry/login window healthy", source)

    def test_wechat_launch_uses_isolated_environment_before_starting_client(self) -> None:
        source = WECHAT.read_text(encoding="utf-8")
        match = re.search(r"(?ms)^launch_wechat\(\) \{.*?^\}", source)
        self.assertIsNotNone(match)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_setsid = temp / "setsid"
            fake_setsid.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "    'home': os.environ.get('HOME'),\n"
                "    'xdg_config': os.environ.get('XDG_CONFIG_HOME'),\n"
                "    'xdg_data': os.environ.get('XDG_DATA_HOME'),\n"
                "    'xdg_cache': os.environ.get('XDG_CACHE_HOME'),\n"
                "    'display': os.environ.get('DISPLAY'),\n"
                "    'wayland_display': os.environ.get('WAYLAND_DISPLAY'),\n"
                "    'qt_plugin_path': os.environ.get('QT_PLUGIN_PATH'),\n"
                "    'qt_platform_plugin_path': os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH'),\n"
                "    'qt_fontdir': os.environ.get('QT_QPA_FONTDIR'),\n"
                "    'qt_style_override': os.environ.get('QT_STYLE_OVERRIDE'),\n"
                "    'qt_platform': os.environ.get('QT_QPA_PLATFORM'),\n"
                "    'argv': sys.argv[1:],\n"
                "}))\n",
                encoding="utf-8",
            )
            fake_setsid.chmod(0o755)
            fake_client = temp / "client" / "wechat"
            fake_client.parent.mkdir()
            fake_client.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_client.chmod(0o755)
            app_log = temp / "wechat_app.log"
            shell = "\n".join(
                [
                    match.group(0),
                    f"WECHAT_HOME={shlex.quote(str(temp / 'profile'))}",
                    f"WECHAT_AGENT_XDG_CONFIG_HOME={shlex.quote(str(temp / 'config'))}",
                    f"WECHAT_AGENT_XDG_DATA_HOME={shlex.quote(str(temp / 'data'))}",
                    f"WECHAT_AGENT_XDG_CACHE_HOME={shlex.quote(str(temp / 'cache'))}",
                    f"WECHAT_BINARY={shlex.quote(str(fake_client))}",
                    "DISPLAY_ID=:97",
                    f"APP_LOG={shlex.quote(str(app_log))}",
                    f"PATH={shlex.quote(str(temp))}:$PATH",
                    "export WAYLAND_DISPLAY=host-wayland",
                    "export QT_PLUGIN_PATH=host-qt-plugin",
                    "export QT_QPA_PLATFORM_PLUGIN_PATH=host-qt-platform",
                    "export QT_QPA_FONTDIR=host-qt-fonts",
                    "export QT_STYLE_OVERRIDE=host-qt-style",
                    "launch_wechat",
                ]
            )
            result = subprocess.run(
                ["bash", "-c", shell], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(app_log.read_text(encoding="utf-8"))

        self.assertEqual(payload["home"], str(temp / "profile"))
        self.assertEqual(payload["xdg_config"], str(temp / "config"))
        self.assertEqual(payload["xdg_data"], str(temp / "data"))
        self.assertEqual(payload["xdg_cache"], str(temp / "cache"))
        self.assertEqual(payload["display"], ":97")
        self.assertEqual(payload["qt_platform"], "xcb")
        for variable in (
            "wayland_display",
            "qt_plugin_path",
            "qt_platform_plugin_path",
            "qt_fontdir",
            "qt_style_override",
        ):
            self.assertIsNone(payload[variable], variable)
        self.assertEqual(payload["argv"], ["-f", str(fake_client)])
        self.assertIn("pgrep -f '(^|/)wechat([[:space:]]|$)'", source)


if __name__ == "__main__":
    unittest.main()
