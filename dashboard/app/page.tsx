import type { Metadata } from "next";
import { Dashboard } from "./Dashboard";

export const metadata: Metadata = {
  title: "模型先生 · 视频互动看板",
  description: "查看模型先生最近视频的数据与博主互动。",
};

export default function Home() {
  return <Dashboard />;
}
