import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = ROOT_DIR / "runtime" / "douyin_profile"
DATA_DIR = ROOT_DIR / "data"
DEBUG_DIR = ROOT_DIR / "debug"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def format_time(value):
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def load_video(video_index=0, video_url=""):
    if video_url:
        return {
            "video_id": urlparse(video_url).path.rstrip("/").split("/")[-1],
            "href": video_url,
        }

    input_path = DATA_DIR / "author_videos_latest.json"
    if not input_path.exists():
        raise FileNotFoundError(
            f"找不到 {input_path}，请先运行 douyin_author_videos.py --scan。"
        )

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    videos = payload.get("videos") or []
    if not videos:
        raise RuntimeError("author_videos_latest.json 中没有视频。")
    if video_index < 0 or video_index >= len(videos):
        raise IndexError(f"video-index 超出范围，目前共有 {len(videos)} 条视频。")
    return videos[video_index]


def is_top_comment_list_url(url):
    lower = url.lower()
    if "comment" not in lower:
        return False
    if "reply" in lower:
        return False
    return "comment/list" in lower or "comment_list" in lower


def find_comment_list(obj):
    """找评论接口的主 comments 数组，避免把每条评论里的 replies 当成分页结果。"""
    if not isinstance(obj, (dict, list)):
        return []

    queue = [obj]
    candidates = []
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for key, value in cur.items():
                if key.lower() in {"comments", "comment_list", "commentlist"} and isinstance(value, list):
                    candidates.append(value)
                elif isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(cur, list):
            queue.extend(x for x in cur if isinstance(x, (dict, list)))

    if not candidates:
        return []
    return max(candidates, key=len)


def first_value(d, *keys):
    if not isinstance(d, dict):
        return None
    lowered = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def normalize_comments(rows):
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = first_value(row, "user", "user_info") or {}
        create_time = first_value(row, "create_time", "createtime", "create_at")
        try:
            create_time = int(create_time)
        except Exception:
            create_time = None
        result.append(
            {
                "comment_id": str(first_value(row, "cid", "comment_id", "id") or ""),
                "create_time": create_time,
                "create_time_display": format_time(create_time),
                "nickname": str(first_value(user, "nickname", "name") or ""),
                "text": str(first_value(row, "text", "content") or "")[:300],
                "digg_count": first_value(row, "digg_count", "like_count"),
                "reply_comment_total": first_value(
                    row, "reply_comment_total", "reply_total", "reply_count"
                ),
            }
        )
    return result


def find_pagination(obj):
    if not isinstance(obj, dict):
        return {}
    wanted = {
        "cursor",
        "has_more",
        "hasmore",
        "total",
        "count",
        "next_cursor",
        "nextcursor",
    }
    found = {}
    queue = [obj]
    while queue and len(found) < len(wanted):
        cur = queue.pop(0)
        if not isinstance(cur, dict):
            continue
        for key, value in cur.items():
            normalized = key.lower()
            if normalized in wanted and normalized not in found and not isinstance(value, (dict, list)):
                found[normalized] = value
            elif isinstance(value, dict):
                queue.append(value)
    return found


def analyze_order(comments):
    timed = [x for x in comments if isinstance(x.get("create_time"), int)]
    times = [x["create_time"] for x in timed]
    desc_breaks = []
    asc_breaks = []
    for index, (left, right) in enumerate(zip(times, times[1:]), start=1):
        if left < right:
            desc_breaks.append(index)
        if left > right:
            asc_breaks.append(index)

    if len(times) < 2:
        verdict = "insufficient_timestamps"
    elif not desc_breaks:
        verdict = "time_descending"
    elif not asc_breaks:
        verdict = "time_ascending"
    else:
        verdict = "mixed_not_time_sorted"

    return {
        "comment_count": len(comments),
        "timestamp_count": len(times),
        "verdict": verdict,
        "descending_break_count": len(desc_breaks),
        "descending_break_positions": desc_breaks[:20],
        "newest_time": format_time(max(times)) if times else "",
        "oldest_time": format_time(min(times)) if times else "",
    }


def query_summary(url):
    query = parse_qs(urlparse(url).query)
    interesting = {}
    for key, values in query.items():
        lowered = key.lower()
        if any(token in lowered for token in ("cursor", "count", "sort", "order", "aweme", "item_type")):
            interesting[key] = values[0] if len(values) == 1 else values
    return interesting


def visible_sort_controls(page):
    labels = ["最新评论", "按时间", "时间排序", "最新", "最热评论", "按热度"]
    result = []
    for label in labels:
        locator = page.get_by_text(label, exact=True)
        try:
            count = min(locator.count(), 10)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    result.append({"label": label, "index": index, "tag": item.evaluate("e => e.tagName")})
            except Exception:
                pass
    return result


def try_click_latest(page):
    # 有些页面先显示“最热评论”，点击后才出现“最新评论”。
    for opener in ("最热评论", "按热度"):
        locator = page.get_by_text(opener, exact=True)
        try:
            for index in range(min(locator.count(), 5)):
                item = locator.nth(index)
                if item.is_visible():
                    item.click(timeout=3000)
                    page.wait_for_timeout(500)
                    break
        except Exception:
            pass

    for label in ("最新评论", "按时间", "时间排序", "最新"):
        locator = page.get_by_text(label, exact=True)
        try:
            for index in range(min(locator.count(), 10)):
                item = locator.nth(index)
                if item.is_visible():
                    item.click(timeout=3000)
                    return {"clicked": True, "label": label, "index": index}
        except Exception:
            continue
    return {"clicked": False, "label": "", "index": None}


def limited_scroll(page, rounds, wait_ms):
    for _ in range(rounds):
        # 视频详情页的评论通常位于右半区域；只做少量滚动，避免读取全部评论。
        page.mouse.move(1120, 760)
        page.mouse.wheel(0, 1100)
        page.wait_for_timeout(wait_ms)


def aggregate_phase_order(captures, phase):
    rows = []
    seen = set()
    for capture in captures:
        if capture.get("phase") != phase:
            continue
        for comment in capture.get("comments", []):
            key = comment.get("comment_id") or (
                comment.get("create_time"), comment.get("nickname"), comment.get("text")
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(comment)
    return {"comments": rows, "analysis": analyze_order(rows)}


def run(args):
    video = load_video(args.video_index, args.video_url)
    href = video.get("href") or ""
    video_id = str(video.get("video_id") or "")
    if not href:
        raise RuntimeError("视频记录没有 href。")

    captures = []
    phase = {"name": "natural"}

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()

        def on_response(response):
            try:
                if response.request.resource_type not in {"xhr", "fetch"}:
                    return
                if not is_top_comment_list_url(response.url):
                    return
                payload = response.json()
                comments = normalize_comments(find_comment_list(payload))
                captures.append(
                    {
                        "captured_at": datetime.now().isoformat(timespec="seconds"),
                        "phase": phase["name"],
                        "url": response.url,
                        "status": response.status,
                        "request_query": query_summary(response.url),
                        "pagination": find_pagination(payload),
                        "comments": comments,
                        "order_analysis": analyze_order(comments),
                    }
                )
                analysis = captures[-1]["order_analysis"]
                print(
                    f"捕获评论页：phase={phase['name']} count={len(comments)} "
                    f"order={analysis['verdict']} cursor={captures[-1]['request_query'].get('cursor', '')}"
                )
            except Exception as exc:
                print(f"解析评论响应失败：{type(exc).__name__}: {exc}")

        page.on("response", on_response)
        print(f"打开视频：{video_id}\n{href}")
        try:
            page.goto(href, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            print("页面导航超时，继续检查已经加载的内容。")
        page.wait_for_timeout(args.wait_ms)

        controls_before = visible_sort_controls(page)
        limited_scroll(page, args.scroll_rounds, args.scroll_wait_ms)

        click_result = {"clicked": False, "label": "", "index": None}
        controls_after_open = []
        if args.try_latest:
            phase["name"] = "latest_ui"
            click_result = try_click_latest(page)
            controls_after_open = visible_sort_controls(page)
            if click_result["clicked"]:
                print(f"已点击时间排序入口：{click_result['label']}")
                page.wait_for_timeout(args.wait_ms)
                limited_scroll(page, args.scroll_rounds, args.scroll_wait_ms)
            else:
                print("页面上没有找到可点击的“最新评论/按时间”入口。")

        natural = aggregate_phase_order(captures, "natural")
        latest_ui = aggregate_phase_order(captures, "latest_ui")
        if click_result["clicked"] and latest_ui["analysis"]["verdict"] == "time_descending":
            conclusion = "confirmed_latest_sort_via_ui"
        elif natural["analysis"]["verdict"] == "time_descending":
            conclusion = "natural_response_is_time_descending_but_no_sort_control_confirmed"
        elif captures:
            conclusion = "captured_comments_are_not_time_descending"
        else:
            conclusion = "no_comment_list_response_captured"

        report = {
            "tested_at": datetime.now().isoformat(timespec="seconds"),
            "video_id": video_id,
            "href": href,
            "final_url": page.url,
            "try_latest": args.try_latest,
            "sort_controls_before": controls_before,
            "sort_controls_after_open": controls_after_open,
            "latest_click": click_result,
            "capture_count": len(captures),
            "natural": natural,
            "latest_ui": latest_ui,
            "conclusion": conclusion,
            "captures": captures,
        }

        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        latest_path = DEBUG_DIR / "comment_order_test_latest.json"
        archive_path = DEBUG_DIR / f"comment_order_test_{now}.json"
        content = json.dumps(report, ensure_ascii=False, indent=2)
        latest_path.write_text(content, encoding="utf-8")
        archive_path.write_text(content, encoding="utf-8")

        print("\n测试结论：", conclusion)
        print("自然加载：", natural["analysis"])
        print("切换最新：", latest_ui["analysis"])
        print("报告文件：", latest_path)

        if args.keep_open:
            input("按 Enter 关闭浏览器...")
        context.close()
        return report


def main():
    parser = argparse.ArgumentParser(description="测试抖音评论接口是否能按时间倒序读取。")
    parser.add_argument("--video-url", default="", help="指定视频 URL；默认读取主页结果中的视频。")
    parser.add_argument("--video-index", type=int, default=0, help="测试第几个视频，默认 0。")
    parser.add_argument("--wait-ms", type=int, default=8000, help="页面或排序切换后的等待时间。")
    parser.add_argument("--scroll-rounds", type=int, default=2, help="最多滚动加载几轮评论，默认 2。")
    parser.add_argument("--scroll-wait-ms", type=int, default=1500, help="每轮滚动后的等待时间。")
    parser.add_argument("--no-try-latest", dest="try_latest", action="store_false", help="不尝试点击最新评论。")
    parser.add_argument("--headless", action="store_true", help="无头运行；登录异常时不要使用。")
    parser.add_argument("--keep-open", action="store_true", help="测试结束后保持浏览器打开。")
    parser.set_defaults(try_latest=True)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
