import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


AUTHOR_SEC_UID = "MS4wLjABAAAAK713M9d8PGNb_WiMYf7yKhOI5y60H4uELJK2guDjJT0"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_value(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def merge_comment_rows(previous, current):
    """回复线程使用并集合并，已经见过的回复不会因删除而消失。"""
    merged = {}
    order = []
    for row in [*(previous or []), *(current or [])]:
        comment_id = str((row or {}).get("comment_id") or "")
        if not comment_id:
            continue
        if comment_id not in merged:
            order.append(comment_id)
            merged[comment_id] = row
        else:
            merged[comment_id] = {**merged[comment_id], **row}
    return [merged[comment_id] for comment_id in order]


class MonitorDB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    def close(self):
        self.connection.close()

    def _create_schema(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                url TEXT NOT NULL DEFAULT '',
                cover TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                description_source TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                published_timestamp INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                like_count INTEGER NOT NULL DEFAULT 0,
                comment_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_success_at TEXT NOT NULL DEFAULT '',
                monitor_started_at TEXT NOT NULL DEFAULT '',
                monitor_ends_at TEXT NOT NULL DEFAULT '',
                next_poll_at TEXT NOT NULL DEFAULT '',
                monitor_status TEXT NOT NULL DEFAULT 'archived',
                deleted_at TEXT NOT NULL DEFAULT '',
                delete_miss_count INTEGER NOT NULL DEFAULT 0,
                transcript_status TEXT NOT NULL DEFAULT 'not_processed',
                transcript_json TEXT NOT NULL DEFAULT '',
                transcript_error TEXT NOT NULL DEFAULT '',
                transcript_attempts INTEGER NOT NULL DEFAULT 0,
                transcript_next_attempt_at TEXT NOT NULL DEFAULT '',
                comments_status TEXT NOT NULL DEFAULT 'not_processed',
                comments_json TEXT NOT NULL DEFAULT '[]',
                comment_reply_count INTEGER NOT NULL DEFAULT 0,
                scanned_top_comment_count INTEGER NOT NULL DEFAULT 0,
                media_url TEXT NOT NULL DEFAULT '',
                source_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS interaction_threads (
                video_id TEXT NOT NULL,
                parent_comment_id TEXT NOT NULL,
                parent_json TEXT NOT NULL,
                author_liked INTEGER NOT NULL DEFAULT 0,
                author_commented INTEGER NOT NULL DEFAULT 0,
                author_replies_json TEXT NOT NULL DEFAULT '[]',
                replies_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                tracking_status TEXT NOT NULL DEFAULT 'active',
                frozen_at TEXT NOT NULL DEFAULT '',
                failure_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (video_id, parent_comment_id),
                FOREIGN KEY (video_id) REFERENCES videos(video_id)
            );

            CREATE TABLE IF NOT EXISTS important_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                video_id TEXT NOT NULL,
                parent_comment_id TEXT NOT NULL DEFAULT '',
                reply_comment_id TEXT NOT NULL DEFAULT '',
                detected_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (video_id) REFERENCES videos(video_id)
            );

            CREATE TABLE IF NOT EXISTS stat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                like_count INTEGER NOT NULL,
                comment_count INTEGER NOT NULL,
                UNIQUE (video_id, captured_at),
                FOREIGN KEY (video_id) REFERENCES videos(video_id)
            );

            CREATE INDEX IF NOT EXISTS idx_videos_due
                ON videos(monitor_status, next_poll_at);
            CREATE INDEX IF NOT EXISTS idx_interactions_video
                ON interaction_threads(video_id, tracking_status);
            CREATE INDEX IF NOT EXISTS idx_events_video
                ON important_events(video_id, detected_at);
            """
        )
        self.connection.commit()

    def get_meta(self, key, default=""):
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.connection.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        self.connection.commit()

    def touch_dashboard(self, changed_at=None):
        self.set_meta("dashboard_updated_at", changed_at or now_iso())

    def bootstrap_dashboard(self, payload):
        if self.get_meta("bootstrap_completed") == "1":
            return 0

        imported = 0
        imported_at = now_iso()
        with self.connection:
            for video in payload.get("videos") or []:
                video_id = str(video.get("video_id") or "")
                if not video_id:
                    continue
                comments = video.get("comments") or []
                transcript = video.get("transcript")
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO videos(
                        video_id, url, cover, description, description_source,
                        published_at, published_timestamp, duration_ms,
                        like_count, comment_count, first_seen_at, last_seen_at,
                        last_success_at, monitor_status, transcript_status,
                        transcript_json, transcript_error, comments_status,
                        comments_json, comment_reply_count, scanned_top_comment_count,
                        source_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        video_id,
                        str(video.get("url") or ""),
                        str(video.get("cover") or ""),
                        str(video.get("description") or ""),
                        str(video.get("description_source") or ""),
                        str(video.get("published_at") or ""),
                        int(video.get("published_timestamp") or 0),
                        int(video.get("duration_ms") or 0),
                        int(video.get("like_count") or 0),
                        int(video.get("comment_count") or 0),
                        imported_at,
                        imported_at,
                        imported_at,
                        "archived",
                        str(video.get("transcript_status") or "not_processed"),
                        json_text(transcript) if transcript else "",
                        str(video.get("transcript_error") or ""),
                        str(video.get("comments_status") or "not_processed"),
                        json_text(comments),
                        int(video.get("comment_reply_count") or 0),
                        int(video.get("scanned_top_comment_count") or 0),
                        json_text(video),
                    ),
                )
                imported += 1

                comments_by_id = {
                    str(comment.get("comment_id") or ""): comment
                    for comment in comments
                }
                for thread in video.get("interaction_threads") or []:
                    parent = thread.get("parent") or {}
                    parent_id = str(parent.get("comment_id") or "")
                    if not parent_id:
                        continue
                    current_comment = comments_by_id.get(parent_id) or {}
                    replies = current_comment.get("replies") or thread.get("replies") or thread.get("author_replies") or []
                    normalized_replies = [
                        {**reply, "is_author": bool(reply.get("is_author") or self._is_author(reply))}
                        for reply in replies
                    ]
                    normalized_thread = {
                        "parent": parent,
                        "author_liked": bool(thread.get("author_liked")),
                        "author_commented": bool(thread.get("author_commented") or self._is_author(parent)),
                        "author_replies": thread.get("author_replies") or [
                            reply for reply in normalized_replies if reply.get("is_author")
                        ],
                        "replies": normalized_replies,
                    }
                    self._upsert_interaction(video_id, normalized_thread, imported_at)

            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('bootstrap_completed', '1')"
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('dashboard_updated_at', ?)",
                (str(payload.get("generated_at") or imported_at),),
            )
            self.connection.execute(
                """
                UPDATE interaction_threads SET tracking_status = 'frozen', frozen_at = ?
                WHERE video_id IN (
                    SELECT video_id FROM videos WHERE monitor_status != 'active'
                )
                """,
                (imported_at,),
            )
        return imported

    @staticmethod
    def _is_author(comment):
        user = (comment or {}).get("user") or {}
        return str(user.get("sec_uid") or "") == AUTHOR_SEC_UID

    def video_exists(self, video_id):
        return self.connection.execute(
            "SELECT 1 FROM videos WHERE video_id = ?", (str(video_id),)
        ).fetchone() is not None

    def known_video_ids(self):
        return {
            row["video_id"]
            for row in self.connection.execute("SELECT video_id FROM videos")
        }

    def latest_published_timestamp(self):
        row = self.connection.execute(
            "SELECT COALESCE(MAX(published_timestamp), 0) AS value FROM videos"
        ).fetchone()
        return int(row["value"] or 0)

    def get_video(self, video_id):
        return self.connection.execute(
            "SELECT * FROM videos WHERE video_id = ?", (str(video_id),)
        ).fetchone()

    def register_new_video(self, profile_video, details, media_url=""):
        created_at = now_iso()
        video_id = str(profile_video.get("video_id") or "")
        published_timestamp = int(details.get("published_timestamp") or 0)
        published_dt = datetime.fromtimestamp(published_timestamp) if published_timestamp else datetime.now()
        monitor_ends = published_dt + timedelta(hours=48)
        monitor_status = "active" if monitor_ends > datetime.now() else "archived"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO videos(
                    video_id, url, cover, description, description_source,
                    published_at, published_timestamp, duration_ms,
                    like_count, comment_count, first_seen_at, last_seen_at,
                    last_success_at, monitor_started_at, monitor_ends_at,
                    next_poll_at, monitor_status, transcript_status,
                    media_url, source_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    video_id,
                    str(profile_video.get("href") or f"https://www.douyin.com/video/{video_id}"),
                    str(profile_video.get("cover") or ""),
                    str(details.get("description") or profile_video.get("title_hint") or ""),
                    str(details.get("description_source") or ""),
                    str(details.get("published_at") or published_dt.strftime("%Y-%m-%d %H:%M:%S")),
                    int(published_dt.timestamp()),
                    int(details.get("duration_ms") or 0),
                    int(details.get("like_count") or 0),
                    int(details.get("comment_count") or 0),
                    created_at,
                    created_at,
                    created_at,
                    published_dt.isoformat(timespec="seconds"),
                    monitor_ends.isoformat(timespec="seconds"),
                    created_at if monitor_status == "active" else "",
                    monitor_status,
                    "pending" if monitor_status == "active" else "not_processed",
                    str(media_url or ""),
                    json_text(profile_video),
                ),
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO important_events(
                    event_key, event_type, video_id, detected_at, payload_json
                ) VALUES(?,?,?,?,?)
                """,
                (
                    f"new_video:{video_id}",
                    "new_video",
                    video_id,
                    created_at,
                    json_text({"profile": profile_video, "details": details}),
                ),
            )
        self.touch_dashboard(created_at)
        return self.get_video(video_id)

    def active_videos(self):
        return self.connection.execute(
            "SELECT * FROM videos WHERE monitor_status = 'active' ORDER BY published_timestamp DESC"
        ).fetchall()

    def due_videos(self, moment=None):
        value = (moment or datetime.now()).isoformat(timespec="seconds")
        return self.connection.execute(
            """
            SELECT * FROM videos
            WHERE monitor_status = 'active'
              AND next_poll_at != ''
              AND next_poll_at <= ?
            ORDER BY next_poll_at, published_timestamp DESC
            """,
            (value,),
        ).fetchall()

    def due_transcriptions(self, moment=None):
        value = (moment or datetime.now()).isoformat(timespec="seconds")
        return self.connection.execute(
            """
            SELECT * FROM videos
            WHERE monitor_status = 'active'
              AND transcript_status IN ('pending', 'error')
              AND transcript_attempts < 3
              AND (transcript_next_attempt_at = '' OR transcript_next_attempt_at <= ?)
            ORDER BY published_timestamp
            """,
            (value,),
        ).fetchall()

    def mark_profile_seen(self, video_ids):
        if not video_ids:
            return
        seen_at = now_iso()
        placeholders = ",".join("?" for _ in video_ids)
        with self.connection:
            self.connection.execute(
                f"""
                UPDATE videos
                SET last_seen_at = ?, delete_miss_count = 0
                WHERE video_id IN ({placeholders})
                """,
                (seen_at, *video_ids),
            )

    def schedule_poll(self, video_id, next_poll_at):
        self.connection.execute(
            "UPDATE videos SET next_poll_at = ? WHERE video_id = ?",
            (next_poll_at, str(video_id)),
        )
        self.connection.commit()

    def update_video_snapshot(self, video_id, details, captured_at=None):
        captured_at = captured_at or now_iso()
        current = self.get_video(video_id)
        if not current:
            return False
        like_count = int(details.get("like_count") or 0)
        comment_count = int(details.get("comment_count") or 0)
        changed = like_count != current["like_count"] or comment_count != current["comment_count"]
        with self.connection:
            if changed:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO stat_history(
                        video_id, captured_at, like_count, comment_count
                    ) VALUES(?,?,?,?)
                    """,
                    (str(video_id), captured_at, like_count, comment_count),
                )
            self.connection.execute(
                """
                UPDATE videos SET
                    description = CASE WHEN ? != '' THEN ? ELSE description END,
                    description_source = CASE WHEN ? != '' THEN ? ELSE description_source END,
                    published_at = CASE WHEN ? != '' THEN ? ELSE published_at END,
                    published_timestamp = CASE WHEN ? > 0 THEN ? ELSE published_timestamp END,
                    duration_ms = CASE WHEN ? > 0 THEN ? ELSE duration_ms END,
                    like_count = ?, comment_count = ?, last_success_at = ?,
                    delete_miss_count = 0
                WHERE video_id = ?
                """,
                (
                    str(details.get("description") or ""), str(details.get("description") or ""),
                    str(details.get("description_source") or ""), str(details.get("description_source") or ""),
                    str(details.get("published_at") or ""), str(details.get("published_at") or ""),
                    int(details.get("published_timestamp") or 0), int(details.get("published_timestamp") or 0),
                    int(details.get("duration_ms") or 0), int(details.get("duration_ms") or 0),
                    like_count, comment_count, captured_at, str(video_id),
                ),
            )
        if changed:
            self.touch_dashboard(captured_at)
        return changed

    def update_comments(self, video_id, comments, reply_count, captured_at=None):
        captured_at = captured_at or now_iso()
        current = self.get_video(video_id)
        encoded = json_text(comments)
        changed = not current or current["comments_json"] != encoded
        with self.connection:
            self.connection.execute(
                """
                UPDATE videos SET comments_status = 'complete', comments_json = ?,
                    comment_reply_count = ?, scanned_top_comment_count = ?,
                    last_success_at = ?
                WHERE video_id = ?
                """,
                (encoded, int(reply_count), len(comments), captured_at, str(video_id)),
            )
        if changed:
            self.touch_dashboard(captured_at)
        return changed

    def set_transcript_processing(self, video_id):
        self.connection.execute(
            """
            UPDATE videos SET transcript_status = 'processing',
                transcript_attempts = transcript_attempts + 1,
                transcript_error = ''
            WHERE video_id = ?
            """,
            (str(video_id),),
        )
        self.connection.commit()

    def set_transcript_complete(self, video_id, transcript):
        completed_at = now_iso()
        self.connection.execute(
            """
            UPDATE videos SET transcript_status = 'complete', transcript_json = ?,
                transcript_error = '', transcript_next_attempt_at = ''
            WHERE video_id = ?
            """,
            (json_text(transcript), str(video_id)),
        )
        self.connection.commit()
        self.touch_dashboard(completed_at)

    def set_transcript_error(self, video_id, message, retry_at=""):
        self.connection.execute(
            """
            UPDATE videos SET transcript_status = 'error', transcript_error = ?,
                transcript_next_attempt_at = ?
            WHERE video_id = ?
            """,
            (str(message), str(retry_at), str(video_id)),
        )
        self.connection.commit()

    def set_media_url(self, video_id, media_url):
        self.connection.execute(
            "UPDATE videos SET media_url = ? WHERE video_id = ?",
            (str(media_url or ""), str(video_id)),
        )
        self.connection.commit()

    def _insert_event(self, event_key, event_type, video_id, parent_id, reply_id, payload, detected_at):
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO important_events(
                event_key, event_type, video_id, parent_comment_id,
                reply_comment_id, detected_at, payload_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                event_key, event_type, str(video_id), str(parent_id or ""),
                str(reply_id or ""), detected_at, json_text(payload),
            ),
        )
        return cursor.rowcount > 0

    def _upsert_interaction(self, video_id, thread, detected_at):
        parent = thread.get("parent") or {}
        parent_id = str(parent.get("comment_id") or "")
        if not parent_id:
            return 0, False
        existing = self.connection.execute(
            """
            SELECT * FROM interaction_threads
            WHERE video_id = ? AND parent_comment_id = ?
            """,
            (str(video_id), parent_id),
        ).fetchone()

        previous_replies = json_value(existing["replies_json"], []) if existing else []
        previous_author_replies = json_value(existing["author_replies_json"], []) if existing else []
        replies = merge_comment_rows(previous_replies, thread.get("replies") or [])
        author_replies = merge_comment_rows(
            previous_author_replies,
            thread.get("author_replies") or [reply for reply in replies if reply.get("is_author")],
        )
        author_liked = bool((existing and existing["author_liked"]) or thread.get("author_liked"))
        author_commented = bool((existing and existing["author_commented"]) or thread.get("author_commented"))
        first_seen = existing["first_seen_at"] if existing else detected_at
        old_parent = json_value(existing["parent_json"], {}) if existing else {}
        merged_parent = {**old_parent, **parent}
        merged_parent["reply_total"] = max(
            int(old_parent.get("reply_total") or 0),
            int(parent.get("reply_total") or 0),
            len(replies),
        )

        previous_state = None if not existing else (
            existing["parent_json"], existing["author_liked"],
            existing["author_commented"], existing["author_replies_json"],
            existing["replies_json"],
        )
        new_state = (
            json_text(merged_parent), int(author_liked), int(author_commented),
            json_text(author_replies), json_text(replies),
        )
        changed = previous_state != new_state

        self.connection.execute(
            """
            INSERT INTO interaction_threads(
                video_id, parent_comment_id, parent_json, author_liked,
                author_commented, author_replies_json, replies_json,
                first_seen_at, last_seen_at, tracking_status, frozen_at,
                failure_count
            ) VALUES(?,?,?,?,?,?,?,?,?,'active','',0)
            ON CONFLICT(video_id, parent_comment_id) DO UPDATE SET
                parent_json = excluded.parent_json,
                author_liked = excluded.author_liked,
                author_commented = excluded.author_commented,
                author_replies_json = excluded.author_replies_json,
                replies_json = excluded.replies_json,
                last_seen_at = excluded.last_seen_at,
                failure_count = 0
            """,
            (
                str(video_id), parent_id, *new_state,
                first_seen, detected_at,
            ),
        )

        new_events = 0
        if thread.get("author_liked"):
            new_events += self._insert_event(
                f"author_like:{video_id}:{parent_id}", "author_like",
                video_id, parent_id, "", {"parent": parent}, detected_at,
            )
        if thread.get("author_commented"):
            new_events += self._insert_event(
                f"author_comment:{video_id}:{parent_id}", "author_comment",
                video_id, parent_id, "", {"parent": parent}, detected_at,
            )
        for reply in thread.get("author_replies") or []:
            reply_id = str(reply.get("comment_id") or "")
            if not reply_id:
                continue
            new_events += self._insert_event(
                f"author_reply:{video_id}:{parent_id}:{reply_id}", "author_reply",
                video_id, parent_id, reply_id,
                {"parent": parent, "reply": reply}, detected_at,
            )
        return int(new_events), changed

    def upsert_interaction(self, video_id, thread, detected_at=None):
        detected_at = detected_at or now_iso()
        with self.connection:
            new_events, changed = self._upsert_interaction(video_id, thread, detected_at)
        if changed or new_events:
            self.touch_dashboard(detected_at)
        return new_events, changed

    def active_interactions(self, video_id):
        return self.connection.execute(
            """
            SELECT * FROM interaction_threads
            WHERE video_id = ? AND tracking_status = 'active'
            ORDER BY first_seen_at
            """,
            (str(video_id),),
        ).fetchall()

    def all_interactions(self):
        return self.connection.execute(
            """
            SELECT interaction_threads.*, videos.url, videos.description,
                   videos.monitor_status
            FROM interaction_threads
            JOIN videos USING(video_id)
            ORDER BY videos.published_timestamp DESC, interaction_threads.first_seen_at
            """
        ).fetchall()

    def mark_interaction_failure(self, video_id, parent_id, freeze_after=3):
        row = self.connection.execute(
            """
            SELECT failure_count FROM interaction_threads
            WHERE video_id = ? AND parent_comment_id = ?
            """,
            (str(video_id), str(parent_id)),
        ).fetchone()
        if not row:
            return 0, False
        failures = int(row["failure_count"] or 0) + 1
        frozen = failures >= freeze_after
        self.connection.execute(
            """
            UPDATE interaction_threads SET failure_count = ?,
                tracking_status = CASE WHEN ? THEN 'frozen' ELSE tracking_status END,
                frozen_at = CASE WHEN ? THEN ? ELSE frozen_at END
            WHERE video_id = ? AND parent_comment_id = ?
            """,
            (failures, int(frozen), int(frozen), now_iso(), str(video_id), str(parent_id)),
        )
        self.connection.commit()
        if frozen:
            self.touch_dashboard(now_iso())
        return failures, frozen

    def freeze_video(self, video_id, status, reason=""):
        frozen_at = now_iso()
        with self.connection:
            self.connection.execute(
                """
                UPDATE videos SET monitor_status = ?, next_poll_at = '',
                    deleted_at = CASE WHEN ? = 'deleted' THEN ? ELSE deleted_at END,
                    transcript_next_attempt_at = ''
                WHERE video_id = ?
                """,
                (status, status, frozen_at, str(video_id)),
            )
            self.connection.execute(
                """
                UPDATE interaction_threads SET tracking_status = 'frozen',
                    frozen_at = CASE WHEN frozen_at = '' THEN ? ELSE frozen_at END
                WHERE video_id = ?
                """,
                (frozen_at, str(video_id)),
            )
            if status == "deleted":
                self._insert_event(
                    f"video_deleted:{video_id}", "video_deleted", video_id,
                    "", "", {"reason": reason}, frozen_at,
                )
        self.touch_dashboard(frozen_at)

    def increment_delete_miss(self, video_id):
        self.connection.execute(
            """
            UPDATE videos SET delete_miss_count = delete_miss_count + 1
            WHERE video_id = ?
            """,
            (str(video_id),),
        )
        self.connection.commit()
        return int(self.get_video(video_id)["delete_miss_count"])

    def reset_delete_miss(self, video_id):
        self.connection.execute(
            "UPDATE videos SET delete_miss_count = 0 WHERE video_id = ?",
            (str(video_id),),
        )
        self.connection.commit()

    def _interaction_payloads(self, video_id):
        rows = self.connection.execute(
            """
            SELECT * FROM interaction_threads
            WHERE video_id = ? ORDER BY first_seen_at
            """,
            (str(video_id),),
        ).fetchall()
        return [
            {
                "parent": json_value(row["parent_json"], {}),
                "author_liked": bool(row["author_liked"]),
                "author_commented": bool(row["author_commented"]),
                "author_replies": json_value(row["author_replies_json"], []),
                "replies": json_value(row["replies_json"], []),
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "tracking_status": row["tracking_status"],
            }
            for row in rows
        ]

    def build_dashboard_payload(self):
        rows = self.connection.execute(
            """
            SELECT * FROM videos
            ORDER BY published_timestamp DESC, first_seen_at DESC
            """
        ).fetchall()
        videos = []
        for order, row in enumerate(rows, 1):
            interactions = self._interaction_payloads(row["video_id"])
            transcript = json_value(row["transcript_json"], None)
            comments = json_value(row["comments_json"], [])
            videos.append(
                {
                    "order": order,
                    "video_id": row["video_id"],
                    "url": row["url"],
                    "cover": row["cover"],
                    "description": row["description"] or "该视频未填写文案",
                    "description_source": row["description_source"],
                    "published_at": row["published_at"],
                    "published_timestamp": row["published_timestamp"],
                    "like_count": row["like_count"],
                    "comment_count": row["comment_count"],
                    "duration_ms": row["duration_ms"],
                    "monitor_status": row["monitor_status"],
                    "monitor_ends_at": row["monitor_ends_at"],
                    "is_deleted": row["monitor_status"] == "deleted",
                    "deleted_at": row["deleted_at"],
                    "interaction_threads": interactions,
                    "has_author_interaction": bool(interactions),
                    "scanned_top_comment_count": row["scanned_top_comment_count"],
                    "transcript_status": row["transcript_status"],
                    "transcript": transcript,
                    "transcript_error": row["transcript_error"],
                    "comments_status": row["comments_status"],
                    "comments": comments,
                    "comment_reply_count": row["comment_reply_count"],
                }
            )
        return {
            "generated_at": self.get_meta("dashboard_updated_at", now_iso()),
            "author": {"name": "模型先生", "sec_uid": AUTHOR_SEC_UID},
            "scan_note": "新视频监控 48 小时；前 20 条评论持续更新；博主互动永久保留。",
            "video_count": len(videos),
            "interaction_video_count": sum(video["has_author_interaction"] for video in videos),
            "transcribed_video_count": sum(video["transcript_status"] == "complete" for video in videos),
            "comment_enriched_video_count": sum(video["comments_status"] == "complete" for video in videos),
            "videos": videos,
        }

    def export_dashboard(self, *paths):
        payload = self.build_dashboard_payload()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        for raw_path in paths:
            path = Path(raw_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        return payload
