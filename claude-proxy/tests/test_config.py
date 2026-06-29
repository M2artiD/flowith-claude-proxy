import unittest

from proxy import config


class ConfigTests(unittest.TestCase):
    def test_exports_hermes_trace_flag(self) -> None:
        self.assertIsInstance(config.FLOWITH_TRACE_HERMES, bool)

    def test_exports_require_server_key_flag(self) -> None:
        self.assertIsInstance(config.FLOWITH_REQUIRE_SERVER_KEY, bool)


if __name__ == "__main__":
    unittest.main()
