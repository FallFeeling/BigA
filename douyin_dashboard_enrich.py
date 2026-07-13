import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from douyin_dashboard_collect import (
    AUTHOR_NAME,
    AUTHOR_SEC_UID,
    DASHBOARD_DATA_DIR,
    DATA_DIR,
    PROFILE_DIR,
    direct_comments,
    direct_value,
    extract_interactions,
    find_aweme,
    image_url,
    is_author_reply,
    load_api_seeds,
    load_source_videos,
    merge_interaction_threads,
    normalize_comment,
    replace_query_value,
    request_json,
    save_output,
)


ROOT_DIR = Path(__file__).resolve().parent
TRANSCRIBER_LIB_DIR = ROOT_DIR / "runtime" / "transcriber"
WHISPER_MODEL_DIR = ROOT_DIR / "runtime" / "whisper_models"


def load_dashboard():
    path = DATA_DIR / "dashboard_videos_latest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_reply_seed():
    report_path = ROOT_DIR / "debug" / "first_comment_all_replies_latest.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    for page in payload.get("reply_pages") or []:
        url = str(page.get("url") or "")
        if "comment/list/reply" in url:
            return url
    raise RuntimeError("找不到可复用的评论回复接口种子 URL。")


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


def fetch_top_comments(api, seed, video, wanted, timeout_ms):
    rows = []
    cursor = 0
    seen_cursors = set()
    while len(rows) < wanted and cursor not in seen_cursors:
        seen_cursors.add(cursor)
        url = replace_query_value(seed, "aweme_id", video["video_id"])
        url = replace_query_value(url, "cursor", cursor)
        url = replace_query_value(url, "count", 20)
        payload = request_json(api, url, video["url"], timeout_ms)
        if not payload:
            break
        rows.extend(direct_comments(payload))
        rows = dedupe_raw_comments(rows)
        if payload.get("has_more") in (0, False):
            break
        next_cursor = payload.get("cursor")
        if next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    return rows[:wanted]


def fetch_all_replies(api, seed, video, parent, timeout_ms):
    parent_id = str(direct_value(parent, "cid", "comment_id", "id") or "")
    expected = int(direct_value(parent, "reply_comment_total", "reply_total") or 0)
    if not parent_id or expected <= 0:
        return []

    rows = []
    cursor = 0
    seen_cursors = set()
    for _ in range(200):
        if cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        url = replace_query_value(seed, "item_id", video["video_id"])
        url = replace_query_value(url, "comment_id", parent_id)
        url = replace_query_value(url, "cursor", cursor)
        url = replace_query_value(url, "count", 20)
        payload = request_json(api, url, video["url"], timeout_ms)
        if not payload:
            break
        rows.extend(direct_comments(payload))
        rows = dedupe_raw_comments(rows)
        if len(rows) >= expected or payload.get("has_more") in (0, False):
            break
        next_cursor = payload.get("cursor")
        if next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    return rows


def normalize_thread(parent, replies):
    normalized = normalize_comment(parent)
    normalized["author_liked"] = bool(direct_value(parent, "is_author_digged"))
    normalized["replies"] = [
        {**normalize_comment(reply), "is_author": is_author_reply(reply)}
        for reply in replies
    ]
    return normalized


def merge_interactions(raw_parents, normalized_threads):
    embedded = extract_interactions(raw_parents)
    by_parent = {thread["parent"]["comment_id"]: thread for thread in embedded}
    for thread in normalized_threads:
        author_replies = [reply for reply in thread["replies"] if reply["is_author"]]
        if not thread["author_liked"] and not author_replies:
            continue
        parent_id = thread["comment_id"]
        current = by_parent.setdefault(
            parent_id,
            {
                "parent": {key: value for key, value in thread.items() if key not in {"replies", "author_liked"}},
                "author_liked": thread["author_liked"],
                "author_replies": [],
            },
        )
        current["author_liked"] = current["author_liked"] or thread["author_liked"]
        known = {reply["comment_id"] for reply in current["author_replies"]}
        current["author_replies"].extend(
            {key: value for key, value in reply.items() if key != "is_author"}
            for reply in author_replies
            if reply["comment_id"] not in known
        )
    return list(by_parent.values())


def media_url(aweme):
    video = direct_value(aweme, "video") or {}
    for key in ("play_addr", "play_addr_265", "download_addr"):
        url = image_url(direct_value(video, key))
        if url:
            return url
    return ""


def seconds_label(value):
    seconds = max(0, int(round(float(value))))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def load_whisper_model(model_name):
    sys.path.insert(0, str(TRANSCRIBER_LIB_DIR))
    from faster_whisper import WhisperModel

    WHISPER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        cpu_threads=min(8, os.cpu_count() or 4),
        num_workers=1,
        download_root=str(WHISPER_MODEL_DIR),
    )
    model.dashboard_model_name = model_name
    return model


def transcribe_video(api, aweme, video, model, temp_dir, timeout_ms):
    url = media_url(aweme)
    if not url:
        raise RuntimeError("详情接口没有返回可下载的视频地址。")
    response = api.get(url, headers={"referer": video["url"]}, timeout=timeout_ms)
    body = response.body()
    if response.status != 200 or not body:
        raise RuntimeError(f"视频下载失败：HTTP {response.status}")

    media_path = Path(temp_dir) / f"{video['video_id']}.mp4"
    media_path.write_bytes(body)
    audio_path = Path(temp_dir) / f"{video['video_id']}.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )
    segments, info = model.transcribe(
        str(audio_path),
        language="zh",
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=True,
        temperature=0,
        initial_prompt=(
            "以下是中文财经、股票与投资口播。可能包含科技股、龙头、板块、估值、"
            "市盈率、业绩、财报、成交量、仓位、买点、卖点等术语。请完整逐句转录。"
        ),
    )
    rows = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        rows.append(
            {
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "start_display": seconds_label(segment.start),
                "end_display": seconds_label(segment.end),
                "text": text,
            }
        )
    return {
        "model": model.dashboard_model_name,
        "audio_preprocessing": "ffmpeg pcm_s16le 16kHz mono",
        "vad_filter": False,
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "text": "".join(row["text"] for row in rows),
        "segments": rows,
    }


def enrich(args):
    payload = load_dashboard()
    videos = payload.get("videos") or []
    detail_seed, comment_seed = load_api_seeds(load_source_videos(len(videos)))
    reply_seed = load_reply_seed()
    for video in videos:
        video.setdefault("transcript_status", "not_processed")
        video.setdefault("comments_status", "not_processed")

    model = None if args.skip_transcript else load_whisper_model(args.model)
    with sync_playwright() as playwright, tempfile.TemporaryDirectory(
        prefix="mrmodel-transcribe-", dir=str(ROOT_DIR / "runtime")
    ) as temp_dir:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False
        )
        start_index = max(0, args.start_order - 1)
        selected_videos = videos[start_index : start_index + args.limit]
        for index, video in enumerate(selected_videos, 1):
            print(
                f"[{index}/{len(selected_videos)}] #{video['order']} {video['video_id']}",
                flush=True,
            )
            detail_url = replace_query_value(detail_seed, "aweme_id", video["video_id"])
            detail_payload = request_json(
                context.request, detail_url, video["url"], args.timeout_ms
            )
            aweme = find_aweme(detail_payload, video["video_id"])

            comments_ready = (
                video.get("comments_status") == "complete"
                and len(video.get("comments") or []) >= args.comment_count
            )
            if not args.skip_comments and not comments_ready:
                try:
                    raw_comments = fetch_top_comments(
                        context.request,
                        comment_seed,
                        video,
                        args.comment_count,
                        args.timeout_ms,
                    )
                    normalized_threads = []
                    reply_count = 0
                    for parent_index, parent in enumerate(raw_comments, 1):
                        replies = fetch_all_replies(
                            context.request, reply_seed, video, parent, args.timeout_ms
                        )
                        reply_count += len(replies)
                        normalized_threads.append(normalize_thread(parent, replies))
                        print(
                            f"  评论 {parent_index}/{len(raw_comments)}：回复 {len(replies)}",
                            flush=True,
                        )
                    video["comments"] = normalized_threads
                    video["comments_status"] = "complete"
                    video["comment_reply_count"] = reply_count
                    detected_interactions = merge_interactions(
                        raw_comments, normalized_threads
                    )
                    video["interaction_threads"] = merge_interaction_threads(
                        video.get("interaction_threads"), detected_interactions
                    )
                    video["has_author_interaction"] = bool(video["interaction_threads"])
                except Exception as exc:
                    video["comments_status"] = "error"
                    video["comments_error"] = f"{type(exc).__name__}: {exc}"

            if args.interaction_scan_count > 0:
                try:
                    interaction_candidates = fetch_top_comments(
                        context.request,
                        comment_seed,
                        video,
                        args.interaction_scan_count,
                        args.timeout_ms,
                    )
                    newly_detected = extract_interactions(interaction_candidates)
                    video["interaction_threads"] = merge_interaction_threads(
                        video.get("interaction_threads"), newly_detected
                    )
                    video["has_author_interaction"] = bool(video["interaction_threads"])
                    print(
                        f"  互动扫描 {len(interaction_candidates)} 条："
                        f"新增候选 {len(newly_detected)}，累计 {len(video['interaction_threads'])}",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"  互动扫描失败：{type(exc).__name__}: {exc}", flush=True)

            transcript_ready = (
                video.get("transcript_status") == "complete"
                and not args.force_transcript
            )
            if not args.skip_transcript and not transcript_ready:
                try:
                    video["transcript"] = transcribe_video(
                        context.request,
                        aweme,
                        video,
                        model,
                        temp_dir,
                        args.media_timeout_ms,
                    )
                    video["transcript_status"] = "complete"
                    print(
                        f"  转录完成：{len(video['transcript']['segments'])} 段，"
                        f"{len(video['transcript']['text'])} 字",
                        flush=True,
                    )
                except Exception as exc:
                    video["transcript_status"] = "error"
                    video["transcript_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"  转录失败：{video['transcript_error']}", flush=True)

            save_output(videos)
        context.close()

    output = save_output(videos)
    print(f"\n已更新：{DASHBOARD_DATA_DIR / 'videos.json'}")
    return output


def main():
    parser = argparse.ArgumentParser(description="补充看板评论回复和视频转录。")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--start-order", type=int, default=1)
    parser.add_argument("--comment-count", type=int, default=20)
    parser.add_argument("--model", default="small")
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    parser.add_argument("--media-timeout-ms", type=int, default=120_000)
    parser.add_argument("--interaction-scan-count", type=int, default=0)
    parser.add_argument("--skip-comments", action="store_true")
    parser.add_argument("--skip-transcript", action="store_true")
    parser.add_argument("--force-transcript", action="store_true")
    enrich(parser.parse_args())


if __name__ == "__main__":
    main()
