import unittest
import sys
import types

sys.modules.setdefault("gdown", types.ModuleType("gdown"))
from workers.agent.live_runner import _rtmp_target


class LiveRtmpsTargetTests(unittest.TestCase):
    def test_defaults_to_youtube_rtmps_primary(self) -> None:
        self.assertEqual(
            _rtmp_target({"stream_key": "abc-def"}),
            "rtmps://a.rtmps.youtube.com/live2/abc-def",
        )

    def test_rewrites_legacy_youtube_primary_rtmp_to_rtmps(self) -> None:
        self.assertEqual(
            _rtmp_target(
                {
                    "rtmp_url": "rtmp://x.rtmp.youtube.com/live2",
                    "stream_key": "abc-def",
                }
            ),
            "rtmps://a.rtmps.youtube.com/live2/abc-def",
        )

    def test_rewrites_legacy_youtube_backup_rtmp_to_rtmps_and_keeps_query_after_key(self) -> None:
        self.assertEqual(
            _rtmp_target(
                {
                    "rtmp_url": "rtmp://y.rtmp.youtube.com/live2?backup=1",
                    "stream_key": "abc-def",
                }
            ),
            "rtmps://b.rtmps.youtube.com/live2/abc-def?backup=1",
        )


if __name__ == "__main__":
    unittest.main()
