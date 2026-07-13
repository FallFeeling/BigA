import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from mrmodel_db import AUTHOR_SEC_UID, MonitorDB
from mrmodel_monitor import (
    MonitorFailure,
    MrModelMonitor,
    classify_response,
    clean_homepage_description,
    is_confirmed_empty_reply_thread,
    is_global_api_failure,
    is_global_monitor_failure,
    is_newer_publication,
    interaction_candidate_from_parent,
    monitor_interval_seconds,
)


def comment(comment_id, nickname="用户", sec_uid="user", text="内容"):
    return {
        "comment_id": comment_id,
        "text": text,
        "create_time": 1,
        "create_time_display": "2026-07-11 12:00:00",
        "digg_count": 0,
        "reply_total": 0,
        "ip_label": "",
        "user": {
            "nickname": nickname,
            "uid": sec_uid,
            "sec_uid": sec_uid,
            "avatar": "",
        },
    }


class ScheduleTests(unittest.TestCase):
    def test_monitor_intervals_follow_publish_age(self):
        now = datetime.now()
        self.assertEqual(monitor_interval_seconds((now - timedelta(hours=1)).timestamp(), now), 180)
        self.assertEqual(monitor_interval_seconds((now - timedelta(hours=4)).timestamp(), now), 1200)
        self.assertEqual(monitor_interval_seconds((now - timedelta(hours=30)).timestamp(), now), 3600)
        self.assertIsNone(monitor_interval_seconds((now - timedelta(hours=49)).timestamp(), now))

    def test_health_classification(self):
        self.assertEqual(classify_response("https://www.douyin.com/verify", "安全验证", 200), "verification")
        self.assertEqual(classify_response("https://passport.douyin.com/login", "扫码登录", 200), "auth")
        self.assertEqual(classify_response("https://www.douyin.com", "", 503), "network")
        self.assertEqual(classify_response("https://www.douyin.com", "normal", 200), "ok")

    def test_new_video_requires_a_later_publish_time(self):
        latest = 1_783_742_400
        self.assertTrue(is_newer_publication(latest + 1, latest))
        self.assertFalse(is_newer_publication(latest, latest))
        self.assertFalse(is_newer_publication(latest - 1, latest))
        self.assertFalse(is_newer_publication(0, latest))
        self.assertFalse(is_newer_publication("", latest))

    def test_only_verification_and_auth_are_global_api_failures(self):
        self.assertTrue(is_global_api_failure(MonitorFailure("verification", "captcha")))
        self.assertTrue(is_global_api_failure(MonitorFailure("auth", "login")))
        self.assertFalse(is_global_api_failure(MonitorFailure("data", "empty")))
        self.assertFalse(is_global_api_failure(MonitorFailure("network", "timeout")))
        self.assertFalse(is_global_api_failure(MonitorFailure("missing", "gone")))

    def test_monitor_level_network_is_global_but_data_is_local(self):
        self.assertTrue(is_global_monitor_failure(MonitorFailure("verification", "captcha")))
        self.assertTrue(is_global_monitor_failure(MonitorFailure("auth", "login")))
        self.assertTrue(is_global_monitor_failure(MonitorFailure("network", "offline")))
        self.assertFalse(is_global_monitor_failure(MonitorFailure("data", "bad payload")))
        self.assertFalse(is_global_monitor_failure(MonitorFailure("session", "one video")))

    def test_only_a_previously_nonempty_thread_can_be_confirmed_empty(self):
        self.assertTrue(is_confirmed_empty_reply_thread(7, 0, 0))
        self.assertFalse(is_confirmed_empty_reply_thread(0, 0, 0))
        self.assertFalse(is_confirmed_empty_reply_thread(7, 7, 0))
        self.assertFalse(is_confirmed_empty_reply_thread(7, 0, 1))

    def test_interaction_candidate_uses_embedded_author_signals(self):
        author_reply = comment("r100", "模型先生", AUTHOR_SEC_UID, "前100条里的作者回复")
        parent = {
            **comment("p100"),
            "is_author_digged": 1,
            "reply_comment": [author_reply],
        }
        candidate = interaction_candidate_from_parent(parent)
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate["author_liked"])
        self.assertEqual(candidate["parent"]["comment_id"], "p100")
        self.assertEqual(candidate["author_replies"][0]["comment_id"], "r100")
        self.assertTrue(candidate["author_replies"][0]["is_author"])

        self.assertIsNone(interaction_candidate_from_parent(comment("ordinary")))

    def test_numeric_homepage_text_is_not_used_as_a_description(self):
        self.assertEqual(clean_homepage_description("99"), "")
        self.assertEqual(clean_homepage_description("1.2万"), "")
        self.assertEqual(clean_homepage_description("正常视频文案"), "正常视频文案")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = MonitorDB(Path(self.temp.name) / "monitor.db")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_bootstrap_and_immutable_events(self):
        parent = comment("p1")
        author_reply = comment("r1", "模型先生", AUTHOR_SEC_UID, "作者回复")
        payload = {
            "generated_at": "2026-07-11T12:00:00",
            "videos": [
                {
                    "video_id": "v1",
                    "url": "https://www.douyin.com/video/v1",
                    "cover": "https://example.com/cover.jpg",
                    "description": "测试视频",
                    "published_at": "2026-07-11 12:00:00",
                    "published_timestamp": 1783742400,
                    "like_count": 10,
                    "comment_count": 2,
                    "duration_ms": 1000,
                    "comments_status": "complete",
                    "comments": [{**parent, "author_liked": True, "replies": [{**author_reply, "is_author": True}]}],
                    "interaction_threads": [
                        {
                            "parent": parent,
                            "author_liked": True,
                            "author_replies": [author_reply],
                        }
                    ],
                }
            ],
        }
        self.assertEqual(self.db.bootstrap_dashboard(payload), 1)
        self.assertEqual(self.db.bootstrap_dashboard(payload), 0)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM important_events").fetchone()[0], 2)

        thread = {
            "parent": parent,
            "author_liked": True,
            "author_commented": False,
            "author_replies": [author_reply],
            "replies": [{**author_reply, "is_author": True}],
        }
        new_events, _ = self.db.upsert_interaction("v1", thread)
        self.assertEqual(new_events, 0)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM important_events").fetchone()[0], 2)

    def test_new_video_lifecycle_and_deleted_export(self):
        profile = {
            "video_id": "v2",
            "href": "https://www.douyin.com/video/v2",
            "cover": "https://example.com/v2.jpg",
            "title_hint": "新视频",
        }
        published = datetime.now() - timedelta(minutes=10)
        details = {
            "description": "新视频",
            "description_source": "caption",
            "published_at": published.strftime("%Y-%m-%d %H:%M:%S"),
            "published_timestamp": int(published.timestamp()),
            "like_count": 12,
            "comment_count": 3,
            "duration_ms": 15000,
        }
        row = self.db.register_new_video(profile, details, "https://example.com/video.mp4")
        self.assertEqual(row["monitor_status"], "active")
        self.assertEqual(len(self.db.due_videos()), 1)

        author_parent = comment("author-parent", "模型先生", AUTHOR_SEC_UID, "作者一级评论")
        new_events, _ = self.db.upsert_interaction(
            "v2",
            {
                "parent": author_parent,
                "author_liked": False,
                "author_commented": True,
                "author_replies": [],
                "replies": [],
            },
        )
        self.assertEqual(new_events, 1)
        self.db.freeze_video("v2", "deleted", "test")
        exported = self.db.build_dashboard_payload()
        video = exported["videos"][0]
        self.assertTrue(video["is_deleted"])
        self.assertTrue(video["has_author_interaction"])
        self.assertEqual(video["interaction_threads"][0]["author_commented"], True)

    def test_latest_published_timestamp(self):
        self.assertEqual(self.db.latest_published_timestamp(), 0)
        profile = {
            "video_id": "latest",
            "href": "https://www.douyin.com/video/latest",
        }
        details = {
            "published_timestamp": 1_783_742_400,
            "published_at": "2026-07-11 12:00:00",
        }
        self.db.register_new_video(profile, details)
        self.assertEqual(self.db.latest_published_timestamp(), 1_783_742_400)

    def test_interaction_thread_freezes_after_three_confirmed_misses(self):
        profile = {"video_id": "v3", "href": "https://www.douyin.com/video/v3"}
        self.db.register_new_video(
            profile,
            {
                "published_timestamp": int(datetime.now().timestamp()),
                "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        parent = comment("p3")
        reply = comment("r3", "模型先生", AUTHOR_SEC_UID, "作者回复")
        self.db.upsert_interaction(
            "v3",
            {
                "parent": parent,
                "author_liked": False,
                "author_commented": False,
                "author_replies": [reply],
                "replies": [{**reply, "is_author": True}],
            },
        )
        self.assertEqual(self.db.mark_interaction_failure("v3", "p3"), (1, False))
        self.assertEqual(self.db.mark_interaction_failure("v3", "p3"), (2, False))
        self.assertEqual(self.db.mark_interaction_failure("v3", "p3"), (3, True))
        self.assertEqual(len(self.db.active_interactions("v3")), 0)
        frozen = self.db.connection.execute(
            "SELECT tracking_status, replies_json FROM interaction_threads "
            "WHERE video_id = 'v3' AND parent_comment_id = 'p3'"
        ).fetchone()
        self.assertEqual(frozen["tracking_status"], "frozen")
        self.assertIn("作者回复", frozen["replies_json"])


class VideoIsolationTests(unittest.TestCase):
    @staticmethod
    def video(video_id):
        return {
            "video_id": video_id,
            "published_at": "2026-07-12 10:00:00",
            "description": "",
        }

    def make_monitor(self, failure_kind):
        processed = []
        scheduled = []

        class FakeDB:
            def due_videos(self_inner):
                return [self.video("A"), self.video("B")]

            def schedule_poll(self_inner, video_id, next_poll_at):
                scheduled.append((video_id, next_poll_at))

        class FakeConsole:
            def line(self_inner, *args, **kwargs):
                pass

            def detail(self_inner, *args, **kwargs):
                pass

        monitor = object.__new__(MrModelMonitor)
        monitor.db = FakeDB()
        monitor.console = FakeConsole()

        def poll_video(video):
            processed.append(video["video_id"])
            if video["video_id"] == "A":
                raise MonitorFailure(failure_kind, "audit failure")

        monitor.poll_video = poll_video
        return monitor, processed, scheduled

    def test_one_video_data_failure_does_not_skip_the_next_video(self):
        monitor, processed, scheduled = self.make_monitor("data")
        monitor._poll_due_videos()
        self.assertEqual(processed, ["A", "B"])
        self.assertEqual([item[0] for item in scheduled], ["A"])

    def test_network_failure_still_pauses_before_the_next_video(self):
        monitor, processed, scheduled = self.make_monitor("network")
        with self.assertRaises(MonitorFailure):
            monitor._poll_due_videos()
        self.assertEqual(processed, ["A"])
        self.assertEqual(scheduled, [])


if __name__ == "__main__":
    unittest.main()
