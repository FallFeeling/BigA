"use client";

import { useEffect, useState } from "react";
import { formatCount } from "../format";
import type { CommentItem, InteractionThread, ThreadComment, VideoItem } from "../types";

type Props = {
  video: VideoItem;
  onClose: () => void;
};

type Tab = "interaction" | "transcript" | "comments";

function Avatar({ comment }: { comment: CommentItem }) {
  if (comment.user.avatar) {
    return <img className="comment-avatar" src={comment.user.avatar} alt="" referrerPolicy="no-referrer" />;
  }
  return <span className="comment-avatar avatar-fallback">{comment.user.nickname.slice(0, 1)}</span>;
}

function CommentMeta({ comment }: { comment: CommentItem }) {
  return (
    <span className="comment-meta">
      {comment.create_time_display || "时间未知"}
      {comment.ip_label ? ` · ${comment.ip_label}` : ""}
      <span>♡ {formatCount(comment.digg_count)}</span>
    </span>
  );
}

function AuthorReply({ reply }: { reply: CommentItem }) {
  return (
    <div className="author-reply">
      <div className="reply-accent" />
      <Avatar comment={reply} />
      <div className="comment-content">
        <div className="comment-heading">
          <strong>{reply.user.nickname}</strong>
          <span className="author-badge">作者</span>
          <span className="reply-time">{reply.create_time_display}</span>
        </div>
        <p>{reply.text}</p>
        <span className="comment-meta">♡ {formatCount(reply.digg_count)}</span>
      </div>
    </div>
  );
}

function InteractionCard({ thread }: { thread: InteractionThread }) {
  const [expanded, setExpanded] = useState(false);
  const replies: Array<CommentItem & { is_author?: boolean }> = thread.replies?.length
    ? thread.replies
    : thread.author_replies.map((reply) => ({ ...reply, is_author: true }));
  const knownAuthorReplyIds = new Set(thread.author_replies.map((reply) => reply.comment_id));
  const authorRepliesById = new Map<string, CommentItem>();
  thread.author_replies.forEach((reply) => authorRepliesById.set(reply.comment_id, reply));
  replies.forEach((reply) => {
    if (reply.is_author || knownAuthorReplyIds.has(reply.comment_id)) {
      authorRepliesById.set(reply.comment_id, reply);
    }
  });
  const authorReplies = Array.from(authorRepliesById.values());
  const otherReplies = replies.filter(
    (reply) => !reply.is_author && !knownAuthorReplyIds.has(reply.comment_id),
  );
  return (
    <article className="interaction-thread">
      <div className="comment-row">
        <Avatar comment={thread.parent} />
        <div className="comment-content">
          <div className="comment-heading">
            <strong>{thread.parent.user.nickname}</strong>
            {thread.author_commented && <span className="author-badge">作者评论</span>}
            {thread.author_liked && <span className="liked-badge">作者赞过</span>}
          </div>
          <p>{thread.parent.text}</p>
          <CommentMeta comment={thread.parent} />
        </div>
      </div>

      {authorReplies.length > 0 && (
        <div className="interaction-author-replies">
          {authorReplies.map((reply) => (
            <AuthorReply reply={reply} key={reply.comment_id} />
          ))}
        </div>
      )}

      {otherReplies.length > 0 && (
        <button
          className="expand-replies"
          type="button"
          data-testid={`interaction-expand-${thread.parent.comment_id}`}
          onClick={(event) => {
            event.stopPropagation();
            setExpanded((value) => !value);
          }}
          aria-expanded={expanded}
        >
          <span>{expanded ? "收起其他回复" : `展开 ${otherReplies.length} 条其他回复`}</span>
          <span className={expanded ? "chevron expanded" : "chevron"}>⌄</span>
        </button>
      )}

      {expanded && (
        <div className="reply-list">
          {otherReplies.map((reply) => (
            <div className="regular-reply" key={reply.comment_id}>
              <Avatar comment={reply} />
              <div className="comment-content">
                <div className="comment-heading"><strong>{reply.user.nickname}</strong></div>
                <p>{reply.text}</p>
                <CommentMeta comment={reply} />
              </div>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

function CommentThread({ comment, expanded, onToggle }: {
  comment: ThreadComment;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <article className="comment-thread-card">
      <div className="comment-row">
        <Avatar comment={comment} />
        <div className="comment-content">
          <div className="comment-heading">
            <strong>{comment.user.nickname}</strong>
            {comment.author_liked && <span className="liked-badge">作者赞过</span>}
          </div>
          <p>{comment.text}</p>
          <CommentMeta comment={comment} />
        </div>
      </div>

      {comment.replies.length > 0 && (
        <button className="expand-replies" type="button" onClick={onToggle} aria-expanded={expanded}>
          <span>{expanded ? "收起回复" : `展开 ${comment.replies.length} 条回复`}</span>
          <span className={expanded ? "chevron expanded" : "chevron"}>⌄</span>
        </button>
      )}

      {expanded && (
        <div className="reply-list">
          {comment.replies.map((reply) => (
            reply.is_author ? (
              <AuthorReply reply={reply} key={reply.comment_id} />
            ) : (
              <div className="regular-reply" key={reply.comment_id}>
                <Avatar comment={reply} />
                <div className="comment-content">
                  <div className="comment-heading"><strong>{reply.user.nickname}</strong></div>
                  <p>{reply.text}</p>
                  <CommentMeta comment={reply} />
                </div>
              </div>
            )
          ))}
        </div>
      )}
    </article>
  );
}

function TranscriptPanel({ video }: { video: VideoItem }) {
  if (video.transcript_status === "complete" && video.transcript) {
    return (
      <div className="transcript-panel">
        <div className="transcript-summary">
          <span>本地转录</span>
          <strong>{video.transcript.segments.length} 个片段</strong>
          <p>Whisper {video.transcript.model} · 中文识别</p>
        </div>
        <div className="transcript-segments">
          {video.transcript.segments.map((segment, index) => (
            <div className="transcript-segment" key={`${segment.start}-${index}`}>
              <span>{segment.start_display}</span>
              <p>{segment.text}</p>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (video.transcript_status === "error") {
    return <StatusPanel label="转录失败" title="这条视频暂时没有生成文字" detail={video.transcript_error || "可稍后重新处理。"} />;
  }

  if (video.transcript_status === "pending" || video.transcript_status === "processing") {
    return <StatusPanel label="转录中" title="正在生成视频文字" detail="转录完成后会自动更新到看板。" />;
  }

  return <StatusPanel label="暂无转录" title="这条历史视频没有转录内容" detail="新视频会在发现后自动进入转录流程。" />;
}

function CommentsPanel({ video }: { video: VideoItem }) {
  const [expandedIds, setExpandedIds] = useState<string[]>([]);
  const comments = video.comments || [];
  const toggle = (commentId: string) => {
    setExpandedIds((current) => current.includes(commentId)
      ? current.filter((id) => id !== commentId)
      : [...current, commentId]);
  };

  if (video.comments_status === "complete") {
    return (
      <div className="comments-panel">
        <div className="comments-overview">
          <span>热度排序前 {comments.length} 条评论</span>
          <strong>含 {video.comment_reply_count || 0} 条展开回复</strong>
        </div>
        <div className="comments-list">
          {comments.map((comment, index) => (
            <div className="numbered-comment" key={comment.comment_id}>
              <span className="comment-index">{String(index + 1).padStart(2, "0")}</span>
              <CommentThread
                comment={comment}
                expanded={expandedIds.includes(comment.comment_id)}
                onToggle={() => toggle(comment.comment_id)}
              />
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (video.comments_status === "error") {
    return <StatusPanel label="读取失败" title="评论内容暂时不可用" detail="可以稍后重新抓取这条视频。" />;
  }

  return <StatusPanel label="暂无快照" title="这条历史视频没有前 20 条评论快照" detail="新视频会在 48 小时周期内持续更新评论。" />;
}

function StatusPanel({ label, title, detail }: { label: string; title: string; detail: string }) {
  return (
    <div className="empty-interaction">
      <span>{label}</span>
      <h3>{title}</h3>
      <p>{detail}</p>
    </div>
  );
}

export function VideoModal({ video, onClose }: Props) {
  const [tab, setTab] = useState<Tab>("interaction");

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKeyDown);
    document.body.classList.add("modal-open");
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("modal-open");
    };
  }, [onClose]);

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="video-modal" role="dialog" aria-modal="true" aria-label="视频详情" onMouseDown={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <div>
            <span className="modal-kicker">视频 #{String(video.order).padStart(2, "0")}{video.is_deleted ? " · 已删除" : ""}</span>
            <p>{video.published_at} · 赞 {formatCount(video.like_count)} · 评 {formatCount(video.comment_count)}</p>
          </div>
          <div className="modal-actions">
            <button type="button" onClick={onClose} aria-label="关闭">×</button>
          </div>
        </header>

        <nav className="modal-tabs" aria-label="详情分类">
          <button className={tab === "interaction" ? "active" : ""} onClick={() => setTab("interaction")}>
            博主互动
          </button>
          <button className={tab === "transcript" ? "active" : ""} onClick={() => setTab("transcript")}>
            视频转录
          </button>
          <button className={tab === "comments" ? "active" : ""} onClick={() => setTab("comments")}>
            评论
          </button>
        </nav>

        <div className="modal-content">
          {tab === "interaction" && (
            video.interaction_threads.length ? (
              <div className="interaction-list">
                {video.interaction_threads.map((thread) => <InteractionCard thread={thread} key={thread.parent.comment_id} />)}
              </div>
            ) : (
              <StatusPanel label="暂无互动" title="当前评论范围内未检测到博主互动" detail={`已检测 ${video.comments?.length || video.scanned_top_comment_count} 条一级评论。`} />
            )
          )}
          {tab === "transcript" && <TranscriptPanel video={video} />}
          {tab === "comments" && <CommentsPanel video={video} />}
        </div>
      </section>
    </div>
  );
}
