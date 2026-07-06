
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from proxy import server


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_server_api_key = server._SERVER_API_KEY
        self.original_default_client = server._default_client
        self.original_debug_dump = server.DEBUG_DUMP
        self.original_debug_dump_dir = server.DEBUG_DUMP_DIR
        self.original_debug_dump_max_bytes = server.DEBUG_DUMP_MAX_BYTES
        self.original_debug_dump_max_files = server.DEBUG_DUMP_MAX_FILES
        self.original_require_server_key = server.FLOWITH_REQUIRE_SERVER_KEY
        self.original_local_only = server.FLOWITH_LOCAL_ONLY
        self.original_custom_model_aliases = dict(server.CUSTOM_MODEL_ALIASES)
        server._SERVER_API_KEY = "flo-secret-dashboard-key"
        server._default_client = None
        server.FLOWITH_REQUIRE_SERVER_KEY = False
        server.FLOWITH_LOCAL_ONLY = True
        server.CUSTOM_MODEL_ALIASES.clear()
        server.CUSTOM_MODEL_ALIASES.update({"fast": "claude-fable-5"})
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server._SERVER_API_KEY = self.original_server_api_key
        server._default_client = self.original_default_client
        server.DEBUG_DUMP = self.original_debug_dump
        server.DEBUG_DUMP_DIR = self.original_debug_dump_dir
        server.DEBUG_DUMP_MAX_BYTES = self.original_debug_dump_max_bytes
        server.DEBUG_DUMP_MAX_FILES = self.original_debug_dump_max_files
        server.FLOWITH_REQUIRE_SERVER_KEY = self.original_require_server_key
        server.FLOWITH_LOCAL_ONLY = self.original_local_only
        server.CUSTOM_MODEL_ALIASES.clear()
        server.CUSTOM_MODEL_ALIASES.update(self.original_custom_model_aliases)

    def test_dashboard_page_is_served_as_local_console(self) -> None:
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Flowith Claude Proxy Console", response.text)
        self.assertIn("/dashboard/api/status", response.text)

    def test_dashboard_status_reports_routes_and_safe_runtime_state(self) -> None:
        response = self.client.get("/dashboard/api/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["service"], "flowith-claude-proxy")
        self.assertTrue(payload["health"]["ok"])
        self.assertTrue(payload["security"]["local_only"])
        self.assertTrue(payload["security"]["server_key_configured"])
        self.assertNotIn("flo-secret-dashboard-key", json.dumps(payload))
        self.assertIn("POST /v1/messages", payload["routes"])
        self.assertIn("POST /v1/chat/completions", payload["routes"])
        self.assertIn("POST /v1/responses", payload["routes"])
        self.assertEqual(payload["models"]["default"], server.DEFAULT_MODEL)
        self.assertIn("claude-fable-5", payload["models"]["available"])

    def test_dashboard_config_masks_secrets_and_exposes_safety_knobs(self) -> None:
        response = self.client.get("/dashboard/api/config")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        text = json.dumps(payload)
        self.assertNotIn("flo-secret-dashboard-key", text)
        self.assertEqual(payload["FLOWITH_API_KEY"], "flo-****-key")
        self.assertEqual(payload["FLOWITH_LOCAL_ONLY"], True)
        self.assertIn("FLOWITH_MAX_REQUEST_BYTES", payload)
        self.assertIn("FLOWITH_SEMAPHORE_TIMEOUT", payload)
        self.assertIn("DEBUG_DUMP_DIR", payload)

    def test_dashboard_routes_are_grouped_by_compatibility_surface(self) -> None:
        response = self.client.get("/dashboard/api/routes")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("anthropic", payload)
        self.assertIn("openai", payload)
        self.assertIn("diagnostics", payload)
        self.assertIn("POST /v1/messages", payload["anthropic"])
        self.assertIn("GET /dashboard", payload["diagnostics"])

    def test_dashboard_debug_dump_listing_is_metadata_only_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dump_dir = Path(td)
            server.DEBUG_DUMP = True
            server.DEBUG_DUMP_DIR = str(dump_dir)
            for idx in range(3):
                (dump_dir / f"flowith_nonstream_20260706_00000{idx}.json").write_text(
                    json.dumps({"request": {"payload": {"authorization": "Bearer secret"}}}),
                    encoding="utf-8",
                )

            response = self.client.get("/dashboard/api/debug-dumps")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["count"], 3)
        self.assertLessEqual(len(payload["files"]), 50)
        self.assertEqual(payload["files"][0]["name"], "flowith_nonstream_20260706_000002.json")
        self.assertIn("size_bytes", payload["files"][0])
        self.assertNotIn("secret", json.dumps(payload))

    def test_dashboard_debug_dump_listing_survives_racing_file_removal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dump_dir = Path(td)
            server.DEBUG_DUMP = True
            server.DEBUG_DUMP_DIR = str(dump_dir)
            stable = dump_dir / "flowith_nonstream_20260706_000001.json"
            disappearing = dump_dir / "flowith_stream_20260706_000002.json"
            stable.write_text("{}", encoding="utf-8")
            disappearing.write_text("{}", encoding="utf-8")

            original_stat = Path.stat

            def stat_with_race(path: Path, *args, **kwargs):
                if path == disappearing:
                    raise OSError("debug dump was pruned concurrently")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", stat_with_race):
                response = self.client.get("/dashboard/api/debug-dumps")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual([item["name"] for item in payload["files"]], [stable.name])
    def test_dashboard_requires_key_when_network_accessible(self) -> None:
        server.FLOWITH_LOCAL_ONLY = False
        server.FLOWITH_REQUIRE_SERVER_KEY = True

        anonymous = self.client.get("/dashboard/api/status")
        authorised = self.client.get(
            "/dashboard/api/status",
            headers={"Authorization": "Bearer flo-secret-dashboard-key"},
        )

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(authorised.status_code, 200)
