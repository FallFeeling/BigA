"use client";

import { useState } from "react";
import { formatCount, formatDuration, shortDate } from "../format";
import type { VideoItem } from "../types";

type Props = {
  video: VideoItem;
  onOpen: (video: VideoItem) => void;
};

export function VideoCard({ video, onOpen }: Props) {
  const duration = formatDuration(video.duration_ms);
  const [coverFailed, setCoverFailed] = useState(false);
  const showPlaceholder = Boolean(video.is_deleted || coverFailed || !video.cover);

  return (
    <button className={video.is_deleted ? "video-card deleted" : "video-card"} type="button" onClick={() => onOpen(video)}>
      <span className="cover-wrap">
        {showPlaceholder ? (
          <span className="deleted-cover">{video.is_deleted ? "已删除" : "封面不可用"}</span>
        ) : (
          <img
            className="video-cover"
            src={video.cover}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setCoverFailed(true)}
          />
        )}
        <span className="cover-gradient" />
        <span className="video-rank">{String(video.order).padStart(2, "0")}</span>
        {duration && <span className="video-duration">{duration}</span>}
        {video.has_author_interaction && (
          <span className="interaction-badge">
            <span className="badge-dot" />博主互动
          </span>
        )}
      </span>

      <span className="card-body">
        <span className="video-meta">
          <span>{shortDate(video.published_at)}</span>
          <span className="meta-separator" />
          <span>赞 {formatCount(video.like_count)}</span>
          <span className="meta-separator" />
          <span>评 {formatCount(video.comment_count)}</span>
        </span>
      </span>
    </button>
  );
}
