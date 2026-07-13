import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from douyin_author_videos import AUTHOR_URL, extract_videos_from_page, launch_douyin_context
from douyin_dashboard_collect import (
    AUTHOR_SEC_UID,
    direct_comments,
    direct_value,
    find_aweme,
    image_url,
    load_api_seeds,
    load_source_videos,
    normalize_comment,
    parse_video_detail,
    replace_query_value,
)
from douyin_dashboard_enrich import load_reply_seed, media_url
from mrmodel_db import MonitorDB, json_value, now_iso, parse_iso
from mrmodel_transcribe import TranscriptionEngine


ROOT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT_DIR / "runtime"
DATA_DIR = ROOT_DIR / "data"
DASHBOARD_DIR = ROOT_DIR / "dashboard"
DASHBOARD_JSON = DASHBOARD_DIR / "public" / "data" / "videos.json"
LOCAL_DASHBOARD_JSON = DATA_DIR / "dashboard_videos_latest.json"
DATABASE_PATH = DATA_DIR / "mrmodel_monitor.db"
LOG_PATH = DATA_DIR / "mrmodel_monitor.log"
LOCK_PATH = RUNTIME_DIR / "mrmodel_monitor.lock"
TRANSCRIPTION_QUEUE_DIR = RUNTIME_DIR / "transcription_queue"
COMMENT_SNAPSHOT_LIMIT = 20
INTERACTION_DISCOVERY_LIMIT = 100


class MonitorFailure(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class Console:
    def __init__(self, log_path=LOG_PATH):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("mrmodel-monitor")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)

    def line(self, message, level="info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}", flush=True)
        getattr(self.logger, level)(message)

    def detail(self, message, exc_info=False):
        self.logger.debug(message, exc_info=exc_info)


class SingleInstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("监控程序已经在运行。") from exc

    def release(self):
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def monitor_interval_seconds(published_timestamp, moment=None):
    moment = moment or datetime.now()
    try:
        published = datetime.fromtimestamp(int(published_timestamp))
    except (TypeError, ValueError, OSError):
        published = moment
    age = moment - published
    if age < timedelta(0):
        age = timedelta(0)
    if age < timedelta(hours=3):
        return 3 * 60
    if age < timedelta(hours=24):
        return 20 * 60
    if age < timedelta(hours=48):
        return 60 * 60
    return None


def is_newer_publication(published_timestamp, latest_known_timestamp):
    try:
        candidate = int(published_timestamp)
        latest_known = int(latest_known_timestamp)
    except (TypeError, ValueError):
        return False
    return candidate > 0 and candidate > latest_known


def is_global_api_failure(failure):
    return getattr(failure, "kind", "") in {"verification", "auth"}


def is_global_monitor_failure(failure):
    return getattr(failure, "kind", "") in {"verification", "auth", "network"}


def is_confirmed_empty_reply_thread(known_count, reported_total, returned_count):
    try:
        known = int(known_count)
        total = int(reported_total)
        returned = int(returned_count)
    except (TypeError, ValueError):
        return False
    return known > 0 and total == 0 and returned == 0


def classify_response(url, text, status):
    combined = f"{url}\n{text[:5000]}".lower()
    captcha_markers = (
        "captcha", "verifycenter", "verify_fp", "安全验证", "验证码", "滑块验证",
        "完成验证", "异常访问",
    )
    login_markers = (
        "passport.douyin.com", "/login", "扫码登录", "手机号登录", "登录后查看",
        "login_required", "not_login",
    )
    if status in (429, 403) or any(marker in combined for marker in captcha_markers):
        return "verification"
    if status == 401 or any(marker in combined for marker in login_markers):
        return "auth"
    if status >= 500:
        return "network"
    return "ok"


def is_author_comment(comment):
    user = (comment or {}).get("user") or {}
    return str(user.get("sec_uid") or "") == AUTHOR_SEC_UID


def normalize_reply(reply):
    normalized = normalize_comment(reply)
    normalized["is_author"] = bool(
        is_author_comment(normalized)
        or direct_value(reply, "label_type") == 1
        or str(direct_value(reply, "label_text") or "") == "作者"
    )
    return normalized


def interaction_candidate_from_parent(parent):
    if not isinstance(parent, dict):
        return None
    normalized_parent = normalize_comment(parent)
    embedded = direct_value(parent, "reply_comment", "reply_comments")
    if not isinstance(embedded, list):
        embedded = []
    author_replies = [
        normalized
        for reply in embedded
        if isinstance(reply, dict)
        for normalized in [normalize_reply(reply)]
        if normalized.get("is_author")
    ]
    author_liked = bool(direct_value(parent, "is_author_digged"))
    author_commented = is_author_comment(normalized_parent)
    if not author_liked and not author_commented and not author_replies:
        return None
    return {
        "parent": normalized_parent,
        "author_liked": author_liked,
        "author_commented": author_commented,
        "author_replies": author_replies,
        "replies": author_replies,
    }


def clean_homepage_description(value):
    text = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?(?:万|亿|w|W|k|K)?", text):
        return ""
    return text


def dedupe_raw_comments(rows):
    result = []
    seen = set()
    for row in rows:
        comment_id = str(direct_value(row, "cid", "comment_id", "id") or "")
        if not comment_id or comment_id in seen:
            continue
        seen.add(comment_id)
        result.append(row)
    return result


class GitPublisher:
    def __init__(self, console):
        self.console = console

    def _run(self, *args):
        return subprocess.run(
            ["git", "-C", str(DASHBOARD_DIR), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )

    def sync(self):
        status = self._run("status", "--porcelain", "--", "public/data/videos.json")
        if status.returncode != 0:
            raise RuntimeError(status.stderr.strip() or "无法读取 Git 状态")
        if not status.stdout.strip():
            self.console.line("GitHub 暂无数据需要同步")
            return False
        self._checked("add", "public/data/videos.json")
        message = f"Update monitor snapshot {datetime.now():%Y-%m-%d %H:%M}"
        self._checked("commit", "-m", message)
        self._checked("push")
        self.console.line("GitHub 同步完成：公开快照已推送")
        return True

    def _checked(self, *args):
        result = self._run(*args)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result


class MrModelMonitor:
    def __init__(self, args):
        self.args = args
        self.console = Console(args.log_path)
        self.db = MonitorDB(args.database)
        self.detail_seed, self.comment_seed = load_api_seeds(load_source_videos(1))
        self.reply_seed = load_reply_seed()
        self.playwright = None
        self.context = None
        self.page = None
        self.last_profile_ids = set()
        self.profile_healthy = False
        self.profile_next_at = datetime.now()
        self.git_next_at = datetime.now() + timedelta(seconds=args.git_interval)
        self.pause_until = None
        self.publisher = GitPublisher(self.console)
        self.transcriber = TranscriptionEngine("small")
        self.transcription_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mrmodel-transcribe")
        self.transcription_futures = {}

    def bootstrap(self):
        if DASHBOARD_JSON.exists():
            payload = json.loads(DASHBOARD_JSON.read_text(encoding="utf-8-sig"))
            imported = self.db.bootstrap_dashboard(payload)
            if imported:
                self.console.line(f"历史看板已导入：{imported} 个视频")
        self.db.connection.execute(
            "UPDATE videos SET transcript_status = 'pending' WHERE transcript_status = 'processing' AND monitor_status = 'active'"
        )
        self.db.connection.execute(
            """
            UPDATE interaction_threads SET tracking_status = 'frozen',
                frozen_at = CASE WHEN frozen_at = '' THEN ? ELSE frozen_at END
            WHERE video_id IN (SELECT video_id FROM videos WHERE monitor_status != 'active')
            """,
            (now_iso(),),
        )
        self.db.connection.commit()
        payload = self.export_dashboard()
        self.console.line(
            f"监控数据库已就绪：{payload['video_count']} 个视频，"
            f"{payload['interaction_video_count']} 个存在博主互动"
        )
        return payload

    def export_dashboard(self):
        return self.db.export_dashboard(LOCAL_DASHBOARD_JSON, DASHBOARD_JSON)

    def open_browser(self):
        self.playwright = sync_playwright().start()
        self.context = launch_douyin_context(self.playwright, headless=self.args.headless)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self):
        try:
            self._collect_transcription_results()
            self.transcription_pool.shutdown(wait=True, cancel_futures=False)
            self._collect_transcription_results()
        finally:
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()
            self.db.close()

    def _page_health(self, url, body, status):
        kind = classify_response(url, body, status)
        if kind != "ok":
            raise MonitorFailure(kind, "抖音主页需要验证或重新登录")

    def scan_profile(self):
        try:
            response = self.page.goto(
                AUTHOR_URL, wait_until="domcontentloaded", timeout=self.args.page_timeout_ms
            )
            status = response.status if response else 0
            try:
                self.page.wait_for_selector('a[href*="/video/"]', timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            self.page.wait_for_timeout(self.args.profile_wait_ms)
            body = self.page.locator("body").inner_text(timeout=10_000)
            self._page_health(self.page.url, body, status)
            result = extract_videos_from_page(self.page, viewport_only=False)
            rows = result.get("rows") or []
            if not rows:
                raise MonitorFailure("session", "主页没有返回视频列表")
            self.profile_healthy = True
            self.last_profile_ids = {str(row.get("video_id") or "") for row in rows}
            self.db.mark_profile_seen(self.last_profile_ids)
            self._register_new_videos(rows)
            self.console.line(f"主页读取成功：发现 {len(rows)} 个当前视频")
            return rows
        except MonitorFailure:
            self.profile_healthy = False
            raise
        except (PlaywrightTimeoutError, OSError) as exc:
            self.profile_healthy = False
            raise MonitorFailure("network", str(exc)) from exc
        except Exception as exc:
            self.profile_healthy = False
            self.console.detail("主页读取异常", exc_info=True)
            raise MonitorFailure("session", str(exc)) from exc

    def _request_json(self, url, referer, request_kind="api"):
        try:
            response = self.context.request.get(
                url, headers={"referer": referer}, timeout=self.args.request_timeout_ms
            )
        except Exception as exc:
            raise MonitorFailure("network", str(exc)) from exc
        text = response.text()
        kind = classify_response(response.url, text, response.status)
        if kind != "ok":
            raise MonitorFailure(kind, f"HTTP {response.status}")
        if response.status in (404, 410):
            raise MonitorFailure("missing", f"HTTP {response.status}")
        if response.status != 200:
            raise MonitorFailure("data", f"{request_kind} HTTP {response.status}")
        if not text:
            raise MonitorFailure("data", f"{request_kind} 接口返回空内容")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MonitorFailure("data", f"{request_kind} 接口返回了非 JSON 内容") from exc
        status_code = payload.get("status_code") if isinstance(payload, dict) else None
        message = str((payload or {}).get("status_msg") or (payload or {}).get("message") or "")
        message_kind = classify_response(response.url, message, 200)
        if message_kind != "ok":
            raise MonitorFailure(message_kind, message or "接口要求验证")
        if status_code not in (None, 0):
            raise MonitorFailure("missing", message or f"status_code={status_code}")
        return payload

    def _detail(self, video_id, referer):
        url = replace_query_value(self.detail_seed, "aweme_id", video_id)
        payload = self._request_json(url, referer, "detail")
        return payload, find_aweme(payload, video_id)

    def _top_comments(self, video_id, referer, wanted=20):
        rows = []
        cursor = 0
        seen_cursors = set()
        while len(rows) < wanted and cursor not in seen_cursors:
            seen_cursors.add(cursor)
            url = replace_query_value(self.comment_seed, "aweme_id", video_id)
            url = replace_query_value(url, "cursor", cursor)
            url = replace_query_value(url, "count", 20)
            payload = self._request_json(url, referer, "comments")
            rows.extend(direct_comments(payload))
            rows = dedupe_raw_comments(rows)
            if payload.get("has_more") in (0, False):
                break
            next_cursor = payload.get("cursor")
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        return rows[:wanted]

    def _all_replies(self, video_id, parent_id, referer, known_count=None):
        rows = []
        cursor = 0
        seen_cursors = set()
        for _ in range(200):
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            url = replace_query_value(self.reply_seed, "item_id", video_id)
            url = replace_query_value(url, "comment_id", parent_id)
            url = replace_query_value(url, "cursor", cursor)
            url = replace_query_value(url, "count", 20)
            payload = self._request_json(url, referer, f"replies:{parent_id}")
            rows.extend(direct_comments(payload))
            rows = dedupe_raw_comments(rows)
            total = int(payload.get("total") or 0)
            if is_confirmed_empty_reply_thread(known_count, total, len(rows)):
                raise MonitorFailure("missing", f"回复线程已不存在或已清空：{parent_id}")
            if known_count is not None and total <= int(known_count):
                break
            if payload.get("has_more") in (0, False):
                break
            next_cursor = payload.get("cursor")
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        return rows

    def _register_new_videos(self, profile_rows):
        known = self.db.known_video_ids()
        # 固定本轮开始前的门槛。若两条新视频在两次主页扫描之间连续发布，
        # 它们都应与旧门槛比较，不能让先入库的一条挡住后一条。
        latest_known_timestamp = self.db.latest_published_timestamp()
        baseline_ids = set(json_value(self.db.get_meta("profile_baseline_ids", "[]"), []))
        known_positions = [
            index for index, row in enumerate(profile_rows)
            if str(row.get("video_id") or "") in known
        ]
        last_known_position = max(known_positions) if known_positions else -1
        for index, profile_video in enumerate(profile_rows):
            video_id = str(profile_video.get("video_id") or "")
            if not video_id or video_id in known or video_id in baseline_ids:
                continue
            if last_known_position < 0 or index > last_known_position:
                baseline_ids.add(video_id)
                continue
            referer = str(profile_video.get("href") or f"https://www.douyin.com/video/{video_id}")
            profile_video = {
                **profile_video,
                "title_hint": clean_homepage_description(profile_video.get("title_hint")),
            }
            details = {
                "description": str(profile_video.get("title_hint") or ""),
                "description_source": "homepage",
                "published_at": "",
                "published_timestamp": 0,
                "like_count": 0,
                "comment_count": 0,
                "duration_ms": 0,
            }
            try:
                _, aweme = self._detail(video_id, referer)
            except MonitorFailure as exc:
                self.console.detail(f"新视频详情暂不可用 {video_id}: {exc}")
                continue
            if not aweme:
                self.console.detail(f"新视频详情为空，等待下轮确认 {video_id}")
                continue
            details = parse_video_detail(aweme, details)
            published_timestamp = int(details.get("published_timestamp") or 0)
            if not is_newer_publication(published_timestamp, latest_known_timestamp):
                # 未知 ID 位于旧视频区域或发布时间不晚于现有最新视频，视为历史基线。
                # 这可避免置顶、排序调整或稍后加载出的旧作品被误报为新视频。
                if published_timestamp > 0:
                    baseline_ids.add(video_id)
                    self.console.detail(
                        f"忽略非新发布视频 {video_id}: "
                        f"published={published_timestamp}, latest={latest_known_timestamp}"
                    )
                else:
                    self.console.detail(f"视频发布时间不可用，等待下轮确认 {video_id}")
                continue
            media = media_url(aweme)
            self.db.register_new_video(profile_video, details, media)
            self.export_dashboard()
            known.add(video_id)
            self.console.line(f"发现新视频：{self._label(video_id)}")
            self.console.line(f"视频卡片已生成：{video_id}")
        self.db.set_meta("profile_baseline_ids", json.dumps(sorted(baseline_ids), ensure_ascii=False))

    @staticmethod
    def _label(video_or_id):
        if isinstance(video_or_id, str):
            return f"视频 {video_or_id}"
        video_id = str(video_or_id["video_id"])
        published_at = str(video_or_id["published_at"] or "").replace("T", " ")
        if published_at:
            return f"{published_at[:16]} 视频（{video_id}）"
        return f"视频 {video_id}"

    def _build_top_snapshot(self, video, raw_comments):
        previous = {
            str(comment.get("comment_id") or ""): comment
            for comment in json_value(video["comments_json"], [])
        }
        threads = []
        reply_count = 0
        refreshed_reply_ids = set()
        reply_failures = 0
        reply_api_unavailable = False
        for parent in raw_comments:
            normalized = normalize_comment(parent)
            parent_id = normalized["comment_id"]
            previous_thread = previous.get(parent_id)
            reply_total = int(normalized.get("reply_total") or 0)
            previous_total = int((previous_thread or {}).get("reply_total") or -1)
            previous_reply_status = str((previous_thread or {}).get("replies_status") or "complete")
            needs_reply_refresh = reply_total > 0 and (
                not previous_thread
                or reply_total != previous_total
                or previous_reply_status != "complete"
            )
            if needs_reply_refresh:
                if reply_api_unavailable:
                    replies = (previous_thread or {}).get("replies") or []
                    normalized["replies_status"] = "error"
                    reply_failures += 1
                else:
                    try:
                        replies = [
                            normalize_reply(reply)
                            for reply in self._all_replies(
                                video["video_id"], parent_id, video["url"]
                            )
                        ]
                        normalized["replies_status"] = "complete"
                        refreshed_reply_ids.add(parent_id)
                    except MonitorFailure as exc:
                        if is_global_api_failure(exc):
                            raise
                        replies = (previous_thread or {}).get("replies") or []
                        normalized["replies_status"] = "error"
                        reply_failures += 1
                        if exc.kind in {"data", "network"}:
                            reply_api_unavailable = True
                        self.console.detail(
                            f"完整回复接口本轮暂停 video={video['video_id']} "
                            f"first_parent={parent_id}: {exc}"
                        )
            else:
                replies = (previous_thread or {}).get("replies") or []
                normalized["replies_status"] = previous_reply_status
            reply_count += len(replies)
            normalized["author_liked"] = bool(direct_value(parent, "is_author_digged"))
            normalized["replies"] = replies
            threads.append(normalized)
        return (
            threads,
            reply_count,
            refreshed_reply_ids,
            reply_failures,
            reply_api_unavailable,
        )

    def _interaction_from_thread(self, thread):
        replies = thread.get("replies") or []
        author_replies = [reply for reply in replies if reply.get("is_author")]
        author_commented = is_author_comment(thread)
        if not thread.get("author_liked") and not author_commented and not author_replies:
            return None
        parent = {
            key: value for key, value in thread.items()
            if key not in {"replies", "author_liked"}
        }
        return {
            "parent": parent,
            "author_liked": bool(thread.get("author_liked")),
            "author_commented": author_commented,
            "author_replies": author_replies,
            "replies": replies,
        }

    def _discover_interactions(self, video, raw_comments):
        new_events = 0
        for parent in raw_comments[:INTERACTION_DISCOVERY_LIMIT]:
            interaction = interaction_candidate_from_parent(parent)
            if not interaction:
                continue
            added, _ = self.db.upsert_interaction(video["video_id"], interaction)
            new_events += added
            if added:
                self.console.line(
                    f"前100条发现博主互动：{video['video_id']}｜"
                    f"评论 {interaction['parent']['comment_id']}｜新增事件 {added}"
                )
        return new_events

    def _track_interactions(
        self, video, top_threads, refreshed_reply_ids, skip_direct_refresh=False
    ):
        new_events = 0
        for thread in top_threads:
            interaction = self._interaction_from_thread(thread)
            if interaction:
                added, _ = self.db.upsert_interaction(video["video_id"], interaction)
                new_events += added

        if skip_direct_refresh:
            return new_events

        for known in self.db.active_interactions(video["video_id"]):
            parent_id = known["parent_comment_id"]
            if parent_id in refreshed_reply_ids:
                continue
            try:
                known_replies = json_value(known["replies_json"], [])
                replies = [
                    normalize_reply(reply)
                    for reply in self._all_replies(
                        video["video_id"], parent_id, video["url"], len(known_replies)
                    )
                ]
                parent = json_value(known["parent_json"], {})
                author_replies = [reply for reply in replies if reply.get("is_author")]
                self.db.upsert_interaction(
                    video["video_id"],
                    {
                        "parent": parent,
                        "author_liked": bool(known["author_liked"]),
                        "author_commented": bool(known["author_commented"]),
                        "author_replies": author_replies,
                        "replies": replies,
                    },
                )
            except MonitorFailure as exc:
                if is_global_api_failure(exc):
                    raise
                if exc.kind == "missing":
                    failures, frozen = self.db.mark_interaction_failure(
                        video["video_id"], parent_id
                    )
                    if frozen:
                        self.console.line(
                            f"互动线程已结束跟踪：{parent_id}｜连续 3 次不存在｜保留最后快照",
                            "warning",
                        )
                    else:
                        self.console.line(
                            f"互动线程不存在待确认：{parent_id}（{failures}/3）",
                            "warning",
                        )
                else:
                    self.console.detail(
                        f"互动完整回复本轮暂停 video={video['video_id']} "
                        f"first_parent={parent_id}: {exc}"
                    )
                    break
        return new_events

    def refresh_historical_interactions(self):
        rows = self.db.all_interactions()
        refreshed = 0
        for index, known in enumerate(rows, 1):
            parent_id = known["parent_comment_id"]
            try:
                replies = [
                    normalize_reply(reply)
                    for reply in self._all_replies(known["video_id"], parent_id, known["url"])
                ]
                parent = json_value(known["parent_json"], {})
                self.db.upsert_interaction(
                    known["video_id"],
                    {
                        "parent": parent,
                        "author_liked": bool(known["author_liked"]),
                        "author_commented": bool(known["author_commented"]),
                        "author_replies": [reply for reply in replies if reply.get("is_author")],
                        "replies": replies,
                    },
                )
                refreshed += 1
                self.console.line(
                    f"历史互动已补全：{index}/{len(rows)}｜{parent_id}｜{len(replies)} 条回复"
                )
            except MonitorFailure as exc:
                self.console.line(f"历史互动暂时无法补全：{parent_id}", "warning")
                self.console.detail(f"历史互动补全失败 {parent_id}: {exc}")
        self.db.connection.execute(
            """
            UPDATE interaction_threads SET tracking_status = 'frozen',
                frozen_at = CASE WHEN frozen_at = '' THEN ? ELSE frozen_at END
            WHERE video_id IN (SELECT video_id FROM videos WHERE monitor_status != 'active')
            """,
            (now_iso(),),
        )
        self.db.connection.commit()
        self.export_dashboard()
        self.console.line(f"历史互动补全结束：成功 {refreshed}/{len(rows)}")

    def poll_video(self, video):
        interval = monitor_interval_seconds(video["published_timestamp"])
        if interval is None:
            self.db.freeze_video(video["video_id"], "completed", "48h completed")
            self.export_dashboard()
            self.console.line(f"视频已结束 48 小时周期：{self._label(video)}")
            return

        try:
            try:
                _, aweme = self._detail(video["video_id"], video["url"])
            except MonitorFailure as exc:
                if exc.kind != "missing":
                    raise
                aweme = None
            if not aweme:
                if self.profile_healthy and video["video_id"] not in self.last_profile_ids:
                    misses = self.db.increment_delete_miss(video["video_id"])
                    if misses >= 3:
                        self.db.freeze_video(video["video_id"], "deleted", "profile and detail missing")
                        self.export_dashboard()
                        self.console.line(f"视频已确认删除：{self._label(video)}", "warning")
                        return
                    self.console.line(f"视频删除待确认：{self._label(video)}（{misses}/3）", "warning")
                self.db.schedule_poll(video["video_id"], (datetime.now() + timedelta(minutes=3)).isoformat(timespec="seconds"))
                return

            details = parse_video_detail(aweme, dict(video))
            media = media_url(aweme)
            if media:
                self.db.set_media_url(video["video_id"], media)
            changed = self.db.update_video_snapshot(video["video_id"], details)
            fresh_video = self.db.get_video(video["video_id"])
            comments_available = True
            comment_failure = ""
            reply_failures = 0
            reply_api_unavailable = False
            try:
                raw_comments = self._top_comments(
                    video["video_id"], video["url"], INTERACTION_DISCOVERY_LIMIT
                )
                if not raw_comments and int(details.get("comment_count") or 0) > 0:
                    raise MonitorFailure("verification", "评论数大于零但评论接口返回空列表")
                discovery_events = self._discover_interactions(fresh_video, raw_comments)
                snapshot_comments = raw_comments[:COMMENT_SNAPSHOT_LIMIT]
                (
                    top_threads,
                    reply_count,
                    refreshed_reply_ids,
                    reply_failures,
                    reply_api_unavailable,
                ) = self._build_top_snapshot(fresh_video, snapshot_comments)
                comments_changed = self.db.update_comments(
                    video["video_id"], top_threads, reply_count
                )
            except MonitorFailure as exc:
                if is_global_api_failure(exc):
                    raise
                comments_available = False
                comment_failure = str(exc)
                discovery_events = 0
                top_threads = json_value(fresh_video["comments_json"], [])
                refreshed_reply_ids = set()
                comments_changed = False
                self.console.detail(
                    f"前100条互动扫描暂不可用 video={video['video_id']}: {exc}"
                )
            new_events = discovery_events + self._track_interactions(
                fresh_video,
                top_threads,
                refreshed_reply_ids,
                skip_direct_refresh=reply_api_unavailable,
            )
            next_poll = datetime.now() + timedelta(seconds=interval)
            self.db.schedule_poll(video["video_id"], next_poll.isoformat(timespec="seconds"))
            if changed or comments_changed or new_events:
                self.export_dashboard()
            if comments_available:
                reply_note = f"｜{reply_failures} 条回复线程待重试" if reply_failures else ""
                self.console.line(
                    f"视频读取成功：{self._label(fresh_video)}｜赞 {details['like_count']}｜"
                    f"评 {details['comment_count']}｜展示前 {len(top_threads)} 条｜"
                    f"互动扫描前 {len(raw_comments)} 条｜新增互动 {new_events}"
                    f"{reply_note}"
                )
            else:
                self.console.line(
                    f"视频统计读取成功，评论保留上次快照：{self._label(fresh_video)}｜"
                    f"赞 {details['like_count']}｜评 {details['comment_count']}｜{comment_failure}",
                    "warning",
                )
        except MonitorFailure:
            raise
        except Exception as exc:
            self.console.detail(f"视频读取异常 {video['video_id']}", exc_info=True)
            raise MonitorFailure("data", str(exc)) from exc

    def _download_transcription_media(self, video):
        media = str(video["media_url"] or "")
        if not media:
            try:
                _, aweme = self._detail(video["video_id"], video["url"])
                if aweme:
                    media = media_url(aweme)
                    self.db.set_media_url(video["video_id"], media)
            except MonitorFailure:
                raise
        if not media:
            raise MonitorFailure("data", "详情接口没有返回视频地址")
        try:
            response = self.context.request.get(
                media, headers={"referer": video["url"]}, timeout=self.args.media_timeout_ms
            )
            body = response.body()
        except Exception as exc:
            raise MonitorFailure("network", str(exc)) from exc
        if response.status != 200 or not body:
            raise MonitorFailure("data", f"视频下载失败 HTTP {response.status}")
        TRANSCRIPTION_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        path = TRANSCRIPTION_QUEUE_DIR / f"{video['video_id']}.mp4"
        path.write_bytes(body)
        return path

    def _queue_transcriptions(self):
        for video in self.db.due_transcriptions():
            video_id = video["video_id"]
            if video_id in self.transcription_futures:
                continue
            try:
                media_path = self._download_transcription_media(video)
                self.db.set_transcript_processing(video_id)
                self.transcription_futures[video_id] = self.transcription_pool.submit(
                    self.transcriber.transcribe_file, media_path
                )
                self.console.line(f"视频开始转录：{self._label(video)}")
            except MonitorFailure as exc:
                retry = datetime.now() + timedelta(minutes=10)
                self.db.set_transcript_error(video_id, str(exc), retry.isoformat(timespec="seconds"))
                self.console.line(f"视频转录准备失败：{self._label(video)}", "warning")
                self.console.detail(f"转录准备失败 {video_id}: {exc}")

    def _collect_transcription_results(self):
        completed = [
            video_id for video_id, future in self.transcription_futures.items()
            if future.done()
        ]
        for video_id in completed:
            future = self.transcription_futures.pop(video_id)
            video = self.db.get_video(video_id)
            try:
                transcript = future.result()
                self.db.set_transcript_complete(video_id, transcript)
                self.export_dashboard()
                self.console.line(
                    f"视频已转录：{self._label(video)}｜{len(transcript['text'])} 字"
                )
            except Exception as exc:
                retry = datetime.now() + timedelta(minutes=10)
                self.db.set_transcript_error(video_id, str(exc), retry.isoformat(timespec="seconds"))
                self.console.line(f"视频转录失败：{self._label(video)}", "warning")
                self.console.detail(f"转录失败 {video_id}", exc_info=True)

    def _handle_global_failure(self, failure):
        if failure.kind == "verification":
            self.pause_until = datetime.now() + timedelta(minutes=10)
            self.console.line("检测到抖音验证码或风控，暂停 10 分钟", "warning")
        elif failure.kind == "auth":
            self.pause_until = datetime.now() + timedelta(minutes=10)
            self.console.line("登录状态已失效，暂停采集并等待重新登录", "warning")
        elif failure.kind == "network":
            self.pause_until = datetime.now() + timedelta(minutes=5)
            self.console.line("网络连接异常，5 分钟后重试", "warning")
        else:
            self.pause_until = datetime.now() + timedelta(minutes=3)
            self.console.line(f"抖音数据暂时不可用，3 分钟后重试：{failure}", "warning")
        self.console.detail(f"全局采集暂停 kind={failure.kind}: {failure}")

    def _finish_expired(self):
        for video in self.db.active_videos():
            if monitor_interval_seconds(video["published_timestamp"]) is None:
                self.db.freeze_video(video["video_id"], "completed", "48h completed")
                self.export_dashboard()
                self.console.line(f"视频已结束 48 小时周期：{self._label(video)}")

    def _poll_due_videos(self):
        for video in self.db.due_videos():
            try:
                self.poll_video(video)
            except MonitorFailure as failure:
                if is_global_monitor_failure(failure):
                    raise
                retry_at = datetime.now() + timedelta(minutes=3)
                self.db.schedule_poll(
                    video["video_id"], retry_at.isoformat(timespec="seconds")
                )
                self.console.line(
                    f"视频局部读取异常：{self._label(video)}｜3 分钟后单独重试，"
                    "继续处理其他视频",
                    "warning",
                )
                self.console.detail(
                    f"视频局部异常 kind={failure.kind} video={video['video_id']}: {failure}"
                )

    def run_once(self):
        self.scan_profile()
        self._finish_expired()
        self._poll_due_videos()
        self._queue_transcriptions()
        self._collect_transcription_results()

    def run_forever(self):
        self.console.line("模型先生监控已启动")
        while True:
            now = datetime.now()
            self._collect_transcription_results()

            if self.pause_until and now < self.pause_until:
                time.sleep(min(30, max(1, int((self.pause_until - now).total_seconds()))))
                continue
            self.pause_until = None

            try:
                if now >= self.profile_next_at:
                    self.scan_profile()
                    self.profile_next_at = datetime.now() + timedelta(seconds=self.args.profile_interval)
                self._finish_expired()
                self._poll_due_videos()
                self._queue_transcriptions()
            except MonitorFailure as failure:
                self._handle_global_failure(failure)

            if datetime.now() >= self.git_next_at:
                if not self.args.no_git:
                    try:
                        self.publisher.sync()
                    except Exception:
                        self.console.line("GitHub 同步失败，详细原因已写入日志", "warning")
                        self.console.detail("GitHub 同步失败", exc_info=True)
                self.git_next_at = datetime.now() + timedelta(seconds=self.args.git_interval)

            time.sleep(10)


def parse_args():
    parser = argparse.ArgumentParser(description="模型先生新视频 48 小时监控服务")
    parser.add_argument("--database", default=str(DATABASE_PATH))
    parser.add_argument("--log-path", default=str(LOG_PATH))
    parser.add_argument("--profile-interval", type=int, default=5 * 60)
    parser.add_argument("--git-interval", type=int, default=3 * 60 * 60)
    parser.add_argument("--profile-wait-ms", type=int, default=2500)
    parser.add_argument("--page-timeout-ms", type=int, default=60_000)
    parser.add_argument("--request-timeout-ms", type=int, default=15_000)
    parser.add_argument("--media-timeout-ms", type=int, default=120_000)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-git", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--refresh-history", action="store_true")
    return parser.parse_args()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    lock = SingleInstanceLock(LOCK_PATH)
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(str(exc))
        return 2

    monitor = MrModelMonitor(args)
    try:
        monitor.bootstrap()
        if args.init_only:
            return 0
        monitor.open_browser()
        if args.refresh_history:
            monitor.refresh_historical_interactions()
            return 0
        if args.once:
            monitor.run_once()
            return 0
        monitor.run_forever()
    except KeyboardInterrupt:
        monitor.console.line("监控已停止")
        return 0
    finally:
        monitor.close()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
