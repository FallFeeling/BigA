import argparse
import csv
import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


ROOT_DIR = Path(__file__).resolve().parent

PROFILE_DIR = ROOT_DIR / "runtime" / "douyin_profile"
DATA_DIR = ROOT_DIR / "data"
DEBUG_DIR = ROOT_DIR / "debug"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def clean_text(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def parse_count(value):
    """
    支持：
    9387 -> 9387
    1.6万 -> 16000
    304.3万 -> 3043000
    2.2w -> 22000
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s:
        return None

    m = re.search(r"(\d+(?:\.\d+)?)(亿|万|w|W|k|K)?", s)
    if not m:
        return None

    num = float(m.group(1))
    unit = m.group(2)

    if unit in ["万", "w", "W"]:
        num *= 10000
    elif unit == "亿":
        num *= 100000000
    elif unit in ["k", "K"]:
        num *= 1000

    return int(num)


def to_int(value):
    return parse_count(value)


def parse_time_value(value):
    """
    返回：
    {
        "timestamp": int | None,
        "display": str
    }
    """
    if value is None:
        return {"timestamp": None, "display": ""}

    if isinstance(value, bool):
        return {"timestamp": None, "display": ""}

    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts // 1000

        try:
            return {
                "timestamp": ts,
                "display": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception:
            return {"timestamp": None, "display": ""}

    s = clean_text(value)

    if not s:
        return {"timestamp": None, "display": ""}

    if re.fullmatch(r"\d{10,13}", s):
        ts = int(s)
        if ts > 10_000_000_000:
            ts = ts // 1000

        try:
            return {
                "timestamp": ts,
                "display": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception:
            return {"timestamp": None, "display": s}

    return {"timestamp": None, "display": s}


def extract_video_id(href):
    m = re.search(r"/video/(\d+)", href or "")
    return m.group(1) if m else ""


def parse_home_like(video):
    """
    优先从 raw_text 开头识别点赞数，避免 1.6万 被误读成 1.6。
    """
    raw_text = clean_text(video.get("raw_text", ""))
    like_hint = clean_text(video.get("like_hint", ""))

    m = re.match(
        r"^(?:置顶\s*)?(\d+(?:\.\d+)?(?:万|亿|w|W|k|K)?)",
        raw_text,
    )
    if m:
        raw = m.group(1)
        return raw, parse_count(raw)

    if like_hint:
        return like_hint, parse_count(like_hint)

    return "", None


def launch_context(playwright):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        accept_downloads=False,
    )


def norm_key(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


ID_KEYS = {
    "awemeid",
    "itemid",
    "groupid",
    "videoid",
    "id",
}

LIKE_KEYS = {
    "diggcount",
    "diggcountstr",
    "likecount",
    "likecountstr",
    "likedcount",
    "likedcountstr",
}

COMMENT_KEYS = {
    "commentcount",
    "commentcountstr",
    "commenttotal",
    "commenttotalstr",
}

TIME_KEYS = {
    "createtime",
    "publishtime",
    "createat",
    "publishat",
}

DESC_KEYS = {
    "desc",
    "title",
    "caption",
    "sharetitle",
}


def iter_dicts(obj):
    stack = [obj]

    while stack:
        cur = stack.pop()

        if isinstance(cur, dict):
            yield cur
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)

        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)


def direct_id_match(d, video_id):
    if not isinstance(d, dict):
        return False

    for k, v in d.items():
        if norm_key(k) in ID_KEYS and str(v) == str(video_id):
            return True

    return False


def find_value_by_keys(obj, keys, max_depth=5):
    """
    在一个对象子树里按 key 找第一个值。
    用 BFS，优先靠近当前对象的字段。
    """
    queue = [(obj, 0)]

    while queue:
        cur, depth = queue.pop(0)

        if depth > max_depth:
            continue

        if isinstance(cur, dict):
            for k, v in cur.items():
                if norm_key(k) in keys:
                    return v

            for v in cur.values():
                if isinstance(v, (dict, list)):
                    queue.append((v, depth + 1))

        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    queue.append((v, depth + 1))

    return None


def extract_candidate_from_object(d, source, video_id):
    if not direct_id_match(d, video_id):
        return None

    like_raw = find_value_by_keys(d, LIKE_KEYS)
    comment_raw = find_value_by_keys(d, COMMENT_KEYS)
    time_raw = find_value_by_keys(d, TIME_KEYS)
    desc_raw = find_value_by_keys(d, DESC_KEYS)

    like_count = to_int(like_raw)
    comment_count = to_int(comment_raw)
    parsed_time = parse_time_value(time_raw)

    desc = ""
    if isinstance(desc_raw, str):
        desc = clean_text(desc_raw)

    score = 5

    if like_count is not None:
        score += 3

    if comment_count is not None:
        score += 3

    if parsed_time["display"]:
        score += 3

    if desc:
        score += 1

    return {
        "source": source,
        "method": "json_object",
        "score": score,
        "video_id": video_id,
        "published_timestamp": parsed_time["timestamp"],
        "published_at": parsed_time["display"],
        "detail_like_raw": str(like_raw) if like_raw is not None else "",
        "detail_like_count": like_count,
        "comment_raw": str(comment_raw) if comment_raw is not None else "",
        "comment_count": comment_count,
        "desc": desc,
    }


def try_json_loads(text):
    if not text:
        return None

    variants = []

    raw = text.strip()
    variants.append(raw)

    try:
        variants.append(unquote(raw))
    except Exception:
        pass

    try:
        variants.append(html.unescape(raw))
    except Exception:
        pass

    try:
        variants.append(html.unescape(unquote(raw)))
    except Exception:
        pass

    for v in variants:
        v = v.strip()
        if not v:
            continue

        if not (v.startswith("{") or v.startswith("[")):
            continue

        try:
            return json.loads(v)
        except Exception:
            pass

    return None


def try_extract_json_from_script(text):
    """
    处理类似：
    window.xxx = {...}
    或 script 中夹着一段 JSON 的情况。
    """
    if not text:
        return None

    text = text.strip()

    direct = try_json_loads(text)
    if direct is not None:
        return direct

    decoded = text
    try:
        decoded = html.unescape(unquote(text))
    except Exception:
        pass

    first_obj = decoded.find("{")
    last_obj = decoded.rfind("}")

    if first_obj >= 0 and last_obj > first_obj:
        candidate = decoded[first_obj:last_obj + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    first_arr = decoded.find("[")
    last_arr = decoded.rfind("]")

    if first_arr >= 0 and last_arr > first_arr:
        candidate = decoded[first_arr:last_arr + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    return None


def extract_json_candidates(data, source, video_id):
    candidates = []

    for d in iter_dicts(data):
        cand = extract_candidate_from_object(d, source, video_id)
        if cand:
            candidates.append(cand)

    return candidates


def regex_count_field(win, keys):
    for key in keys:
        patterns = [
            rf'"{key}"\s*:\s*"?([0-9.万亿wWkK]+)"?',
            rf"'{key}'\s*:\s*'?([0-9.万亿wWkK]+)'?",
        ]

        for pat in patterns:
            m = re.search(pat, win)
            if m:
                return m.group(1), parse_count(m.group(1))

    return "", None


def regex_time_field(win):
    keys = [
        "create_time",
        "createTime",
        "publish_time",
        "publishTime",
    ]

    for key in keys:
        patterns = [
            rf'"{key}"\s*:\s*"?(\d{{10,13}})"?',
            rf"'{key}'\s*:\s*'?(\d{{10,13}})'?",
        ]

        for pat in patterns:
            m = re.search(pat, win)
            if m:
                parsed = parse_time_value(m.group(1))
                return m.group(1), parsed["timestamp"], parsed["display"]

    return "", None, ""


def regex_desc_field(win):
    keys = ["desc", "title"]

    for key in keys:
        pat = rf'"{key}"\s*:\s*"((?:\\.|[^"\\]){{0,500}})"'
        m = re.search(pat, win)
        if m:
            s = m.group(1)
            try:
                s = json.loads(f'"{s}"')
            except Exception:
                pass
            return clean_text(s)

    return ""


def extract_regex_candidates(text, source, video_id):
    """
    兜底：在包含 video_id 的文本窗口附近找点赞/评论/时间字段。
    """
    if not text or str(video_id) not in text:
        return []

    normalized = text.replace('\\"', '"').replace("\\/", "/")

    candidates = []
    positions = [m.start() for m in re.finditer(re.escape(str(video_id)), normalized)]

    for pos in positions[:10]:
        left = max(0, pos - 25000)
        right = min(len(normalized), pos + 25000)
        win = normalized[left:right]

        like_raw, like_count = regex_count_field(
            win,
            [
                "digg_count",
                "diggCount",
                "digg_count_str",
                "diggCountStr",
                "like_count",
                "likeCount",
                "like_count_str",
                "likeCountStr",
            ],
        )

        comment_raw, comment_count = regex_count_field(
            win,
            [
                "comment_count",
                "commentCount",
                "comment_count_str",
                "commentCountStr",
            ],
        )

        time_raw, ts, published_at = regex_time_field(win)
        desc = regex_desc_field(win)

        score = 1

        if like_count is not None:
            score += 3

        if comment_count is not None:
            score += 3

        if published_at:
            score += 3

        if desc:
            score += 1

        if score > 1:
            candidates.append(
                {
                    "source": source,
                    "method": "regex_window",
                    "score": score,
                    "video_id": video_id,
                    "published_timestamp": ts,
                    "published_at": published_at,
                    "detail_like_raw": like_raw,
                    "detail_like_count": like_count,
                    "comment_raw": comment_raw,
                    "comment_count": comment_count,
                    "desc": desc,
                }
            )

    return candidates


def collect_page_script_sources(page, video_id):
    sources = []

    try:
        scripts = page.evaluate(
            """
            () => Array.from(document.scripts).map((s, i) => ({
                index: i,
                id: s.id || "",
                type: s.type || "",
                text: s.textContent || ""
            }))
            """
        )
    except Exception:
        return sources

    for s in scripts:
        text = s.get("text", "")

        if not text:
            continue

        if str(video_id) not in text and "aweme" not in text.lower() and "comment" not in text.lower():
            continue

        name = f"script#{s.get('id') or s.get('index')}"

        sources.append(
            {
                "source": name,
                "text": text,
            }
        )

        try:
            decoded = html.unescape(unquote(text))
            if decoded != text:
                sources.append(
                    {
                        "source": name + ":decoded",
                        "text": decoded,
                    }
                )
        except Exception:
            pass

    return sources


def collect_storage_sources(page, video_id):
    sources = []

    try:
        storage_items = page.evaluate(
            """
            () => {
                const rows = [];

                function dumpStorage(storage, prefix) {
                    for (let i = 0; i < storage.length; i++) {
                        const key = storage.key(i);
                        const value = storage.getItem(key);
                        rows.push({
                            source: prefix + ":" + key,
                            text: value || ""
                        });
                    }
                }

                dumpStorage(window.localStorage, "localStorage");
                dumpStorage(window.sessionStorage, "sessionStorage");

                return rows;
            }
            """
        )
    except Exception:
        return sources

    for item in storage_items:
        text = item.get("text", "")

        if not text:
            continue

        if str(video_id) not in text and "aweme" not in text.lower() and "comment" not in text.lower():
            continue

        sources.append(item)

    return sources


def analyze_sources(sources, video_id):
    all_candidates = []

    for src in sources:
        source_name = src.get("source", "")
        text = src.get("text", "")

        data = try_extract_json_from_script(text)

        if data is not None:
            all_candidates.extend(
                extract_json_candidates(
                    data=data,
                    source=source_name,
                    video_id=video_id,
                )
            )

        all_candidates.extend(
            extract_regex_candidates(
                text=text,
                source=source_name,
                video_id=video_id,
            )
        )

    # 去重
    dedup = {}
    for c in all_candidates:
        key = (
            c.get("method"),
            c.get("source"),
            c.get("published_at"),
            c.get("detail_like_count"),
            c.get("comment_count"),
            c.get("desc"),
        )
        old = dedup.get(key)
        if old is None or c.get("score", 0) > old.get("score", 0):
            dedup[key] = c

    candidates = list(dedup.values())
    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

    best = candidates[0] if candidates else {}

    return {
        "best": best,
        "candidates": candidates[:30],
    }


def extract_dom_publish_time(page):
    """
    DOM 兜底：发布时间通常能从可见文本读到。
    """
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""

    body_text = clean_text(body_text)

    patterns = [
        r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}[日号]?(?:\s+\d{1,2}:\d{2})?)",
        r"(\d{1,2}[-/.月]\d{1,2}[日号]?(?:\s+\d{1,2}:\d{2})?)",
        r"(昨天\s*\d{1,2}:\d{2})",
        r"(\d+分钟前)",
        r"(\d+小时前)",
        r"(刚刚)",
    ]

    for pat in patterns:
        m = re.search(pat, body_text)
        if m:
            return clean_text(m.group(1))

    return ""


def save_debug(video_id, page_url, captured, analysis, dom_publish_raw):
    latest_path = DEBUG_DIR / f"detail_debug_{video_id}_latest.json"
    time_path = DEBUG_DIR / f"detail_debug_{video_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    payload = {
        "video_id": video_id,
        "page_url": page_url,
        "debug_at": datetime.now().isoformat(timespec="seconds"),
        "dom_publish_raw": dom_publish_raw,
        "captured_response_count": len(captured),
        "captured_responses_preview": [
            {
                "url": x.get("url", ""),
                "status": x.get("status", ""),
                "resource_type": x.get("resource_type", ""),
                "content_type": x.get("content_type", ""),
                "text_len": len(x.get("text", "")),
                "text_preview": x.get("text", "")[:500],
            }
            for x in captured
        ],
        "analysis": analysis,
    }

    for path in [latest_path, time_path]:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"  debug 已保存：{latest_path}")


def save_output(videos, processed_count):
    now = datetime.now().strftime("%Y%m%d_%H%M%S")

    output_payload = {
        "enriched_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(videos),
        "processed_count": processed_count,
        "videos": videos,
    }

    latest_json = DATA_DIR / "author_videos_enriched_latest.json"
    timestamp_json = DATA_DIR / f"author_videos_enriched_{now}.json"

    latest_csv = DATA_DIR / "author_videos_enriched_latest.csv"
    timestamp_csv = DATA_DIR / f"author_videos_enriched_{now}.csv"

    for path in [latest_json, timestamp_json]:
        path.write_text(
            json.dumps(output_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    fieldnames = [
        "order_index",
        "video_id",
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
        "detail_desc",
        "detail_method",
        "detail_source",
        "detail_score",
        "detail_error",
    ]

    for path in [latest_csv, timestamp_csv]:
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for v in videos:
                row = {k: v.get(k, "") for k in fieldnames}
                writer.writerow(row)

    print(f"\n已保存 JSON：{latest_json}")
    print(f"已保存 CSV ：{latest_csv}")
    print(f"本次归档 JSON：{timestamp_json}")
    print(f"本次归档 CSV ：{timestamp_csv}")


def enrich_details(limit=5, wait_ms=8000, debug=False):
    input_path = DATA_DIR / "author_videos_latest.json"

    if not input_path.exists():
        raise FileNotFoundError(
            f"找不到 {input_path}，请先运行：python douyin_author_videos.py --scan"
        )

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    videos = payload.get("videos", [])

    if limit > 0:
        videos_to_process = videos[:limit]
    else:
        videos_to_process = videos

    with sync_playwright() as p:
        context = launch_context(p)

        for idx, video in enumerate(videos_to_process, 1):
            href = video.get("href", "")
            video_id = video.get("video_id") or extract_video_id(href)

            print(f"\n[{idx}/{len(videos_to_process)}] 打开视频详情页：{video_id}")
            print(href)

            captured = []
            page = context.new_page()

            def on_response(response):
                try:
                    req = response.request
                    resource_type = req.resource_type
                    url = response.url
                    content_type = response.headers.get("content-type", "")

                    if resource_type not in ["xhr", "fetch", "document"]:
                        return

                    if "douyin" not in url:
                        return

                    lower_url = url.lower()

                    interesting = any(
                        k in lower_url
                        for k in [
                            "aweme",
                            "comment",
                            "detail",
                            "item",
                            "post",
                            "video",
                        ]
                    )

                    if "json" not in content_type.lower() and not interesting:
                        return

                    text = response.text()

                    if not text:
                        return

                    if len(text) > 5_000_000:
                        return

                    captured.append(
                        {
                            "url": url,
                            "status": response.status,
                            "resource_type": resource_type,
                            "content_type": content_type,
                            "text": text,
                        }
                    )

                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(href, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(wait_ms)

                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass

                sources = []

                for x in captured:
                    sources.append(
                        {
                            "source": x.get("url", ""),
                            "text": x.get("text", ""),
                        }
                    )

                sources.extend(collect_page_script_sources(page, video_id))
                sources.extend(collect_storage_sources(page, video_id))

                analysis = analyze_sources(sources, video_id)
                best = analysis.get("best", {}) or {}

                dom_publish_raw = extract_dom_publish_time(page)

                home_like_raw, home_like_count = parse_home_like(video)

                published_at = best.get("published_at") or dom_publish_raw
                published_timestamp = best.get("published_timestamp")

                detail_like_raw = best.get("detail_like_raw", "")
                detail_like_count = best.get("detail_like_count")

                comment_raw = best.get("comment_raw", "")
                comment_count = best.get("comment_count")

                final_like_count = (
                    detail_like_count
                    if detail_like_count is not None
                    else home_like_count
                )

                video["home_like_raw"] = home_like_raw
                video["home_like_count"] = home_like_count

                video["published_at"] = published_at
                video["published_timestamp"] = published_timestamp

                video["detail_like_raw"] = detail_like_raw
                video["detail_like_count"] = detail_like_count
                video["final_like_count"] = final_like_count

                video["comment_raw"] = comment_raw
                video["comment_count"] = comment_count

                video["detail_desc"] = best.get("desc", "")
                video["detail_source"] = best.get("source", "")
                video["detail_method"] = best.get("method", "")
                video["detail_score"] = best.get("score", 0)

                video["detail_final_url"] = page.url
                video["detail_extracted_at"] = datetime.now().isoformat(timespec="seconds")
                video["detail_error"] = ""

                print(f"  发布时间       : {video['published_at']}")
                print(f"  主页点赞       : {video['home_like_raw']} -> {video['home_like_count']}")
                print(f"  详情点赞       : {video['detail_like_raw']} -> {video['detail_like_count']}")
                print(f"  最终点赞       : {video['final_like_count']}")
                print(f"  评论数         : {video['comment_raw']} -> {video['comment_count']}")
                print(f"  数据来源       : {video['detail_method']} | {video['detail_source'][:120]}")

                if debug:
                    save_debug(
                        video_id=video_id,
                        page_url=page.url,
                        captured=captured,
                        analysis=analysis,
                        dom_publish_raw=dom_publish_raw,
                    )

            except PlaywrightTimeoutError:
                print("  打开详情页超时。")
                video["detail_error"] = "timeout"

            except Exception as e:
                print(f"  提取失败：{e}")
                video["detail_error"] = repr(e)

            finally:
                page.close()

        context.close()

    save_output(videos, processed_count=len(videos_to_process))


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="处理前多少条视频。默认 5。传 0 表示处理全部。",
    )

    parser.add_argument(
        "--wait-ms",
        type=int,
        default=8000,
        help="打开详情页后等待多久再提取，默认 8000ms。",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="保存网络响应预览和候选提取结果到 debug 目录。",
    )

    args = parser.parse_args()

    enrich_details(
        limit=args.limit,
        wait_ms=args.wait_ms,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()