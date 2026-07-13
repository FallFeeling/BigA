"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { VideoCard } from "./components/VideoCard";
import { VideoModal } from "./components/VideoModal";
import type { DashboardData, VideoItem } from "./types";

type Filter = "all" | "interaction";

export function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selectedVideo, setSelectedVideo] = useState<VideoItem | null>(null);

  useEffect(() => {
    const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
    // GitHub Pages 对静态 JSON 默认缓存约 10 分钟。监控数据需要每次打开都取最新版本。
    fetch(`${basePath}/data/videos.json?v=${Date.now()}`, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error("数据文件读取失败");
        return response.json();
      })
      .then(setData)
      .catch((reason: Error) => setError(reason.message));
  }, []);

  const closeModal = useCallback(() => setSelectedVideo(null), []);
  const videos = useMemo(() => {
    if (!data) return [];
    return filter === "interaction"
      ? data.videos.filter((video) => video.has_author_interaction)
      : data.videos;
  }, [data, filter]);

  if (error) {
    return <main className="state-page"><h1>看板加载失败</h1><p>{error}</p></main>;
  }

  if (!data) {
    return <main className="state-page"><div className="loading-ring" /><p>正在载入视频数据…</p></main>;
  }

  return (
    <main className="dashboard-shell">
      <header className="dashboard-header">
        <div className="brand-block">
          <span className="eyebrow">DOUYIN ARCHIVE · MRMODEL</span>
          <h1>模型先生<span>视频互动看板</span></h1>
          <p>视频永久存档 · 新视频 48 小时互动监控</p>
        </div>

        <div className="summary-strip">
          <div><strong>{data.video_count}</strong><span>视频</span></div>
          <div><strong>{data.interaction_video_count}</strong><span>检测到互动</span></div>
        </div>
      </header>

      <section className="toolbar">
        <div className="filter-tabs" role="group" aria-label="筛选视频">
          <button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部视频</button>
          <button className={filter === "interaction" ? "active" : ""} onClick={() => setFilter("interaction")}>
            博主互动 <span>{data.interaction_video_count}</span>
          </button>
        </div>
        <p>数据更新于 {data.generated_at.replace("T", " ")}</p>
      </section>

      <section className="video-grid" aria-live="polite">
        {videos.map((video) => (
          <VideoCard key={video.video_id} video={video} onOpen={setSelectedVideo} />
        ))}
      </section>

      <footer className="dashboard-footer">
        <span>监控范围</span>
        <p>{data.scan_note}</p>
      </footer>

      {selectedVideo && <VideoModal video={selectedVideo} onClose={closeModal} />}
    </main>
  );
}
