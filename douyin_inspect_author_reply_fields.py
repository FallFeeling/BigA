import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from douyin_first_video_comments import launch_context


ROOT_DIR = Path(__file__).resolve().parent
DEBUG_DIR = ROOT_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_VIDEO_URL = "https://www.douyin.com/video/7659728886310452709"
TARGET_COMMENT_PHRASE = "我一直有一个疑问"
TARGET_REPLY_PHRASE = "三倍不是很出色"


def is_top_comment_url(url):
    lower = url.lower()
    return "comment/list" in lower and "reply" not in lower


def direct_comments(payload):
    if not isinstance(payload, dict):
        return []
    for key in ("comments", "comment_list", "commentList"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        return direct_comments(data)
    return []


def direct_value(row, *keys):
    if not isinstance(row, dict):
        return None
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def comment_text(row):
    return str(direct_value(row, "text", "content") or "")


def comment_id(row):
    return str(direct_value(row, "cid", "comment_id", "id") or "")


def user_summary(row):
    user = direct_value(row, "user", "user_info") or {}
    return {
        "nickname": str(direct_value(user, "nickname", "name") or ""),
        "uid": str(direct_value(user, "uid", "user_id") or ""),
        "sec_uid": str(direct_value(user, "sec_uid", "sec_user_id") or ""),
    }


def iter_dicts_with_paths(value, path="$", max_depth=12):
    stack = [(value, path, 0)]
    while stack:
        current, current_path, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(current, dict):
            yield current_path, current
            for key, child in current.items():
                if isinstance(child, (dict, list)):
                    stack.append((child, f"{current_path}.{key}", depth + 1))
        elif isinstance(current, list):
            for index, child in enumerate(current):
                if isinstance(child, (dict, list)):
                    stack.append((child, f"{current_path}[{index}]", depth + 1))


def json_safe_preview(value, max_text=1000):
    if isinstance(value, str):
        return value[:max_text]
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "preview": value[:3],
        }
    if isinstance(value, dict):
        return {
            "type": "dict",
            "keys": list(value.keys()),
        }
    return value


def interesting_fields(row):
    result = {}
    for key, value in row.items():
        lower = str(key).lower()
        if any(
            token in lower
            for token in (
                "reply",
                "author",
                "digg",
                "like",
                "label",
                "top",
                "relation",
                "highlight",
            )
        ):
            result[key] = json_safe_preview(value)
    return result


def summarize_comment(row):
    nested_text_rows = []
    for path, nested in iter_dicts_with_paths(row):
        text = comment_text(nested)
        if text and nested is not row:
            nested_text_rows.append(
                {
                    "path": path,
                    "comment_id": comment_id(nested),
                    "text": text,
                    "user": user_summary(nested),
                    "interesting_fields": interesting_fields(nested),
                    "raw": nested,
                }
            )
    return {
        "comment_id": comment_id(row),
        "text": comment_text(row),
        "user": user_summary(row),
        "reply_comment_total": direct_value(
            row, "reply_comment_total", "reply_total", "reply_count"
        ),
        "interesting_fields": interesting_fields(row),
        "nested_text_rows": nested_text_rows,
        "raw": row,
    }


def find_phrase(rows, phrase):
    matches = []
    for page_index, row_index, row in rows:
        if phrase in comment_text(row):
            matches.append(
                {
                    "page_index": page_index,
                    "row_index": row_index,
                    "path": "$",
                    "row": row,
                }
            )
        for path, nested in iter_dicts_with_paths(row):
            if nested is row:
                continue
            if phrase in comment_text(nested):
                matches.append(
                    {
                        "page_index": page_index,
                        "row_index": row_index,
                        "path": path,
                        "row": nested,
                        "parent_top_comment": row,
                    }
                )
    return matches


def run(args):
    captures = []

    with sync_playwright() as playwright:
        context = launch_context(playwright)
        page = context.new_page()

        def on_response(response):
            try:
                if response.request.resource_type not in {"xhr", "fetch"}:
                    return
                if not is_top_comment_url(response.url):
                    return
                text = response.text()
                if not text:
                    print(f"评论接口返回空正文：status={response.status}")
                    return
                payload = json.loads(text)
                rows = direct_comments(payload)
                query = parse_qs(urlparse(response.url).query)
                captures.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "request_cursor": (query.get("cursor") or [""])[0],
                        "response_cursor": payload.get("cursor"),
                        "has_more": payload.get("has_more"),
                        "comments": rows,
                    }
                )
                print(
                    f"捕获一级评论页：cursor={(query.get('cursor') or [''])[0]}，"
                    f"count={len(rows)}"
                )
            except Exception as exc:
                print(f"解析一级评论响应失败：{type(exc).__name__}: {exc}")

        page.on("response", on_response)
        print(f"打开视频：{args.video_url}")
        try:
            page.goto(args.video_url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            print("页面导航超时，继续检查已加载内容。")
        page.wait_for_timeout(args.wait_ms)

        for _ in range(args.scroll_rounds):
            all_rows = [
                (page_index, row_index, row)
                for page_index, capture in enumerate(captures)
                for row_index, row in enumerate(capture["comments"])
            ]
            if find_phrase(all_rows, args.target_reply_phrase):
                break
            page.mouse.move(650, 820)
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(args.scroll_wait_ms)

        all_rows = [
            (page_index, row_index, row)
            for page_index, capture in enumerate(captures)
            for row_index, row in enumerate(capture["comments"])
        ]
        target_comments = find_phrase(all_rows, args.target_comment_phrase)
        target_replies = find_phrase(all_rows, args.target_reply_phrase)

        parent_row = None
        if target_comments:
            parent_row = target_comments[0]["row"]
        elif target_replies:
            parent_row = target_replies[0].get("parent_top_comment")

        ordinary_candidates = []
        for _, _, row in all_rows:
            if row is parent_row:
                continue
            reply_total = direct_value(
                row, "reply_comment_total", "reply_total", "reply_count"
            )
            try:
                has_replies = int(reply_total or 0) > 0
            except Exception:
                has_replies = False
            if has_replies:
                ordinary_candidates.append(row)

        target_summary = summarize_comment(parent_row) if parent_row else None
        ordinary_summary = (
            summarize_comment(ordinary_candidates[0]) if ordinary_candidates else None
        )

        target_keys = set(parent_row.keys()) if isinstance(parent_row, dict) else set()
        ordinary_keys = (
            set(ordinary_candidates[0].keys()) if ordinary_candidates else set()
        )
        key_diff = {
            "only_target_parent": sorted(target_keys - ordinary_keys),
            "only_ordinary_parent": sorted(ordinary_keys - target_keys),
        }

        report = {
            "tested_at": datetime.now().isoformat(timespec="seconds"),
            "video_url": args.video_url,
            "target_comment_phrase": args.target_comment_phrase,
            "target_reply_phrase": args.target_reply_phrase,
            "capture_count": len(captures),
            "captured_top_comment_count": len(all_rows),
            "target_comment_match_count": len(target_comments),
            "target_reply_match_count": len(target_replies),
            "target_reply_paths": [
                {
                    "page_index": match["page_index"],
                    "row_index": match["row_index"],
                    "path": match["path"],
                    "reply_summary": summarize_comment(match["row"]),
                }
                for match in target_replies
            ],
            "target_parent_summary": target_summary,
            "ordinary_expand_comment_summary": ordinary_summary,
            "parent_key_diff": key_diff,
            "captures_meta": [
                {
                    "request_cursor": item["request_cursor"],
                    "response_cursor": item["response_cursor"],
                    "has_more": item["has_more"],
                    "count": len(item["comments"]),
                }
                for item in captures
            ],
            "dom_contains_target_comment": args.target_comment_phrase in page.locator("body").inner_text(),
            "dom_contains_target_reply": args.target_reply_phrase in page.locator("body").inner_text(),
        }

        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        latest_path = DEBUG_DIR / "author_reply_field_inspection_latest.json"
        archive_path = DEBUG_DIR / f"author_reply_field_inspection_{now}.json"
        content = json.dumps(report, ensure_ascii=False, indent=2)
        latest_path.write_text(content, encoding="utf-8")
        archive_path.write_text(content, encoding="utf-8")
        page.screenshot(
            path=str(DEBUG_DIR / "author_reply_field_inspection_latest.png"),
            full_page=False,
        )

        print("\n检查结果：")
        print(f"目标一级评论匹配：{len(target_comments)}")
        print(f"目标作者回复匹配：{len(target_replies)}")
        for match in target_replies:
            print(
                f"作者回复路径：page={match['page_index']} row={match['row_index']} "
                f"path={match['path']}"
            )
        print(f"报告：{latest_path}")

        if args.keep_open:
            input("按 Enter 关闭浏览器...")
        context.close()
        return report


def main():
    parser = argparse.ArgumentParser(description="检查作者内嵌回复与普通展开回复的原始字段差异。")
    parser.add_argument("--video-url", default=DEFAULT_VIDEO_URL)
    parser.add_argument("--target-comment-phrase", default=TARGET_COMMENT_PHRASE)
    parser.add_argument("--target-reply-phrase", default=TARGET_REPLY_PHRASE)
    parser.add_argument("--wait-ms", type=int, default=8000)
    parser.add_argument("--scroll-rounds", type=int, default=12)
    parser.add_argument("--scroll-wait-ms", type=int, default=1200)
    parser.add_argument("--keep-open", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
