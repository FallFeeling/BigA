import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "模型先生 · 视频互动看板",
  description: "查看模型先生最近 30 条视频的数据、转录与博主互动。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
