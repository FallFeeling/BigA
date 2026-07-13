import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from douyin_first_video_comments import format_timestamp, launch_context, load_first_video


ROOT_DIR = Path(__file__).resolve().parent
DEBUG_DIR = ROOT_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def is_top_comment_url(url):
    lower = url.lower()
    return "comment/list" in lower and "reply" not in lower


def is_reply_url(url):
    lower = url.lower()
    return "comment" in lower and "reply" in lower


def reply_parent_id_from_url(url):
    query = parse_qs(urlparse(url).query)
    return str((query.get("comment_id") or [""])[0])


def replace_query_value(url, key, value):
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query[key] = [str(value)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


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


def get_value(row, *keys):
    if not isinstance(row, dict):
        return None
    lowered = {str(k).lower(): value for k, value in row.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def normalize_comment(row):
    user = get_value(row, "user", "user_info") or {}
    create_time = get_value(row, "create_time", "createtime")
    try:
        create_time = int(create_time)
    except Exception:
        create_time = None
    return {
        "comment_id": str(get_value(row, "cid", "comment_id", "id") or ""),
        "text": str(get_value(row, "text", "content") or ""),
        "create_time": create_time,
        "create_time_display": format_timestamp(create_time),
        "digg_count": get_value(row, "digg_count", "like_count"),
        "reply_comment_total": get_value(
            row, "reply_comment_total", "reply_total", "reply_count"
        ),
        "user_nickname": str(get_value(user, "nickname", "name") or ""),
        "user_uid": str(get_value(user, "uid", "user_id") or ""),
        "user_sec_uid": str(get_value(user, "sec_uid", "sec_user_id") or ""),
    }


def page_meta(payload):
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return {
        "cursor": data.get("cursor"),
        "has_more": data.get("has_more", data.get("hasMore")),
        "total": data.get("total"),
    }


def find_and_click_target_reply_button(page, target_text, remaining_count):
    return page.evaluate(
        r"""
        ({targetText, remainingCount}) => {
            const clean = value => (value || "").replace(/\u200b/g, "").replace(/\s+/g, " ").trim();
            const wanted = clean(targetText);
            const visible = el => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" &&
                    Number(style.opacity) !== 0 && rect.width > 5 && rect.height > 5;
            };
            const isExpand = text => {
                const value = clean(text).replace(/\s+/g, "");
                if (!value || value === "回复" || value.length > 80 || value.includes("发表评论")) return false;
                return [
                    /^展开\d*条?回复$/, /^展开全部\d*条?回复$/, /^展开更多回复$/,
                    /^查看\d*条?回复$/, /^查看全部\d*条?回复$/, /^查看更多\d*条?回复$/,
                    /^共\d+条?回复$/, /^还有\d+条?回复$/, /^更多回复$/,
                    /^展开.*回复$/, /^查看.*回复$/, /^还有.*回复$/
                ].some(pattern => pattern.test(value));
            };

            const all = Array.from(document.querySelectorAll("div, p, span"));
            const textNode = all.find(el => clean(el.innerText || el.textContent) === wanted);
            if (!textNode) return {clicked: false, reason: "target_comment_not_in_dom"};

            let scope = textNode;
            for (let depth = 0; depth < 12 && scope; depth++, scope = scope.parentElement) {
                const candidates = Array.from(scope.querySelectorAll("button, a, div, p, span"))
                    .filter(el => !el.dataset.mrReplyAttempted && visible(el) && isExpand(el.innerText || el.textContent))
                    .sort((a, b) => {
                        const aText = clean(a.innerText || a.textContent).replace(/\s+/g, "");
                        const bText = clean(b.innerText || b.textContent).replace(/\s+/g, "");
                        const aExact = remainingCount > 0 && aText.includes(String(remainingCount) + "条回复") ? 0 : 1;
                        const bExact = remainingCount > 0 && bText.includes(String(remainingCount) + "条回复") ? 0 : 1;
                        if (aExact !== bExact) return aExact - bExact;
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return ar.width * ar.height - br.width * br.height;
                    });
                if (!candidates.length) continue;

                const target = candidates[0];
                target.scrollIntoView({block: "center", inline: "nearest"});
                const rect = target.getBoundingClientRect();
                const label = clean(candidates[0].innerText || candidates[0].textContent);
                target.dataset.mrReplyAttempted = "1";
                target.click();
                return {clicked: true, label, depth, x: rect.x, y: rect.y};
            }
            return {clicked: false, reason: "reply_expand_button_not_found_near_target"};
        }
        """,
        {"targetText": target_text, "remainingCount": remaining_count},
    )


def scroll_until_target(page, target_text, max_rounds=20):
    for index in range(max_rounds + 1):
        locator = page.get_by_text(target_text, exact=True)
        try:
            if locator.count() and locator.first.is_visible():
                locator.first.scroll_into_view_if_needed()
                return {"found": True, "scroll_rounds": index}
        except Exception:
            pass
        page.mouse.move(650, 820)
        page.mouse.wheel(0, 650)
        page.wait_for_timeout(500)
    return {"found": False, "scroll_rounds": max_rounds}


def target_ancestor_summaries(page, target_text):
    return page.evaluate(
        r"""
        (targetText) => {
            const clean = value => (value || "").replace(/\u200b/g, "").replace(/\s+/g, " ").trim();
            const wanted = clean(targetText);
            const node = Array.from(document.querySelectorAll("div, p, span"))
                .find(el => clean(el.innerText || el.textContent) === wanted);
            if (!node) return [];
            const rows = [];
            let current = node;
            for (let depth = 0; depth < 10 && current; depth++, current = current.parentElement) {
                rows.push({
                    depth,
                    tag: current.tagName,
                    class_name: String(current.className || "").slice(0, 500),
                    text: clean(current.innerText || current.textContent).slice(0, 6000)
                });
            }
            return rows;
        }
        """,
        target_text,
    )


def run(args):
    source_video, video_id, href = load_first_video()
    top_pages = []
    reply_pages = []
    response_diagnostics = []

    with sync_playwright() as playwright:
        context = launch_context(playwright)
        page = context.new_page()

        def on_response(response):
            try:
                if response.request.resource_type not in {"xhr", "fetch"}:
                    return
                if not (is_top_comment_url(response.url) or is_reply_url(response.url)):
                    return
                content_type = response.headers.get("content-type", "")
                text = response.text()
                try:
                    payload = json.loads(text)
                except Exception as exc:
                    response_diagnostics.append(
                        {
                            "url": response.url,
                            "status": response.status,
                            "content_type": content_type,
                            "body_length": len(text),
                            "body_preview": text[:1000],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    print(
                        f"评论响应不是 JSON：status={response.status} type={content_type} "
                        f"length={len(text)} url={response.url[:180]}"
                    )
                    return
                raw_comments = direct_comments(payload)
                record = {
                    "url": response.url,
                    "status": response.status,
                    "meta": page_meta(payload),
                    "comments": [normalize_comment(row) for row in raw_comments],
                }
                if is_reply_url(response.url):
                    reply_pages.append(record)
                    print(
                        f"捕获回复页：parent={reply_parent_id_from_url(response.url)}，"
                        f"{len(record['comments'])} 条，"
                        f"cursor={record['meta']['cursor']}，has_more={record['meta']['has_more']}"
                    )
                else:
                    record["raw_comments"] = raw_comments
                    top_pages.append(record)
            except Exception as exc:
                print(f"解析评论接口失败：{type(exc).__name__}: {exc}")

        page.on("response", on_response)
        print(f"打开第一条视频：{video_id}\n{href}")
        try:
            page.goto(href, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            print("页面导航超时，继续检查已加载内容。")
        page.wait_for_timeout(args.wait_ms)

        if not top_pages or not top_pages[0]["comments"]:
            raise RuntimeError("没有捕获到第一屏一级评论接口。")

        first_page_comments = top_pages[0]["comments"]
        if args.first_with_replies:
            target = next(
                (
                    item
                    for item in first_page_comments
                    if int(item.get("reply_comment_total") or 0) > 0
                ),
                first_page_comments[0],
            )
            target_definition = "first item with replies in the first top-level comment API response"
        else:
            target = first_page_comments[0]
            target_definition = "first item of the first top-level comment API response"
        expected_reply_count = int(target.get("reply_comment_total") or 0)
        target_raw = next(
            (
                row
                for row in top_pages[0].get("raw_comments", [])
                if normalize_comment(row)["comment_id"] == target["comment_id"]
            ),
            {},
        )
        embedded_reply_fields = {}
        for key, value in target_raw.items():
            if "reply" in str(key).lower() and isinstance(value, list):
                embedded_reply_fields[key] = {
                    "count": len(value),
                    "items": [normalize_comment(item) for item in value if isinstance(item, dict)],
                }
        print(f"\n目标：{target_definition}")
        print(f"评论 ID：{target['comment_id']}")
        print(f"用户    ：{target['user_nickname']}")
        print(f"内容    ：{target['text']}")
        print(f"回复总数：{expected_reply_count}")

        target_location = scroll_until_target(page, target["text"], args.max_scroll_rounds)
        click_attempts = []
        no_progress = 0

        while expected_reply_count > 0 and len(click_attempts) < args.max_expand_clicks:
            existing_ids = {
                item["comment_id"]
                for page_record in reply_pages
                if reply_parent_id_from_url(page_record.get("url", "")) == target["comment_id"]
                for item in page_record["comments"]
                if item["comment_id"]
            }
            remaining_count = max(0, expected_reply_count - len(existing_ids))
            before_pages = sum(
                1
                for item in reply_pages
                if reply_parent_id_from_url(item.get("url", "")) == target["comment_id"]
            )
            before_all_pages = len(reply_pages)
            before_diagnostics = len(response_diagnostics)
            result = find_and_click_target_reply_button(
                page, target["text"], remaining_count
            )
            click_attempts.append(result)
            if not result.get("clicked"):
                break
            print(f"点击：{result.get('label', '')}")
            page.wait_for_timeout(args.expand_wait_ms)

            target_page_count = sum(
                1
                for item in reply_pages
                if reply_parent_id_from_url(item.get("url", "")) == target["comment_id"]
            )
            if target_page_count == before_pages:
                no_progress += 1
                if len(reply_pages) > before_all_pages:
                    print("刚才点到的是另一条评论的回复，继续寻找目标按钮。")
                    no_progress = 0
                    continue
                if args.pause_on_empty_reply and len(response_diagnostics) > before_diagnostics:
                    input(
                        "检测到回复接口为空。如果浏览器出现验证码，请完成验证；"
                        "完成后在此按 Enter 继续重试..."
                    )
                    no_progress = 0
            else:
                no_progress = 0

            unique_ids = {
                item["comment_id"]
                for page_record in reply_pages
                if reply_parent_id_from_url(page_record.get("url", "")) == target["comment_id"]
                for item in page_record["comments"]
                if item["comment_id"]
            }
            if expected_reply_count and len(unique_ids) >= expected_reply_count:
                break
            if args.direct_pagination and target_page_count > before_pages:
                break
            target_pages = [
                item
                for item in reply_pages
                if reply_parent_id_from_url(item.get("url", "")) == target["comment_id"]
            ]
            if target_pages and target_pages[-1]["meta"].get("has_more") in (0, False):
                break
            if no_progress >= 2:
                break

        direct_pagination_attempts = []
        if args.direct_pagination:
            target_pages = [
                item
                for item in reply_pages
                if reply_parent_id_from_url(item.get("url", "")) == target["comment_id"]
            ]
            if target_pages:
                base_url = target_pages[0]["url"]
                cursor = target_pages[-1]["meta"].get("cursor")
                has_more = target_pages[-1]["meta"].get("has_more")
                seen_cursors = set()
                while has_more not in (0, False) and cursor is not None and cursor not in seen_cursors:
                    seen_cursors.add(cursor)
                    page_url = replace_query_value(base_url, "cursor", cursor)
                    try:
                        direct_response = context.request.get(
                            page_url,
                            headers={"referer": href},
                            timeout=15_000,
                        )
                        direct_text = direct_response.text()
                        attempt = {
                            "request_cursor": cursor,
                            "status": direct_response.status,
                            "body_length": len(direct_text),
                            "parsed": False,
                        }
                        try:
                            direct_payload = json.loads(direct_text)
                            direct_record = {
                                "url": page_url,
                                "status": direct_response.status,
                                "meta": page_meta(direct_payload),
                                "comments": [
                                    normalize_comment(row)
                                    for row in direct_comments(direct_payload)
                                ],
                                "source": "cursor_mutation_test",
                            }
                            attempt["parsed"] = True
                            attempt["returned"] = len(direct_record["comments"])
                            attempt["meta"] = direct_record["meta"]
                            direct_pagination_attempts.append(attempt)
                            reply_pages.append(direct_record)
                            print(
                                f"直接分页 cursor={cursor}：{len(direct_record['comments'])} 条，"
                                f"next={direct_record['meta'].get('cursor')}，"
                                f"has_more={direct_record['meta'].get('has_more')}"
                            )
                            cursor = direct_record["meta"].get("cursor")
                            has_more = direct_record["meta"].get("has_more")
                            continue
                        except Exception:
                            pass
                        direct_pagination_attempts.append(attempt)
                        print(
                            f"直接分页 cursor={cursor} 返回非 JSON，"
                            f"status={direct_response.status} length={len(direct_text)}"
                        )
                    except Exception as exc:
                        direct_pagination_attempts.append(
                            {"request_cursor": cursor, "error": f"{type(exc).__name__}: {exc}"}
                        )
                    break

        # 某些回复请求在页面 response 事件中显示 200 但正文为空；立即用同一浏览器
        # 上下文和同一签名 URL 重试一次，排除监听时机导致的误判。
        retry_attempts = []
        target_empty_urls = []
        for diagnostic in response_diagnostics:
            query = parse_qs(urlparse(diagnostic.get("url", "")).query)
            if str((query.get("comment_id") or [""])[0]) == target["comment_id"]:
                target_empty_urls.append(diagnostic["url"])
        for url in dict.fromkeys(target_empty_urls):
            try:
                retry_response = context.request.get(
                    url,
                    headers={"referer": href},
                    timeout=15_000,
                )
                retry_text = retry_response.text()
                retry_record = {
                    "status": retry_response.status,
                    "content_type": retry_response.headers.get("content-type", ""),
                    "body_length": len(retry_text),
                    "body_preview": retry_text[:1000],
                    "parsed": False,
                }
                try:
                    retry_payload = json.loads(retry_text)
                    retry_record["parsed"] = True
                    parsed_record = {
                        "url": url,
                        "status": retry_response.status,
                        "meta": page_meta(retry_payload),
                        "comments": [
                            normalize_comment(row)
                            for row in direct_comments(retry_payload)
                        ],
                        "source": "browser_context_request_retry",
                    }
                    reply_pages.append(parsed_record)
                except Exception:
                    pass
                retry_attempts.append(retry_record)
                print(
                    f"浏览器上下文重试：status={retry_response.status} "
                    f"length={len(retry_text)} parsed={retry_record['parsed']}"
                )
            except Exception as exc:
                retry_attempts.append({"error": f"{type(exc).__name__}: {exc}"})

        replies = []
        seen = set()
        for page_record in reply_pages:
            if reply_parent_id_from_url(page_record.get("url", "")) != target["comment_id"]:
                continue
            for reply in page_record["comments"]:
                key = reply["comment_id"] or (
                    reply["create_time"], reply["user_nickname"], reply["text"]
                )
                if key in seen:
                    continue
                seen.add(key)
                replies.append(reply)

        ancestor_summaries = target_ancestor_summaries(page, target["text"])

        complete = expected_reply_count == len(replies)
        if expected_reply_count == 0:
            complete = True
            conclusion = "first_comment_has_no_replies"
        elif complete:
            conclusion = "all_replies_captured"
        elif not reply_pages:
            conclusion = "reply_button_or_reply_api_not_reached"
        else:
            conclusion = "partial_replies_captured"

        report = {
            "tested_at": datetime.now().isoformat(timespec="seconds"),
            "video_id": video_id,
            "href": href,
            "source_video": source_video,
            "target_definition": target_definition,
            "target_comment": target,
            "target_raw_keys": list(target_raw.keys()),
            "embedded_reply_fields": embedded_reply_fields,
            "target_location": target_location,
            "expected_reply_count": expected_reply_count,
            "captured_reply_count": len(replies),
            "complete": complete,
            "conclusion": conclusion,
            "click_attempts": click_attempts,
            "reply_pages": reply_pages,
            "replies": replies,
            "response_diagnostics": response_diagnostics,
            "retry_attempts": retry_attempts,
            "direct_pagination_attempts": direct_pagination_attempts,
            "target_ancestor_summaries_after_expand": ancestor_summaries,
        }

        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        latest_path = DEBUG_DIR / "first_comment_all_replies_latest.json"
        archive_path = DEBUG_DIR / f"first_comment_all_replies_{now}.json"
        content = json.dumps(report, ensure_ascii=False, indent=2)
        latest_path.write_text(content, encoding="utf-8")
        archive_path.write_text(content, encoding="utf-8")
        page.screenshot(
            path=str(DEBUG_DIR / "first_comment_all_replies_latest.png"),
            full_page=False,
        )

        print(f"\n结论：{conclusion}")
        print(f"回复：{len(replies)}/{expected_reply_count}")
        for index, reply in enumerate(replies, 1):
            print(
                f"[{index}] {reply['user_nickname']} | {reply['create_time_display']} | "
                f"{re.sub(r'\\s+', ' ', reply['text']).strip()}"
            )
        print(f"报告：{latest_path}")

        if args.keep_open:
            input("按 Enter 关闭浏览器...")
        context.close()
        return report


def main():
    parser = argparse.ArgumentParser(description="读取第一条视频中第一条一级评论的全部回复。")
    parser.add_argument("--wait-ms", type=int, default=8000)
    parser.add_argument("--expand-wait-ms", type=int, default=1200)
    parser.add_argument("--max-expand-clicks", type=int, default=30)
    parser.add_argument("--max-scroll-rounds", type=int, default=20)
    parser.add_argument(
        "--first-with-replies",
        action="store_true",
        help="若第一页第一条没有回复，则测试第一页中第一条确实有回复的评论。",
    )
    parser.add_argument(
        "--pause-on-empty-reply",
        action="store_true",
        help="回复接口为空时暂停，等待手动完成浏览器验证码后继续。",
    )
    parser.add_argument(
        "--direct-pagination",
        action="store_true",
        help="首批回复成功后，测试复用签名 URL 直接修改 cursor 分页。",
    )
    parser.add_argument("--keep-open", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
