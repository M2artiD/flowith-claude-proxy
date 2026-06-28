import unittest

from proxy import config


class ConfigTests(unittest.TestCase):
    def test_exports_hermes_trace_flag(self) -> None:
        self.assertIsInstance(config.FLOWITH_TRACE_HERMES, bool)


if __name__ == "__main__":
    unittest.main()
