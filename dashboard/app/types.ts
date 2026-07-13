export type CommentUser = {
  nickname: string;
  uid: string;
  sec_uid: string;
  avatar: string;
};

export type CommentItem = {
  comment_id: string;
  text: string;
  create_time: number;
  create_time_display: string;
  digg_count: number;
  reply_total: number;
  ip_label: string;
  user: CommentUser;
};

export type ThreadComment = CommentItem & {
  author_liked: boolean;
  replies: Array<CommentItem & { is_author: boolean }>;
};

export type TranscriptSegment = {
  start: number;
  end: number;
  start_display: string;
  end_display: string;
  text: string;
};

export type Transcript = {
  model: string;
  language: string;
  language_probability: number;
  text: string;
  segments: TranscriptSegment[];
};

export type InteractionThread = {
  parent: CommentItem;
  author_liked: boolean;
  author_commented?: boolean;
  author_replies: CommentItem[];
  replies?: Array<CommentItem & { is_author?: boolean }>;
  first_seen_at?: string;
  last_seen_at?: string;
  tracking_status?: "active" | "frozen";
};

export type VideoItem = {
  order: number;
  video_id: string;
  url: string;
  cover: string;
  description: string;
  published_at: string;
  published_timestamp: number;
  like_count: number;
  comment_count: number;
  duration_ms: number;
  monitor_status?: "active" | "completed" | "deleted" | "archived";
  monitor_ends_at?: string;
  is_deleted?: boolean;
  deleted_at?: string;
  interaction_threads: InteractionThread[];
  has_author_interaction: boolean;
  scanned_top_comment_count: number;
  transcript_status?: "complete" | "not_processed" | "pending" | "processing" | "error";
  transcript?: Transcript;
  transcript_error?: string;
  comments_status?: "complete" | "not_processed" | "error";
  comments?: ThreadComment[];
  comment_reply_count?: number;
};

export type DashboardData = {
  generated_at: string;
  author: { name: string; sec_uid: string };
  scan_note: string;
  video_count: number;
  interaction_video_count: number;
  transcribed_video_count: number;
  comment_enriched_video_count: number;
  videos: VideoItem[];
};
