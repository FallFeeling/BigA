export function formatCount(value: number): string {
  if (value >= 10000) {
    return `${(value / 10000).toFixed(value >= 100000 ? 0 : 1).replace(/\.0$/, "")}万`;
  }
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function formatDuration(milliseconds: number): string {
  if (!milliseconds) return "";
  const seconds = Math.round(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function shortDate(value: string): string {
  return value ? value.slice(5, 16) : "时间未知";
}
