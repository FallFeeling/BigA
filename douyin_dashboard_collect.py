import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import sync_playwright


ROOT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = ROOT_DIR / "runtime" / "douyin_profile"
DATA_DIR = ROOT_DIR / "data"
DASHBOARD_DATA_DIR = ROOT_DIR / "dashboard" / "public" / "data"
INTERACTION_HISTORY_PATH = DATA_DIR / "interaction_history.json"

AUTHOR_NAME = "模型先生"
AUTHOR_SEC_UID = "MS4wLjABAAAAK713M9d8PGNb_WiMYf7yKhOI5y60H4uELJK2guDjJT0"


def direct_value(row, *keys):
    if not isinstance(row, dict):
        return None
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def iter_dicts(value):
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(
                child for child in current.values() if isinstance(child, (dict, list))
            )
        elif isinstance(current, list):
            stack.extend(child for child in current if isinstance(child, (dict, list)))


def find_aweme(payload, video_id):
    if not isinstance(payload, (dict, list)):
        return None
    if isinstance(payload, dict):
        for key in ("aweme_detail", "aweme", "item"):
            candidate = payload.get(key)
            if isinstance(candidate, dict):
                candidate_id = str(
                    direct_value(candidate, "aweme_id", "aweme_id_str", "item_id") or ""
                )
                if not candidate_id or candidate_id == video_id:
                    return candidate
    for candidate in iter_dicts(payload):
        candidate_id = str(
            direct_value(candidate, "aweme_id", "aweme_id_str", "item_id") or ""
        )
        if candidate_id == video_id and isinstance(candidate.get("statistics"), dict):
            return candidate
    return None


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


def user_summary(row):
    user = direct_value(row, "user", "user_info") or {}
    return {
        "nickname": str(direct_value(user, "nickname", "name") or ""),
        "uid": str(direct_value(user, "uid", "user_id") or ""),
        "sec_uid": str(direct_value(user, "sec_uid", "sec_user_id") or ""),
        "avatar": image_url(direct_value(user, "avatar_thumb", "avatar")),
    }


def image_url(image):
    if isinstance(image, str):
        return image
    if not isinstance(image, dict):
        return ""
    urls = direct_value(image, "url_list", "urls")
    if isinstance(urls, list) and urls:
        return str(urls[0])
    return str(direct_value(image, "url", "uri") or "")


def timestamp_display(value):
    try:
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def normalize_comment(row):
    create_time = direct_value(row, "create_time", "createtime")
    return {
        "comment_id": str(direct_value(row, "cid", "comment_id", "id") or ""),
        "text": str(direct_value(row, "text", "content") or ""),
        "create_time": create_time,
        "create_time_display": timestamp_display(create_time),
        "digg_count": int(direct_value(row, "digg_count", "like_count") or 0),
        "reply_total": int(
            direct_value(row, "reply_comment_total", "reply_total", "reply_count") or 0
        ),
        "ip_label": str(direct_value(row, "ip_label", "ip_location") or ""),
        "user": user_summary(row),
    }


def is_author_reply(row):
    user = user_summary(row)
    label_type = direct_value(row, "label_type")
    label_text = str(direct_value(row, "label_text") or "")
    return (
        str(user.get("sec_uid") or "") == AUTHOR_SEC_UID
        or user.get("nickname") == AUTHOR_NAME
        or label_type == 1
        or label_text == "作者"
    )


def extract_interactions(comments):
    threads = []
    for parent in comments:
        if not isinstance(parent, dict):
            continue
        author_liked = bool(direct_value(parent, "is_author_digged"))
        embedded = direct_value(parent, "reply_comment", "reply_comments")
        if not isinstance(embedded, list):
            embedded = []
        author_replies = [
            normalize_comment(reply)
            for reply in embedded
            if isinstance(reply, dict) and is_author_reply(reply)
        ]
        if author_liked or author_replies:
            threads.append(
                {
                    "parent": normalize_comment(parent),
                    "author_liked": author_liked,
                    "author_replies": author_replies,
                }
            )
    return threads


def merge_interaction_threads(*groups):
    merged = {}
    for group in groups:
        for thread in group or []:
            parent = thread.get("parent") or {}
            parent_id = str(parent.get("comment_id") or "")
            if not parent_id:
                continue
            current = merged.setdefault(
                parent_id,
                {
                    "parent": parent,
                    "author_liked": False,
                    "author_replies": [],
                },
            )
            current["parent"] = parent or current["parent"]
            current["author_liked"] = bool(
                current["author_liked"] or thread.get("author_liked")
            )
            known_reply_ids = {
                reply.get("comment_id") for reply in current["author_replies"]
            }
            for reply in thread.get("author_replies") or []:
                reply_id = reply.get("comment_id")
                if reply_id in known_reply_ids:
                    continue
                current["author_replies"].append(reply)
                known_reply_ids.add(reply_id)
    return list(merged.values())


def load_interaction_history():
    if not INTERACTION_HISTORY_PATH.exists():
        return {}
    payload = json.loads(INTERACTION_HISTORY_PATH.read_text(encoding="utf-8"))
    return payload.get("videos") or {}


def preserve_interaction_history(results):
    history = load_interaction_history()
    for video in results:
        video_id = str(video.get("video_id") or "")
        if not video_id:
            continue
        old_threads = (history.get(video_id) or {}).get("interaction_threads")
        video["interaction_threads"] = merge_interaction_threads(
            old_threads, video.get("interaction_threads")
        )
        video["has_author_interaction"] = bool(video["interaction_threads"])
        if video["has_author_interaction"]:
            history[video_id] = {
                "interaction_threads": video["interaction_threads"],
                "last_preserved_at": datetime.now().isoformat(timespec="seconds"),
            }
    INTERACTION_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INTERACTION_HISTORY_PATH.write_text(
        json.dumps(
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "videos": history,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def parse_video_detail(aweme, fallback):
    if not isinstance(aweme, dict):
        aweme = {}
    statistics = direct_value(aweme, "statistics", "stats") or {}
    video = direct_value(aweme, "video") or {}
    chapter_info = direct_value(aweme, "recommend_chapter_info") or {}
    suggest_root = direct_value(aweme, "suggest_words") or {}
    suggest_groups = direct_value(suggest_root, "suggest_words") or []
    suggested_words = []
    for group in suggest_groups if isinstance(suggest_groups, list) else []:
        for item in direct_value(group, "words") or []:
            word = str(direct_value(item, "word", "text") or "").strip()
            if word and word not in suggested_words:
                suggested_words.append(word)
    create_time = direct_value(aweme, "create_time", "create_timestamp")
    published = timestamp_display(create_time) or str(fallback.get("published_at") or "")
    raw_description = str(direct_value(aweme, "desc", "description") or "").strip()
    chapter_abstract = str(direct_value(chapter_info, "chapter_abstract") or "").strip()
    return {
        "description": raw_description or chapter_abstract,
        "description_source": "caption" if raw_description else ("chapter_abstract" if chapter_abstract else ""),
        "suggested_words": suggested_words[:5],
        "published_at": published,
        "published_timestamp": int(create_time or fallback.get("published_timestamp") or 0),
        "like_count": int(
            direct_value(statistics, "digg_count", "like_count")
            or fallback.get("final_like_count")
            or fallback.get("home_like_count")
            or 0
        ),
        "comment_count": int(
            direct_value(statistics, "comment_count")
            or fallback.get("comment_count")
            or 0
        ),
        "duration_ms": int(direct_value(video, "duration") or 0),
    }


def replace_query_value(url, key, value):
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query[key] = [str(value)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def load_source_videos(limit):
    source_path = DATA_DIR / "author_videos_enriched_latest.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    return (payload.get("videos") or [])[:limit]


def build_output(results):
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "author": {"name": AUTHOR_NAME, "sec_uid": AUTHOR_SEC_UID},
        "scan_note": "每个视频读取首屏一级评论，检测 is_author_digged 与内嵌作者回复。",
        "video_count": len(results),
        "interaction_video_count": sum(
            1 for video in results if video["has_author_interaction"]
        ),
        "transcribed_video_count": sum(
            1 for video in results if video.get("transcript_status") == "complete"
        ),
        "comment_enriched_video_count": sum(
            1 for video in results if video.get("comments_status") == "complete"
        ),
        "videos": results,
    }


def save_output(results):
    preserve_interaction_history(results)
    output = build_output(results)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(output, ensure_ascii=False, indent=2)
    (DATA_DIR / "dashboard_videos_latest.json").write_text(text, encoding="utf-8")
    (DASHBOARD_DATA_DIR / "videos.json").write_text(text, encoding="utf-8")
    return output


def load_api_seeds(source_videos):
    detail_seed = str(source_videos[0].get("detail_source") or "")
    comment_payload = json.loads(
        (DATA_DIR / "first_video_comments_latest.json").read_text(encoding="utf-8")
    )
    comment_seed = ""
    for item in comment_payload.get("flat_items") or []:
        source_url = str(item.get("source_url") or "")
        if "comment/list" in source_url and "reply" not in source_url:
            comment_seed = source_url
            break
    if not detail_seed or not comment_seed:
        raise RuntimeError("缺少可复用的详情或评论接口种子 URL。")
    return detail_seed, comment_seed


def request_json(api, url, referer, timeout_ms):
    response = api.get(url, headers={"referer": referer}, timeout=timeout_ms)
    text = response.text()
    if response.status != 200 or not text:
        return None
    return json.loads(text)


def collect(args):
    source_videos = load_source_videos(args.limit)
    detail_seed, comment_seed = load_api_seeds(source_videos)
    results = []
    existing_by_id = {}
    existing_path = DATA_DIR / "dashboard_videos_latest.json"
    if existing_path.exists():
        existing_payload = json.loads(existing_path.read_text(encoding="utf-8"))
        existing_by_id = {
            str(item.get("video_id") or ""): item
            for item in existing_payload.get("videos") or []
        }

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        for index, source in enumerate(source_videos, 1):
            video_id = str(source.get("video_id") or "")
            print(f"[{index}/{len(source_videos)}] {video_id}", flush=True)
            detail_payload = None
            comment_payload = None
            errors = []

            try:
                detail_url = replace_query_value(detail_seed, "aweme_id", video_id)
                detail_payload = request_json(
                    context.request,
                    detail_url,
                    source["href"],
                    args.timeout_ms,
                )
            except Exception as exc:
                errors.append(f"detail: {type(exc).__name__}")

            try:
                comment_url = replace_query_value(comment_seed, "aweme_id", video_id)
                comment_url = replace_query_value(comment_url, "cursor", 0)
                comment_payload = request_json(
                    context.request,
                    comment_url,
                    source["href"],
                    args.timeout_ms,
                )
            except Exception as exc:
                errors.append(f"comment: {type(exc).__name__}")

            aweme = find_aweme(detail_payload, video_id)
            details = parse_video_detail(aweme, source)
            comments = direct_comments(comment_payload)
            detected_interactions = extract_interactions(comments)
            description = details["description"]
            description_source = details["description_source"]
            if not description:
                hint = str(source.get("title_hint") or "")
                if hint and hint != str(source.get("like_hint") or ""):
                    description = hint
                    description_source = "homepage"
            if not description and details["suggested_words"]:
                description = " · ".join(details["suggested_words"][:3])
                description_source = "topics"

            result = {
                    "order": index,
                    "video_id": video_id,
                    "url": source.get("href") or "",
                    "cover": source.get("cover") or "",
                    **details,
                    "description": description or "该视频未填写文案",
                    "description_source": description_source or "missing",
                    "interaction_threads": detected_interactions,
                    "has_author_interaction": bool(detected_interactions),
                    "scanned_top_comment_count": len(comments),
                    "collection_errors": errors,
                }
            old = existing_by_id.get(video_id) or {}
            result["interaction_threads"] = merge_interaction_threads(
                old.get("interaction_threads"), result["interaction_threads"]
            )
            result["has_author_interaction"] = bool(result["interaction_threads"])
            for key in (
                "transcript",
                "transcript_status",
                "comments",
                "comments_status",
                "comment_reply_count",
            ):
                if key in old:
                    result[key] = old[key]
            results.append(result)
            print(
                f"  文案={len(description)}字，首屏评论={len(comments)}，"
                f"互动线程={len(result['interaction_threads'])}",
                flush=True,
            )
            save_output(results)

        context.close()

    output = save_output(results)
    print(f"\n已生成：{DASHBOARD_DATA_DIR / 'videos.json'}")
    return output


def main():
    parser = argparse.ArgumentParser(description="为本地看板采集视频和博主互动数据。")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    collect(parser.parse_args())


if __name__ == "__main__":
    main()
