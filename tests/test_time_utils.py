import unittest

from xagent.core.time import format_in_timezone, format_utc, resolve_timezone


class TimeUtilsTests(unittest.TestCase):
    def test_format_in_timezone_uses_asia_shanghai(self):
        timestamp = 1779093994.0

        self.assertEqual(
            format_in_timezone(timestamp, resolve_timezone(request_timezone="Asia/Shanghai")),
            "2026-05-18 16:46:34 Asia/Shanghai (+08:00)",
        )

    def test_format_utc_is_explicit(self):
        self.assertEqual(
            format_utc(1779093994.0),
            "2026-05-18 08:46:34 UTC (+00:00)",
        )

    def test_invalid_request_timezone_falls_back_to_config_timezone(self):
        timezone = resolve_timezone(
            {"runtime": {"timezone": "Asia/Shanghai"}},
            request_timezone="Not/AZone",
        )

        self.assertEqual(timezone.key, "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
