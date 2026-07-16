from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class LauncherBatchTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_launchers_open_dashboard_only_after_proxy_is_serving_it(self) -> None:
        for name in ("start.bat", "start-codex.bat", "start-hermes.bat"):
            with self.subTest(name=name):
                text = self._read(name)
                self.assertIn("call :open_dashboard %FLOWITH_API_PORT%", text)
                self.assertIn("/dashboard", text)
                self.assertIn("Invoke-WebRequest", text)
                self.assertRegex(text, r"for\(\$i=0; \$i -lt 40; \$i\+\+\)")
                self.assertIn("Start-Process (''http://127.0.0.1:{0}/dashboard'' -f $port)", text)

    def test_launchers_reuse_only_healthy_forwarding_proxy_on_same_port(self) -> None:
        for name in ("start.bat", "start-codex.bat", "start-hermes.bat"):
            with self.subTest(name=name):
                text = self._read(name)
                self.assertIn('set "PORT_ALREADY_RUNNING=0"', text)
                self.assertIn("call :check_port %FLOWITH_API_PORT%", text)
                self.assertIn('if "%PORT_ALREADY_RUNNING%"=="1"', text)
                self.assertIn("/health", text)
                self.assertIn("/dashboard", text)
                self.assertIn("Proxy already running", text)
                self.assertIn("non-dashboard or unhealthy process", text)
                self.assertIn('set "PORT_ALREADY_RUNNING=1"', text)

    def test_launchers_advertise_forwarding_urls_for_their_client_surface(self) -> None:
        expected = {
            "start.bat": ("claude", "8787", "Endpoint: /v1/messages"),
            "start-codex.bat": ("codex", "8788", "Endpoints: /v1/responses, /v1/chat/completions"),
            "start-hermes.bat": ("codex", "8789", "OpenAI:    http://127.0.0.1:%FLOWITH_API_PORT%/v1"),
        }
        for name, (profile, port, advertised_endpoint) in expected.items():
            with self.subTest(name=name):
                text = self._read(name)
                self.assertIn(f'set "FLOWITH_API_PROFILE={profile}"', text)
                self.assertIn(f'set "FLOWITH_API_PORT={port}"', text)
                self.assertIn(advertised_endpoint, text)
                self.assertIn("Dashboard: http://127.0.0.1:%FLOWITH_API_PORT%/dashboard", text)

    def test_open_dashboard_can_be_disabled_for_headless_forwarding(self) -> None:
        for name in ("start.bat", "start-codex.bat", "start-hermes.bat"):
            with self.subTest(name=name):
                text = self._read(name)
                self.assertIn("FLOWITH_OPEN_DASHBOARD=", text)
                self.assertIn('if /i "%FLOWITH_OPEN_DASHBOARD%"=="false" exit /b 0', text)
                self.assertIn('if /i "%FLOWITH_OPEN_DASHBOARD%"=="0" exit /b 0', text)

    def test_hermes_smoke_defaults_to_configured_key_and_working_model(self) -> None:
        text = (ROOT / "scripts" / "smoke_hermes.ps1").read_text(encoding="utf-8")
        self.assertNotIn('"test-key"', text)
        self.assertNotIn('"hermes"', text)
        self.assertIn('"claude-5-sonnet"', text)
        self.assertIn("Get-ConfiguredFlowithApiKey", text)
        self.assertIn(".flowith_api_key", text)
        self.assertIn(".env", text)
        self.assertIn('$ProgressPreference = "SilentlyContinue"', text)
        self.assertIn("-UseBasicParsing", text)

    def test_hermes_launcher_enables_single_answer_mode(self) -> None:
        text = self._read("start-hermes.bat")
        self.assertIn(
            'set "FLOWITH_HERMES_SINGLE_ANSWER=true"',
            text,
        )


if __name__ == "__main__":
    unittest.main()
