import importlib
import os
import tempfile
import unittest
from pathlib import Path

from proxy import config


class ConfigTests(unittest.TestCase):
    def test_exports_hermes_trace_flag(self) -> None:
        self.assertIsInstance(config.FLOWITH_TRACE_HERMES, bool)

    def test_exports_request_log_flag(self) -> None:
        self.assertIsInstance(config.FLOWITH_REQUEST_LOG, bool)

    def test_exports_require_server_key_flag(self) -> None:
        self.assertIsInstance(config.FLOWITH_REQUIRE_SERVER_KEY, bool)

    def test_exports_local_only_flag(self) -> None:
        self.assertIsInstance(config.FLOWITH_LOCAL_ONLY, bool)

    def test_exports_max_request_bytes(self) -> None:
        self.assertIsInstance(config.FLOWITH_MAX_REQUEST_BYTES, int)
        self.assertGreater(config.FLOWITH_MAX_REQUEST_BYTES, 0)

    def test_load_api_key_reads_env_var(self) -> None:
        old_env = os.environ.get("FLOWITH_API_KEY")
        try:
            os.environ["FLOWITH_API_KEY"] = " env-key \n"

            self.assertEqual(config.load_api_key(), "env-key")
        finally:
            if old_env is None:
                os.environ.pop("FLOWITH_API_KEY", None)
            else:
                os.environ["FLOWITH_API_KEY"] = old_env

    def test_load_api_key_reads_dot_flowith_api_key_file(self) -> None:
        old_env = os.environ.pop("FLOWITH_API_KEY", None)
        old_root = config._PROJECT_ROOT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                temp_root = Path(tmp)
                config._PROJECT_ROOT = temp_root
                (temp_root / ".flowith_api_key").write_text(" file-key \n", encoding="utf-8")

                self.assertEqual(config.load_api_key(), "file-key")
        finally:
            config._PROJECT_ROOT = old_root
            if old_env is not None:
                os.environ["FLOWITH_API_KEY"] = old_env

    def test_load_api_key_ignores_example_placeholder_values(self) -> None:
        old_env = os.environ.get("FLOWITH_API_KEY")
        old_root = config._PROJECT_ROOT
        old_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                temp_root = Path(tmp)
                config._PROJECT_ROOT = temp_root
                os.environ["FLOWITH_API_KEY"] = "flo-your-key"
                (temp_root / ".env").write_text("FLOWITH_API_KEY=flo-your-key\n", encoding="utf-8")
                (temp_root / ".flowith_api_key").write_text("your Flowith API key\n", encoding="utf-8")
                os.chdir(temp_root)
                try:
                    api_key = config.load_api_key()
                finally:
                    os.chdir(old_cwd)

                self.assertIsNone(api_key)
        finally:
            config._PROJECT_ROOT = old_root
            if old_env is None:
                os.environ.pop("FLOWITH_API_KEY", None)
            else:
                os.environ["FLOWITH_API_KEY"] = old_env

    def test_load_api_key_skips_placeholder_env_and_uses_file_key(self) -> None:
        old_env = os.environ.get("FLOWITH_API_KEY")
        old_root = config._PROJECT_ROOT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                temp_root = Path(tmp)
                config._PROJECT_ROOT = temp_root
                os.environ["FLOWITH_API_KEY"] = "flo-your-key"
                (temp_root / ".flowith_api_key").write_text("real-file-key\n", encoding="utf-8")

                self.assertEqual(config.load_api_key(), "real-file-key")
        finally:
            config._PROJECT_ROOT = old_root
            if old_env is None:
                os.environ.pop("FLOWITH_API_KEY", None)
            else:
                os.environ["FLOWITH_API_KEY"] = old_env

    def test_env_path_uses_default_when_value_is_blank(self) -> None:
        old_env = os.environ.get("FLOWITH_TEST_BLANK_PATH")
        try:
            os.environ["FLOWITH_TEST_BLANK_PATH"] = "  "
            self.assertEqual(
                config._env_path("FLOWITH_TEST_BLANK_PATH", Path("fallback")),
                str(Path("fallback")),
            )
        finally:
            if old_env is None:
                os.environ.pop("FLOWITH_TEST_BLANK_PATH", None)
            else:
                os.environ["FLOWITH_TEST_BLANK_PATH"] = old_env

    def test_env_int_uses_default_when_value_is_blank(self) -> None:
        old_env = os.environ.get("FLOWITH_TEST_INT")
        try:
            os.environ["FLOWITH_TEST_INT"] = "  "
            self.assertEqual(config._env_int("FLOWITH_TEST_INT", 42), 42)
        finally:
            if old_env is None:
                os.environ.pop("FLOWITH_TEST_INT", None)
            else:
                os.environ["FLOWITH_TEST_INT"] = old_env

    def test_env_int_uses_default_when_value_is_invalid(self) -> None:
        old_env = os.environ.get("FLOWITH_TEST_INT")
        try:
            os.environ["FLOWITH_TEST_INT"] = "not-a-number"
            self.assertEqual(config._env_int("FLOWITH_TEST_INT", 42), 42)
        finally:
            if old_env is None:
                os.environ.pop("FLOWITH_TEST_INT", None)
            else:
                os.environ["FLOWITH_TEST_INT"] = old_env

    def test_pool_maxsize_env_uses_default_when_value_is_invalid_on_import(self) -> None:
        old_env = os.environ.get("FLOWITH_POOL_MAXSIZE")
        try:
            os.environ["FLOWITH_POOL_MAXSIZE"] = "not-a-number"

            reloaded = importlib.reload(config)

            self.assertEqual(
                reloaded.FLOWITH_POOL_MAXSIZE,
                max(16, reloaded.FLOWITH_MAX_CONCURRENCY * 2),
            )
        finally:
            if old_env is None:
                os.environ.pop("FLOWITH_POOL_MAXSIZE", None)
            else:
                os.environ["FLOWITH_POOL_MAXSIZE"] = old_env
            importlib.reload(config)

    def test_env_float_uses_default_when_value_is_blank(self) -> None:
        old_env = os.environ.get("FLOWITH_TEST_FLOAT")
        try:
            os.environ["FLOWITH_TEST_FLOAT"] = "  "
            self.assertEqual(config._env_float("FLOWITH_TEST_FLOAT", 3.5), 3.5)
        finally:
            if old_env is None:
                os.environ.pop("FLOWITH_TEST_FLOAT", None)
            else:
                os.environ["FLOWITH_TEST_FLOAT"] = old_env

    def test_env_float_uses_default_when_value_is_invalid(self) -> None:
        old_env = os.environ.get("FLOWITH_TEST_FLOAT")
        try:
            os.environ["FLOWITH_TEST_FLOAT"] = "not-a-number"
            self.assertEqual(config._env_float("FLOWITH_TEST_FLOAT", 3.5), 3.5)
        finally:
            if old_env is None:
                os.environ.pop("FLOWITH_TEST_FLOAT", None)
            else:
                os.environ["FLOWITH_TEST_FLOAT"] = old_env

if __name__ == "__main__":
    unittest.main()
