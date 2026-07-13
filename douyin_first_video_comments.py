import argparse
import csv
import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


ROOT_DIR = Path(__file__).resolve().parent

PROFILE_DIR = ROOT_DIR / "runtime" / "douyin_profile"
DATA_DIR = ROOT_DIR / "data"
DEBUG_DIR = ROOT_DIR / "debug"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def clean_text(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def norm_key(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def normalize_parent_id(value):
    if value is None:
        return ""

    s = clean_text(value)

    if s.lower() in {"", "0", "0.0", "none", "null", "undefined", "-1"}:
        return ""

    return s


def parse_count(value):
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


def format_timestamp(value):
    if value is None:
        return ""

    try:
        ts = int(float(value))
    except Exception:
        return ""

    if ts <= 0:
        return ""

    if ts > 10_000_000_000:
        ts = ts // 1000

    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def launch_context(playwright):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        accept_downloads=False,
    )


def load_first_video():
    input_path = DATA_DIR / "author_videos_latest.json"

    if not input_path.exists():
        raise FileNotFoundError(
            f"找不到 {input_path}\n"
            f"请先运行：python douyin_author_videos.py --scan --debug"
        )

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    videos = payload.get("videos", [])

    if not videos:
        raise RuntimeError(
            "author_videos_latest.json 里没有视频。\n"
            "请先确认 douyin_author_videos.py --scan 能正常抓到主页首屏视频。"
        )

    first_video = videos[0]

    href = first_video.get("href", "")
    video_id = first_video.get("video_id", "")

    if not href:
        raise RuntimeError("第一条视频没有 href 字段，无法打开详情页。")

    return first_video, video_id, href


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


COMMENT_ID_KEYS = {
    "cid",
    "commentid",
    "commentidstr",
    "commentidlong",
    "id",
}

TEXT_KEYS = {
    "text",
    "content",
    "commenttext",
    "commentcontent",
}

TIME_KEYS = {
    "createtime",
    "createat",
    "replytime",
}

LIKE_KEYS = {
    "diggcount",
    "diggcountstr",
    "likecount",
    "likecountstr",
}

REPLY_TOTAL_KEYS = {
    "replycommenttotal",
    "replytotal",
    "replycount",
    "replycommentcount",
}

PARENT_ID_KEYS = {
    "parentid",
    "parentcommentid",
    "replyid",
    "replycommentid",
    "rootcommentid",
    "rootid",
}

IP_KEYS = {
    "iplabel",
    "iplocation",
}

USER_KEYS = {
    "user",
    "userinfo",
    "userinfomap",
    "author",
}

REPLY_TO_USER_KEYS = {
    "replytouser",
    "replyuser",
    "touser",
}

USER_ID_KEYS = {
    "uid",
    "userid",
    "useruniqueid",
    "shortid",
    "uniqueid",
}

USER_SEC_UID_KEYS = {
    "secuid",
    "secuserid",
}

USER_NICKNAME_KEYS = {
    "nickname",
    "nick",
    "username",
    "name",
}


def get_direct_by_keys(d, keys):
    if not isinstance(d, dict):
        return None

    for k, v in d.items():
        if norm_key(k) in keys:
            return v

    return None


def get_nested_dict_by_keys(d, keys):
    if not isinstance(d, dict):
        return {}

    for k, v in d.items():
        if norm_key(k) in keys and isinstance(v, dict):
            return v

    return {}


def extract_user_info(d):
    user = get_nested_dict_by_keys(d, USER_KEYS)

    if not user:
        user = d

    nickname = get_direct_by_keys(user, USER_NICKNAME_KEYS)
    uid = get_direct_by_keys(user, USER_ID_KEYS)
    sec_uid = get_direct_by_keys(user, USER_SEC_UID_KEYS)

    return {
        "user_nickname": clean_text(nickname) if nickname is not None else "",
        "user_uid": str(uid) if uid is not None else "",
        "user_sec_uid": str(sec_uid) if sec_uid is not None else "",
    }


def extract_reply_to_user_info(d):
    reply_to_user = get_nested_dict_by_keys(d, REPLY_TO_USER_KEYS)

    if not reply_to_user:
        return {
            "reply_to_user_nickname": "",
            "reply_to_user_uid": "",
            "reply_to_user_sec_uid": "",
        }

    nickname = get_direct_by_keys(reply_to_user, USER_NICKNAME_KEYS)
    uid = get_direct_by_keys(reply_to_user, USER_ID_KEYS)
    sec_uid = get_direct_by_keys(reply_to_user, USER_SEC_UID_KEYS)

    return {
        "reply_to_user_nickname": clean_text(nickname) if nickname is not None else "",
        "reply_to_user_uid": str(uid) if uid is not None else "",
        "reply_to_user_sec_uid": str(sec_uid) if sec_uid is not None else "",
    }


def get_parent_comment_id_from_url(url):
    try:
        query = parse_qs(urlparse(url).query)
    except Exception:
        return ""

    possible_keys = {
        "commentid",
        "cid",
        "parentcommentid",
        "rootcommentid",
        "replyid",
    }

    for k, values in query.items():
        if norm_key(k) in possible_keys and values:
            return normalize_parent_id(values[0])

    return ""


def is_reply_response_url(url):
    lower = (url or "").lower()
    return "comment" in lower and "reply" in lower


def extract_comment_candidate(d, source_url, source_method):
    if not isinstance(d, dict):
        return None

    text = get_direct_by_keys(d, TEXT_KEYS)

    if not isinstance(text, str):
        return None

    text = clean_text(text)

    if not text:
        return None

    if len(text) > 2000:
        return None

    comment_id = get_direct_by_keys(d, COMMENT_ID_KEYS)
    create_time = get_direct_by_keys(d, TIME_KEYS)
    digg_count = get_direct_by_keys(d, LIKE_KEYS)
    reply_total = get_direct_by_keys(d, REPLY_TOTAL_KEYS)
    ip_label = get_direct_by_keys(d, IP_KEYS)

    user_info = extract_user_info(d)
    reply_to_info = extract_reply_to_user_info(d)

    parent_from_obj = normalize_parent_id(get_direct_by_keys(d, PARENT_ID_KEYS))
    parent_from_url = get_parent_comment_id_from_url(source_url)

    source_is_reply = is_reply_response_url(source_url)

    parent_comment_id = parent_from_obj

    if source_is_reply and not parent_comment_id:
        parent_comment_id = parent_from_url

    if comment_id is not None and parent_comment_id == str(comment_id):
        parent_comment_id = ""

    item_type = "reply" if source_is_reply or parent_comment_id else "comment"

    score = 0

    if comment_id is not None:
        score += 4

    if user_info["user_nickname"] or user_info["user_uid"] or user_info["user_sec_uid"]:
        score += 3

    if create_time is not None:
        score += 2

    if digg_count is not None:
        score += 1

    if reply_total is not None:
        score += 1

    if score < 5:
        return None

    return {
        "item_type": item_type,
        "comment_id": str(comment_id) if comment_id is not None else "",
        "parent_comment_id": parent_comment_id,
        "text": text,
        "create_time": int(create_time) if str(create_time).isdigit() else None,
        "create_time_display": format_timestamp(create_time),
        "digg_count": parse_count(digg_count),
        "reply_comment_total": parse_count(reply_total),
        "ip_label": clean_text(ip_label) if ip_label is not None else "",
        "user_nickname": user_info["user_nickname"],
        "user_uid": user_info["user_uid"],
        "user_sec_uid": user_info["user_sec_uid"],
        "reply_to_user_nickname": reply_to_info["reply_to_user_nickname"],
        "reply_to_user_uid": reply_to_info["reply_to_user_uid"],
        "reply_to_user_sec_uid": reply_to_info["reply_to_user_sec_uid"],
        "source_method": source_method,
        "source_url": source_url,
    }


def extract_items_from_json(data, source_url):
    items = []

    for d in iter_dicts(data):
        cand = extract_comment_candidate(
            d=d,
            source_url=source_url,
            source_method="network_json",
        )

        if cand:
            items.append(cand)

    return items


def dedupe_items(items):
    seen = {}
    rows = []

    for c in items:
        key = c.get("comment_id")

        if not key:
            key = (
                f"{c.get('item_type', '')}|"
                f"{c.get('parent_comment_id', '')}|"
                f"{c.get('user_uid', '')}|"
                f"{c.get('user_nickname', '')}|"
                f"{c.get('create_time', '')}|"
                f"{c.get('text', '')}"
            )

        if key in seen:
            old = seen[key]

            if old.get("item_type") == "comment" and c.get("item_type") == "reply":
                old["item_type"] = "reply"
                old["parent_comment_id"] = c.get("parent_comment_id", old.get("parent_comment_id", ""))

            continue

        seen[key] = c
        rows.append(c)

    for idx, c in enumerate(rows, 1):
        c["order_index"] = idx

    return rows


def build_threads(items):
    top_comments = []
    replies_by_parent = {}
    orphan_replies = []

    for item in items:
        if item.get("item_type") == "reply":
            parent_id = item.get("parent_comment_id", "")

            if parent_id:
                replies_by_parent.setdefault(parent_id, []).append(item)
            else:
                orphan_replies.append(item)
        else:
            top_comments.append(item)

    threads = []

    for c in top_comments:
        comment_id = c.get("comment_id", "")
        thread = dict(c)
        thread["replies"] = replies_by_parent.get(comment_id, [])
        thread["loaded_reply_count"] = len(thread["replies"])
        threads.append(thread)

    return {
        "threads": threads,
        "orphan_replies": orphan_replies,
    }


def get_visible_reply_expand_candidates(page):
    try:
        return page.evaluate(
            """
            () => {
                function cleanText(s) {
                    return (s || "")
                        .replace(/\\u200b/g, "")
                        .replace(/\\s+/g, " ")
                        .trim();
                }

                function visible(el) {
                    if (!el) return false;

                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.display === "none") return false;
                    if (style.visibility === "hidden") return false;
                    if (Number(style.opacity) === 0) return false;

                    const r = el.getBoundingClientRect();

                    if (r.width < 10 || r.height < 8) return false;
                    if (r.bottom <= 0 || r.right <= 0) return false;
                    if (r.top >= window.innerHeight || r.left >= window.innerWidth) return false;

                    return true;
                }

                function isReplyExpandText(text) {
                    if (!text) return false;

                    const t = text.replace(/\\s+/g, "");

                    if (t.length > 100) return false;
                    if (t === "回复") return false;
                    if (t === "评论") return false;
                    if (t.includes("发表评论")) return false;
                    if (t.includes("写评论")) return false;
                    if (t.includes("加载更多评论")) return false;
                    if (t.includes("收起")) return false;

                    const patterns = [
                        /^展开\\d*条?回复$/,
                        /^展开全部\\d*条?回复$/,
                        /^展开更多回复$/,
                        /^查看\\d*条?回复$/,
                        /^查看全部\\d*条?回复$/,
                        /^查看更多\\d*条?回复$/,
                        /^共\\d+条?回复$/,
                        /^还有\\d+条?回复$/,
                        /^更多回复$/,
                        /^展开.*回复$/,
                        /^查看.*回复$/,
                        /^还有.*回复$/,
                    ];

                    return patterns.some(p => p.test(t));
                }

                function rectObj(r) {
                    return {
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        top: Math.round(r.top),
                        left: Math.round(r.left),
                        width: Math.round(r.width),
                        height: Math.round(r.height)
                    };
                }

                const nodes = Array.from(document.querySelectorAll("button, a, span, div, p"));
                const rows = [];

                for (const el of nodes) {
                    if (!visible(el)) continue;

                    const r = el.getBoundingClientRect();
                    const text = cleanText(el.innerText || el.textContent || "");

                    if (!isReplyExpandText(text)) continue;

                    rows.push({
                        text,
                        tag: el.tagName,
                        rect: rectObj(r)
                    });
                }

                return rows.slice(0, 50);
            }
            """
        )
    except Exception:
        return []


def click_visible_reply_expand_buttons(page, max_clicks=20, wait_after_click_ms=1000):
    clicked = []
    last_candidates = []

    for _ in range(max_clicks):
        result = page.evaluate(
            """
            () => {
                function cleanText(s) {
                    return (s || "")
                        .replace(/\\u200b/g, "")
                        .replace(/\\s+/g, " ")
                        .trim();
                }

                function visible(el) {
                    if (!el) return false;

                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.display === "none") return false;
                    if (style.visibility === "hidden") return false;
                    if (Number(style.opacity) === 0) return false;

                    const r = el.getBoundingClientRect();

                    if (r.width < 10 || r.height < 8) return false;
                    if (r.bottom <= 0 || r.right <= 0) return false;
                    if (r.top >= window.innerHeight || r.left >= window.innerWidth) return false;

                    return true;
                }

                function isReplyExpandText(text) {
                    if (!text) return false;

                    const t = text.replace(/\\s+/g, "");

                    if (t.length > 100) return false;
                    if (t === "回复") return false;
                    if (t === "评论") return false;
                    if (t.includes("发表评论")) return false;
                    if (t.includes("写评论")) return false;
                    if (t.includes("加载更多评论")) return false;
                    if (t.includes("收起")) return false;

                    const patterns = [
                        /^展开\\d*条?回复$/,
                        /^展开全部\\d*条?回复$/,
                        /^展开更多回复$/,
                        /^查看\\d*条?回复$/,
                        /^查看全部\\d*条?回复$/,
                        /^查看更多\\d*条?回复$/,
                        /^共\\d+条?回复$/,
                        /^还有\\d+条?回复$/,
                        /^更多回复$/,
                        /^展开.*回复$/,
                        /^查看.*回复$/,
                        /^还有.*回复$/,
                    ];

                    return patterns.some(p => p.test(t));
                }

                function clickableScore(el) {
                    if (!el) return 0;

                    const tag = (el.tagName || "").toLowerCase();
                    const role = el.getAttribute("role") || "";
                    const style = window.getComputedStyle(el);
                    const text = cleanText(el.innerText || el.textContent || "");

                    let score = 0;

                    if (tag === "button" || tag === "a") score += 5;
                    if (role === "button") score += 4;
                    if (style && style.cursor === "pointer") score += 3;
                    if (el.onclick) score += 2;
                    if (isReplyExpandText(text)) score += 3;

                    return score;
                }

                function findClickableTarget(el) {
                    let best = el;
                    let bestScore = clickableScore(el);

                    let node = el;

                    for (let depth = 0; depth < 6 && node; depth++) {
                        if (!visible(node)) {
                            node = node.parentElement;
                            continue;
                        }

                        const score = clickableScore(node);

                        if (score > bestScore) {
                            best = node;
                            bestScore = score;
                        }

                        node = node.parentElement;
                    }

                    return best;
                }

                function rectObj(r) {
                    return {
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        top: Math.round(r.top),
                        left: Math.round(r.left),
                        width: Math.round(r.width),
                        height: Math.round(r.height)
                    };
                }

                const nodes = Array.from(document.querySelectorAll("button, a, span, div, p"));
                const debugCandidates = [];

                for (const el of nodes) {
                    if (el.dataset.mrModelReplyClicked === "1") continue;
                    if (!visible(el)) continue;

                    const text = cleanText(el.innerText || el.textContent || "");

                    if (!isReplyExpandText(text)) continue;

                    const target = findClickableTarget(el);
                    const r = target.getBoundingClientRect();

                    debugCandidates.push({
                        text,
                        tag: el.tagName,
                        target_tag: target.tagName,
                        target_text: cleanText(target.innerText || target.textContent || "").slice(0, 120),
                        rect: rectObj(r)
                    });

                    el.dataset.mrModelReplyClicked = "1";
                    target.dataset.mrModelReplyClicked = "1";

                    return {
                        found: true,
                        text,
                        tag: el.tagName,
                        target_tag: target.tagName,
                        target_text: cleanText(target.innerText || target.textContent || "").slice(0, 120),
                        x: Math.round(r.left + r.width / 2),
                        y: Math.round(r.top + r.height / 2),
                        rect: rectObj(r)
                    };
                }

                return {
                    found: false,
                    candidates: debugCandidates
                };
            }
            """
        )

        if not result.get("found"):
            last_candidates = result.get("candidates", [])
            break

        x = result.get("x")
        y = result.get("y")

        if x is None or y is None:
            break

        print(f"  准备点击展开回复：{result.get('text', '')} @ ({x}, {y})")

        page.mouse.click(x, y)
        page.wait_for_timeout(wait_after_click_ms)

        clicked.append(result)

    return clicked, last_candidates


def extract_visible_comment_blocks_from_dom(page):
    try:
        blocks = page.evaluate(
            """
            () => {
                function cleanText(s) {
                    return (s || "")
                        .replace(/\\u200b/g, "")
                        .replace(/\\s+/g, " ")
                        .trim();
                }

                function visible(el) {
                    if (!el) return false;

                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.display === "none") return false;
                    if (style.visibility === "hidden") return false;
                    if (Number(style.opacity) === 0) return false;

                    const r = el.getBoundingClientRect();

                    if (r.width < 120 || r.height < 20) return false;
                    if (r.bottom <= 0 || r.right <= 0) return false;
                    if (r.top >= window.innerHeight || r.left >= window.innerWidth) return false;

                    return true;
                }

                function rectObj(r) {
                    return {
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        top: Math.round(r.top),
                        left: Math.round(r.left),
                        bottom: Math.round(r.bottom),
                        right: Math.round(r.right),
                        width: Math.round(r.width),
                        height: Math.round(r.height)
                    };
                }

                const vw = window.innerWidth || document.documentElement.clientWidth;
                const candidates = [];
                const nodes = Array.from(document.querySelectorAll("div"));

                for (const el of nodes) {
                    if (!visible(el)) continue;

                    const r = el.getBoundingClientRect();
                    const text = cleanText(el.innerText || "");

                    if (!text) continue;
                    if (text.length < 3 || text.length > 800) continue;

                    if (r.left < vw * 0.35) continue;

                    const looksComment =
                        text.includes("回复") ||
                        text.includes("IP属地") ||
                        /\\d+分钟前|\\d+小时前|昨天|刚刚|\\d{4}-\\d{1,2}-\\d{1,2}|\\d{1,2}月\\d{1,2}日/.test(text);

                    if (!looksComment) continue;

                    if (text.includes("用户协议") || text.includes("隐私政策")) continue;
                    if (text.includes("相关推荐")) continue;

                    candidates.push({
                        raw_text: text,
                        rect: rectObj(r)
                    });
                }

                const seen = new Set();
                const rows = [];

                for (const c of candidates) {
                    if (seen.has(c.raw_text)) continue;
                    seen.add(c.raw_text);
                    rows.push(c);
                }

                return rows.slice(0, 120);
            }
            """
        )
    except Exception:
        blocks = []

    for idx, b in enumerate(blocks, 1):
        b["order_index"] = idx

    return blocks


def save_output(payload, flat_items):
    now = datetime.now().strftime("%Y%m%d_%H%M%S")

    latest_json = DATA_DIR / "first_video_comments_latest.json"
    timestamp_json = DATA_DIR / f"first_video_comments_{now}.json"

    latest_csv = DATA_DIR / "first_video_comments_latest.csv"
    timestamp_csv = DATA_DIR / f"first_video_comments_{now}.csv"

    for path in [latest_json, timestamp_json]:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    fieldnames = [
        "order_index",
        "item_type",
        "comment_id",
        "parent_comment_id",
        "user_nickname",
        "user_uid",
        "user_sec_uid",
        "reply_to_user_nickname",
        "reply_to_user_uid",
        "reply_to_user_sec_uid",
        "text",
        "create_time_display",
        "create_time",
        "digg_count",
        "reply_comment_total",
        "ip_label",
        "source_method",
        "source_url",
    ]

    for path in [latest_csv, timestamp_csv]:
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for c in flat_items:
                row = {k: c.get(k, "") for k in fieldnames}
                writer.writerow(row)

    print(f"\n已保存 JSON：{latest_json}")
    print(f"已保存 CSV ：{latest_csv}")
    print(f"本次归档 JSON：{timestamp_json}")
    print(f"本次归档 CSV ：{timestamp_csv}")


def save_debug(video_id, page, captured, flat_items, dom_blocks, expand_candidates_before, clicked_buttons, last_candidates):
    now = datetime.now().strftime("%Y%m%d_%H%M%S")

    latest_json = DEBUG_DIR / "first_video_comments_debug_latest.json"
    timestamp_json = DEBUG_DIR / f"first_video_comments_debug_{now}.json"

    latest_png = DEBUG_DIR / "first_video_comments_screenshot_latest.png"
    timestamp_png = DEBUG_DIR / f"first_video_comments_screenshot_{now}.png"

    debug_payload = {
        "debug_at": datetime.now().isoformat(timespec="seconds"),
        "video_id": video_id,
        "page_url": page.url,
        "expand_candidates_before_count": len(expand_candidates_before),
        "expand_candidates_before": expand_candidates_before,
        "clicked_reply_expand_count": len(clicked_buttons),
        "clicked_reply_expand_buttons": clicked_buttons,
        "last_candidates_when_stop": last_candidates,
        "captured_response_count": len(captured),
        "extracted_item_count": len(flat_items),
        "dom_block_count": len(dom_blocks),
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
        "flat_items": flat_items,
        "dom_blocks": dom_blocks,
    }

    for path in [latest_json, timestamp_json]:
        path.write_text(
            json.dumps(debug_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    for path in [latest_png, timestamp_png]:
        page.screenshot(path=str(path), full_page=False)

    print(f"\n[debug] 调试 JSON：{latest_json}")
    print(f"[debug] 当前截图：{latest_png}")


def read_first_video_comments(
    wait_ms=8000,
    expand_replies=True,
    max_expand_clicks=20,
    expand_wait_ms=1200,
    keep_open=False,
    debug=False,
):
    first_video, video_id, href = load_first_video()

    captured = []

    with sync_playwright() as p:
        context = launch_context(p)
        page = context.new_page()

        def on_response(response):
            try:
                req = response.request
                resource_type = req.resource_type
                url = response.url
                content_type = response.headers.get("content-type", "")

                if resource_type not in ["xhr", "fetch"]:
                    return

                if "douyin" not in url:
                    return

                lower_url = url.lower()

                if "comment" not in lower_url:
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

        print("\n正在打开第一条视频详情页...")
        print(f"video_id: {video_id}")
        print(f"href    : {href}")

        try:
            page.goto(href, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            print("页面打开超时。请观察浏览器是否出现登录/验证/网络问题。")

        page.wait_for_timeout(wait_ms)

        expand_candidates_before = get_visible_reply_expand_candidates(page)

        print(f"\n当前可见的展开回复候选数量：{len(expand_candidates_before)}")
        for idx, c in enumerate(expand_candidates_before[:10], 1):
            print(f"  候选 {idx}: {c.get('text', '')} | {c.get('tag', '')} | {c.get('rect', {})}")

        clicked_buttons = []
        last_candidates = []

        if expand_replies:
            print("\n开始点击当前可见的“展开回复/查看回复/更多回复”按钮...")
            clicked_buttons, last_candidates = click_visible_reply_expand_buttons(
                page=page,
                max_clicks=max_expand_clicks,
                wait_after_click_ms=expand_wait_ms,
            )
            print(f"已点击展开回复按钮数量：{len(clicked_buttons)}")
            page.wait_for_timeout(expand_wait_ms)

        flat_items = []

        for item in captured:
            data = try_json_loads(item.get("text", ""))

            if data is None:
                continue

            flat_items.extend(
                extract_items_from_json(
                    data=data,
                    source_url=item.get("url", ""),
                )
            )

        flat_items = dedupe_items(flat_items)
        thread_result = build_threads(flat_items)

        top_comment_count = sum(1 for x in flat_items if x.get("item_type") == "comment")
        reply_count = sum(1 for x in flat_items if x.get("item_type") == "reply")

        dom_blocks = extract_visible_comment_blocks_from_dom(page)

        payload = {
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "first_video_initial_comments_with_visible_reply_expand_no_scroll",
            "note": (
                "读取第一条视频详情页首次自然加载出来的评论；"
                "会点击当前可见的展开回复/查看回复/更多回复按钮；"
                "不滚动评论区，不点击加载更多评论。"
            ),
            "source_video": first_video,
            "video_id": video_id,
            "href": href,
            "final_url": page.url,
            "wait_ms": wait_ms,
            "expand_replies": expand_replies,
            "max_expand_clicks": max_expand_clicks,
            "expand_candidates_before_count": len(expand_candidates_before),
            "expand_candidates_before": expand_candidates_before,
            "clicked_reply_expand_count": len(clicked_buttons),
            "clicked_reply_expand_buttons": clicked_buttons,
            "captured_comment_response_count": len(captured),
            "flat_item_count": len(flat_items),
            "top_comment_count": top_comment_count,
            "reply_count": reply_count,
            "flat_items": flat_items,
            "threads": thread_result["threads"],
            "orphan_replies": thread_result["orphan_replies"],
            "dom_fallback_blocks_count": len(dom_blocks),
            "dom_fallback_blocks": dom_blocks,
        }

        print("\n读取结果：")
        print(f"  捕获到评论相关响应数：{len(captured)}")
        print(f"  展开回复候选数量      ：{len(expand_candidates_before)}")
        print(f"  点击展开回复按钮数    ：{len(clicked_buttons)}")
        print(f"  提取到总条目数        ：{len(flat_items)}")
        print(f"  一级评论数            ：{top_comment_count}")
        print(f"  回复数                ：{reply_count}")
        print(f"  DOM 兜底文本块数      ：{len(dom_blocks)}")

        print("\n条目预览：")
        for c in flat_items[:30]:
            prefix = "回复" if c.get("item_type") == "reply" else "评论"
            parent = c.get("parent_comment_id", "")
            parent_text = f" parent={parent}" if parent else ""

            print(f"\n[{c['order_index']}] {prefix}{parent_text} | {c.get('user_nickname', '')}")
            print(f"    时间：{c.get('create_time_display', '')}")
            print(f"    点赞：{c.get('digg_count', '')}，回复数：{c.get('reply_comment_total', '')}")
            print(f"    内容：{c.get('text', '')}")

        save_output(payload, flat_items)

        if debug:
            save_debug(
                video_id=video_id,
                page=page,
                captured=captured,
                flat_items=flat_items,
                dom_blocks=dom_blocks,
                expand_candidates_before=expand_candidates_before,
                clicked_buttons=clicked_buttons,
                last_candidates=last_candidates,
            )

        if keep_open:
            input("\nkeep_open=True，按 Enter 后关闭浏览器...")

        context.close()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--wait-ms",
        type=int,
        default=8000,
        help="打开视频详情页后等待首次评论区自然加载的时间，默认 8000ms。不滚动页面。",
    )

    parser.add_argument(
        "--no-expand-replies",
        action="store_true",
        help="不点击展开回复按钮。默认会点击当前可见的展开回复按钮。",
    )

    parser.add_argument(
        "--max-expand-clicks",
        type=int,
        default=20,
        help="最多点击多少个当前可见的展开回复按钮，默认 20。",
    )

    parser.add_argument(
        "--expand-wait-ms",
        type=int,
        default=1200,
        help="每次点击展开回复后等待多久，默认 1200ms。",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="保存评论响应预览、DOM 兜底文本、当前截图到 debug 目录。",
    )

    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="读取结束后不立刻关闭浏览器，方便观察页面。",
    )

    args = parser.parse_args()

    read_first_video_comments(
        wait_ms=args.wait_ms,
        expand_replies=not args.no_expand_replies,
        max_expand_clicks=args.max_expand_clicks,
        expand_wait_ms=args.expand_wait_ms,
        keep_open=args.keep_open,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()