import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DEBUG_DIR = ROOT_DIR / "debug"

AUTHOR_SCRIPT = ROOT_DIR / "douyin_author_videos.py"
ENRICH_SCRIPT = ROOT_DIR / "douyin_enrich_details.py"

AUTHOR_LATEST_JSON = DATA_DIR / "author_videos_latest.json"
ENRICHED_LATEST_JSON = DATA_DIR / "author_videos_enriched_latest.json"

MONITOR_STATE_JSON = DATA_DIR / "monitor_state.json"
MONITOR_LATEST_JSON = DATA_DIR / "monitor_latest.json"

NEW_VIDEO_EVENTS_TSV = DATA_DIR / "monitor_new_video_events.tsv"
TRACKED_STATS_HISTORY_TSV = DATA_DIR / "monitor_tracked_video_stats_history.tsv"
TRACKED_STATS_LATEST_TSV = DATA_DIR / "monitor_tracked_video_stats_latest.tsv"


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def now_str():
    return datetime.now().isoformat(timespec="seconds")


def safe_load_json(path: Path, default=None):
    if default is None:
        default = {}

    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_video_id(video):
    return str(video.get("video_id", "")).strip()


def load_videos_from_author_latest():
    payload = safe_load_json(AUTHOR_LATEST_JSON, default={})
    videos = payload.get("videos", [])
    return payload, videos


def load_videos_from_enriched_latest():
    payload = safe_load_json(ENRICHED_LATEST_JSON, default={})
    videos = payload.get("videos", [])
    return payload, videos


def init_empty_state():
    return {
        "created_at": now_str(),
        "updated_at": now_str(),
        "known_video_ids": [],
        "known_videos": {},
        "tracked_video_ids": [],
        "tracked_videos": {},
    }


def load_state():
    state = safe_load_json(MONITOR_STATE_JSON, default=None)

    if not state:
        return init_empty_state()

    state.setdefault("known_video_ids", [])
    state.setdefault("known_videos", {})
    state.setdefault("tracked_video_ids", [])
    state.setdefault("tracked_videos", {})

    return state


def save_state(state):
    state["updated_at"] = now_str()
    save_json(MONITOR_STATE_JSON, state)


def append_tsv(path: Path, fieldnames, rows):
    if not rows:
        return

    file_exists = path.exists()

    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")

        if not file_exists:
            writer.writeheader()

        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def overwrite_tsv(path: Path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()

        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def is_timestamp_archive(path: Path):
    return re.search(r"_\d{8}_\d{6}\.(json|csv)$", path.name) is not None


def snapshot_tool_archives():
    patterns = [
        "author_videos_*.json",
        "author_videos_*.csv",
        "author_videos_enriched_*.json",
        "author_videos_enriched_*.csv",
    ]

    files = set()

    for pattern in patterns:
        for p in DATA_DIR.glob(pattern):
            if is_timestamp_archive(p):
                files.add(p)

    return files


def cleanup_created_archives(before_files, label):
    after_files = snapshot_tool_archives()
    created = sorted(after_files - before_files)

    for p in created:
        try:
            p.unlink()
            print(f"  已清理 {label} 临时归档：{p.name}")
        except Exception as e:
            print(f"  清理失败 {p}: {e}")


def run_command(cmd, timeout_seconds=None):
    result = subprocess.run(
        cmd,
        cwd=str(ROOT_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
    )

    return result.returncode, result.stdout, result.stderr


def run_author_scan(scan_wait_ms, cleanup_archives=True):
    before = snapshot_tool_archives()

    cmd = [
        sys.executable,
        str(AUTHOR_SCRIPT),
        "--scan",
        "--wait-ms",
        str(scan_wait_ms),
    ]

    print(f"\n[{now_str()}] 调用主页扫描脚本：douyin_author_videos.py --scan")
    code, stdout, stderr = run_command(cmd)

    if stdout.strip():
        print(stdout.strip())

    if stderr.strip():
        print("[stderr]")
        print(stderr.strip())

    if cleanup_archives:
        cleanup_created_archives(before, "主页扫描")

    if code != 0:
        raise RuntimeError(f"douyin_author_videos.py 执行失败，returncode={code}")


def run_enrich_details(limit, enrich_wait_ms, cleanup_archives=True, debug=False):
    before = snapshot_tool_archives()

    cmd = [
        sys.executable,
        str(ENRICH_SCRIPT),
        "--limit",
        str(limit),
        "--wait-ms",
        str(enrich_wait_ms),
    ]

    if debug:
        cmd.append("--debug")

    print(f"\n[{now_str()}] 调用详情增强脚本：douyin_enrich_details.py --limit {limit}")

    code, stdout, stderr = run_command(cmd)

    if stdout.strip():
        print(stdout.strip())

    if stderr.strip():
        print("[stderr]")
        print(stderr.strip())

    if cleanup_archives:
        cleanup_created_archives(before, "详情增强")

    if code != 0:
        raise RuntimeError(f"douyin_enrich_details.py 执行失败，returncode={code}")


def build_known_from_previous_snapshot(previous_videos):
    known_ids = []
    known_videos = {}

    for v in previous_videos:
        vid = get_video_id(v)
        if not vid:
            continue

        known_ids.append(vid)
        known_videos[vid] = {
            "video_id": vid,
            "href": v.get("href", ""),
            "title_hint": v.get("title_hint", ""),
            "first_seen_at": now_str(),
            "source": "previous_author_videos_latest",
        }

    return known_ids, known_videos


def detect_new_videos(current_videos, known_ids):
    known = set(known_ids)
    new_videos = []

    for v in current_videos:
        vid = get_video_id(v)

        if not vid:
            continue

        if vid not in known:
            new_videos.append(v)

    return new_videos


def register_new_videos(state, new_videos):
    detected_at = now_str()

    event_rows = []

    known_ids = set(state.get("known_video_ids", []))
    tracked_ids = set(state.get("tracked_video_ids", []))

    for v in new_videos:
        vid = get_video_id(v)

        if not vid:
            continue

        known_ids.add(vid)
        tracked_ids.add(vid)

        state["known_videos"][vid] = {
            "video_id": vid,
            "href": v.get("href", ""),
            "title_hint": v.get("title_hint", ""),
            "raw_text": v.get("raw_text", ""),
            "like_hint": v.get("like_hint", ""),
            "is_pinned": v.get("is_pinned", ""),
            "first_seen_at": detected_at,
            "source": "monitor_detected",
        }

        state["tracked_videos"][vid] = {
            "video_id": vid,
            "href": v.get("href", ""),
            "title_hint": v.get("title_hint", ""),
            "detected_at": detected_at,
            "active": True,
            "last_stats_at": "",
            "last_like_count": "",
            "last_comment_count": "",
        }

        event_rows.append(
            {
                "detected_at": detected_at,
                "event_type": "new_video",
                "video_id": vid,
                "order_index": v.get("order_index", ""),
                "title_hint": v.get("title_hint", ""),
                "home_like_raw": v.get("like_hint", ""),
                "is_pinned": v.get("is_pinned", ""),
                "href": v.get("href", ""),
                "raw_text": v.get("raw_text", ""),
            }
        )

    state["known_video_ids"] = list(known_ids)
    state["tracked_video_ids"] = list(tracked_ids)

    fieldnames = [
        "detected_at",
        "event_type",
        "video_id",
        "order_index",
        "title_hint",
        "home_like_raw",
        "is_pinned",
        "href",
        "raw_text",
    ]

    append_tsv(NEW_VIDEO_EVENTS_TSV, fieldnames, event_rows)

    return event_rows


def parse_dt(s):
    if not s:
        return None

    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def get_active_tracked_ids(state, track_hours):
    ids = []

    now_dt = datetime.now()

    for vid in state.get("tracked_video_ids", []):
        info = state.get("tracked_videos", {}).get(vid, {})

        if not info.get("active", True):
            continue

        detected_at = parse_dt(info.get("detected_at", ""))

        if track_hours > 0 and detected_at:
            if now_dt - detected_at > timedelta(hours=track_hours):
                info["active"] = False
                continue

        ids.append(vid)

    return ids


def compute_enrich_limit(current_videos, active_ids, min_limit):
    if not current_videos:
        return 0

    positions = {}

    for idx, v in enumerate(current_videos, 1):
        vid = get_video_id(v)
        if vid:
            positions[vid] = idx

    max_pos = 0

    for vid in active_ids:
        if vid in positions:
            max_pos = max(max_pos, positions[vid])

    if max_pos <= 0:
        max_pos = min_limit

    return min(len(current_videos), max(min_limit, max_pos))


def build_stats_rows_from_enriched(active_ids):
    _, enriched_videos = load_videos_from_enriched_latest()

    active = set(active_ids)
    checked_at = now_str()
    rows = []

    for v in enriched_videos:
        vid = get_video_id(v)

        if vid not in active:
            continue

        rows.append(
            {
                "checked_at": checked_at,
                "video_id": vid,
                "order_index": v.get("order_index", ""),
                "title_hint": v.get("title_hint", ""),
                "published_at": v.get("published_at", ""),
                "published_timestamp": v.get("published_timestamp", ""),
                "home_like_raw": v.get("home_like_raw", ""),
                "home_like_count": v.get("home_like_count", ""),
                "detail_like_raw": v.get("detail_like_raw", ""),
                "detail_like_count": v.get("detail_like_count", ""),
                "final_like_count": v.get("final_like_count", ""),
                "comment_raw": v.get("comment_raw", ""),
                "comment_count": v.get("comment_count", ""),
                "href": v.get("href", ""),
                "detail_final_url": v.get("detail_final_url", ""),
                "detail_method": v.get("detail_method", ""),
                "detail_source": v.get("detail_source", ""),
                "detail_error": v.get("detail_error", ""),
            }
        )

    return rows


def update_state_with_stats(state, stats_rows):
    for row in stats_rows:
        vid = row.get("video_id")

        if not vid:
            continue

        info = state.setdefault("tracked_videos", {}).setdefault(vid, {})
        info["last_stats_at"] = row.get("checked_at", "")
        info["last_like_count"] = row.get("final_like_count", "")
        info["last_comment_count"] = row.get("comment_count", "")
        info["last_published_at"] = row.get("published_at", "")
        info["last_detail_error"] = row.get("detail_error", "")


def write_stats_files(stats_rows):
    if not stats_rows:
        return

    fieldnames = [
        "checked_at",
        "video_id",
        "order_index",
        "title_hint",
        "published_at",
        "published_timestamp",
        "home_like_raw",
        "home_like_count",
        "detail_like_raw",
        "detail_like_count",
        "final_like_count",
        "comment_raw",
        "comment_count",
        "href",
        "detail_final_url",
        "detail_method",
        "detail_source",
        "detail_error",
    ]

    append_tsv(TRACKED_STATS_HISTORY_TSV, fieldnames, stats_rows)
    overwrite_tsv(TRACKED_STATS_LATEST_TSV, fieldnames, stats_rows)


def write_monitor_latest(current_videos, new_videos, active_ids, stats_rows):
    payload = {
        "updated_at": now_str(),
        "current_home_video_count": len(current_videos),
        "current_home_videos": current_videos,
        "new_video_count_this_poll": len(new_videos),
        "new_videos_this_poll": new_videos,
        "active_tracked_video_ids": active_ids,
        "tracked_stats_this_poll": stats_rows,
    }

    save_json(MONITOR_LATEST_JSON, payload)


def sleep_with_countdown(seconds):
    if seconds <= 0:
        return

    print(f"\n下一次轮询将在 {seconds} 秒后开始。按 Ctrl+C 停止。")

    remaining = seconds

    while remaining > 0:
        step = min(30, remaining)
        time.sleep(step)
        remaining -= step

        if remaining > 0:
            print(f"  剩余 {remaining} 秒...")


def poll_once(
    iteration,
    scan_wait_ms,
    enrich_wait_ms,
    min_enrich_limit,
    track_hours,
    treat_first_run_as_new,
    cleanup_tool_archives,
    enrich_debug,
):
    print("\n" + "=" * 90)
    print(f"[{now_str()}] 第 {iteration} 次轮询开始")

    previous_payload, previous_videos = load_videos_from_author_latest()

    state_exists = MONITOR_STATE_JSON.exists()
    state = load_state()

    if not state_exists:
        previous_ids, previous_known = build_known_from_previous_snapshot(previous_videos)

        if previous_ids and not treat_first_run_as_new:
            state["known_video_ids"] = previous_ids
            state["known_videos"] = previous_known
            print(f"  初始化监控状态：使用已有 author_videos_latest.json 作为基线，基线视频数 {len(previous_ids)}")

    run_author_scan(
        scan_wait_ms=scan_wait_ms,
        cleanup_archives=cleanup_tool_archives,
    )

    current_payload, current_videos = load_videos_from_author_latest()

    current_ids = [get_video_id(v) for v in current_videos if get_video_id(v)]

    print(f"\n[{now_str()}] 本次主页首屏视频数：{len(current_videos)}")
    print(f"当前首屏 video_id：{current_ids}")

    if not state_exists and not previous_videos and not treat_first_run_as_new:
        print("  第一次运行且没有旧 author_videos_latest.json，当前列表只作为基线，不报警。")

        for v in current_videos:
            vid = get_video_id(v)
            if not vid:
                continue

            state["known_video_ids"].append(vid)
            state["known_videos"][vid] = {
                "video_id": vid,
                "href": v.get("href", ""),
                "title_hint": v.get("title_hint", ""),
                "first_seen_at": now_str(),
                "source": "first_run_baseline",
            }

        save_state(state)
        write_monitor_latest(current_videos, [], [], [])
        return

    new_videos = detect_new_videos(current_videos, state.get("known_video_ids", []))

    event_rows = []

    if new_videos:
        print(f"\n发现新视频：{len(new_videos)} 条")

        for v in new_videos:
            print(f"  NEW video_id={get_video_id(v)} | {v.get('title_hint', '')} | {v.get('href', '')}")

        event_rows = register_new_videos(state, new_videos)
        save_state(state)

        print(f"  已更新新视频事件 TSV：{NEW_VIDEO_EVENTS_TSV}")
    else:
        print("\n没有发现新视频。")

    active_ids = get_active_tracked_ids(state, track_hours=track_hours)

    stats_rows = []

    if active_ids:
        print(f"\n当前需要追踪点赞/评论数的视频：{active_ids}")

        enrich_limit = compute_enrich_limit(
            current_videos=current_videos,
            active_ids=active_ids,
            min_limit=min_enrich_limit,
        )

        if enrich_limit <= 0:
            print("  当前主页列表为空，跳过详情增强。")
        else:
            run_enrich_details(
                limit=enrich_limit,
                enrich_wait_ms=enrich_wait_ms,
                cleanup_archives=cleanup_tool_archives,
                debug=enrich_debug,
            )

            stats_rows = build_stats_rows_from_enriched(active_ids)

            if stats_rows:
                write_stats_files(stats_rows)
                update_state_with_stats(state, stats_rows)
                save_state(state)

                print(f"\n已更新追踪视频统计：")
                for row in stats_rows:
                    print(
                        f"  video_id={row.get('video_id')} | "
                        f"点赞={row.get('final_like_count')} | "
                        f"评论={row.get('comment_count')} | "
                        f"发布时间={row.get('published_at')}"
                    )

                print(f"  最新统计 TSV：{TRACKED_STATS_LATEST_TSV}")
                print(f"  历史统计 TSV：{TRACKED_STATS_HISTORY_TSV}")
            else:
                print("  详情增强完成，但没有在 enriched 结果里匹配到正在追踪的视频。")
    else:
        print("\n当前没有需要追踪点赞/评论数的新视频。")

    if new_videos or stats_rows:
        write_monitor_latest(
            current_videos=current_videos,
            new_videos=new_videos,
            active_ids=active_ids,
            stats_rows=stats_rows,
        )
        print(f"\n已更新监控最新状态：{MONITOR_LATEST_JSON}")
    else:
        print("\n本轮无新视频、无追踪统计更新；不写入 monitor_* 记录文件。")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="轮询间隔，默认 300 秒，也就是 5 分钟。",
    )

    parser.add_argument(
        "--scan-wait-ms",
        type=int,
        default=3000,
        help="调用 douyin_author_videos.py 时页面等待时间，默认 3000ms。",
    )

    parser.add_argument(
        "--enrich-wait-ms",
        type=int,
        default=8000,
        help="调用 douyin_enrich_details.py 时详情页等待时间，默认 8000ms。",
    )

    parser.add_argument(
        "--min-enrich-limit",
        type=int,
        default=5,
        help="详情增强至少处理主页前多少条视频，默认 5。",
    )

    parser.add_argument(
        "--track-hours",
        type=float,
        default=24,
        help="新视频发现后持续追踪多少小时，默认 24 小时。传 0 表示一直追踪。",
    )

    parser.add_argument(
        "--treat-first-run-as-new",
        action="store_true",
        help="第一次运行时，把当前主页列表也当作新视频处理。默认不这样做，而是作为基线。",
    )

    parser.add_argument(
        "--keep-tool-archives",
        action="store_true",
        help="保留 douyin_author_videos.py / douyin_enrich_details.py 自动生成的时间戳归档文件。默认自动清理。",
    )

    parser.add_argument(
        "--enrich-debug",
        action="store_true",
        help="调用 douyin_enrich_details.py 时加 --debug。",
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一轮，方便测试。",
    )

    args = parser.parse_args()

    ensure_dirs()

    print("监控脚本启动")
    print(f"项目目录：{ROOT_DIR}")
    print(f"轮询间隔：{args.interval_seconds} 秒")
    print(f"主页脚本：{AUTHOR_SCRIPT}")
    print(f"详情脚本：{ENRICH_SCRIPT}")
    print(f"状态文件：{MONITOR_STATE_JSON}")
    print(f"新视频事件 TSV：{NEW_VIDEO_EVENTS_TSV}")
    print(f"追踪统计 TSV：{TRACKED_STATS_HISTORY_TSV}")

    iteration = 1

    while True:
        try:
            poll_once(
                iteration=iteration,
                scan_wait_ms=args.scan_wait_ms,
                enrich_wait_ms=args.enrich_wait_ms,
                min_enrich_limit=args.min_enrich_limit,
                track_hours=args.track_hours,
                treat_first_run_as_new=args.treat_first_run_as_new,
                cleanup_tool_archives=not args.keep_tool_archives,
                enrich_debug=args.enrich_debug,
            )
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，监控停止。")
            break
        except Exception as e:
            print(f"\n[{now_str()}] 本轮轮询出错：{repr(e)}")
            print("程序不会退出，等待下一轮继续。")

        if args.once:
            print("\n--once 模式，执行一轮后退出。")
            break

        iteration += 1

        try:
            sleep_with_countdown(args.interval_seconds)
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，监控停止。")
            break


if __name__ == "__main__":
    main()