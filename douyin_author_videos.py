import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# 根目录：D:\BigA
ROOT_DIR = Path(__file__).resolve().parent

# 相对路径
RUNTIME_DIR = ROOT_DIR / "runtime"
PROFILE_DIR = RUNTIME_DIR / "douyin_profile"
DATA_DIR = ROOT_DIR / "data"
DEBUG_DIR = ROOT_DIR / "debug"

# 模型先生主页
AUTHOR_URL = (
    "https://www.douyin.com/user/"
    "MS4wLjABAAAAK713M9d8PGNb_WiMYf7yKhOI5y60H4uELJK2guDjJT0"
    "?from_tab_name=main&showSubTab=video&showTab=post"
)


def ensure_dirs():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def launch_douyin_context(playwright, headless=False):
    """
    使用 Playwright 自带 Chromium。
    登录态保存在 runtime/douyin_profile。
    不影响你日常使用的 Chrome。
    """
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        accept_downloads=False,
    )


def login_once():
    """
    首次登录。
    打开专用 Chromium 浏览器，手动登录抖音。
    登录完成后按 Enter，登录态会保存到 runtime/douyin_profile。
    """
    ensure_dirs()

    with sync_playwright() as p:
        context = launch_douyin_context(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()

        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)

        print("\n[首次登录]")
        print("1. 请在打开的 Chromium 浏览器里手动登录抖音。")
        print("2. 登录成功后，确认页面已经是登录状态。")
        print("3. 不要直接关闭浏览器，回到终端按 Enter。")
        input("\n登录完成后按 Enter 保存登录态...")

        context.close()

    print(f"\n登录态已保存到：{PROFILE_DIR}")
    print("注意：runtime/douyin_profile 等价于账号登录凭证，不要发给别人，也不要上传 GitHub。")


def extract_videos_from_page(page, viewport_only=True):
    """
    viewport_only=True:
        只提取当前视口可见的视频卡片。
        不滚动，不触发加载更多。

    viewport_only=False:
        不滚动，但提取当前 DOM 里已经存在的所有视频卡片。
        数量可能比当前屏幕可见的更多。
    """
    result = page.evaluate(
        """
        ({ viewportOnly }) => {
            function cleanText(s) {
                return (s || "")
                    .replace(/\\u200b/g, "")
                    .replace(/\\s+/g, " ")
                    .trim();
            }

            function absUrl(href) {
                try {
                    return new URL(href, location.origin).href;
                } catch (e) {
                    return href || "";
                }
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

            function hasVisibleStyle(el) {
                if (!el) return false;

                const style = window.getComputedStyle(el);
                if (!style) return false;

                if (style.display === "none") return false;
                if (style.visibility === "hidden") return false;
                if (Number(style.opacity) === 0) return false;

                return true;
            }

            function inViewport(rect) {
                const vw = window.innerWidth || document.documentElement.clientWidth;
                const vh = window.innerHeight || document.documentElement.clientHeight;

                return (
                    rect.bottom > 0 &&
                    rect.right > 0 &&
                    rect.top < vh &&
                    rect.left < vw
                );
            }

            function looksLikeVideoCard(el) {
                if (!el || !hasVisibleStyle(el)) return false;

                const r = el.getBoundingClientRect();
                const raw = cleanText(el.innerText || "");

                // 视频卡片大致尺寸过滤
                if (r.width < 90 || r.width > 460) return false;
                if (r.height < 100 || r.height > 620) return false;

                // 过滤左侧导航栏、顶部区域、页脚区域、大容器
                if (r.left < 100) return false;
                if (r.top < 220) return false;

                // 文本太长说明可能爬到了主页大容器
                if (raw.length > 800) return false;

                // 视频卡片通常有封面图
                if (!el.querySelector("img")) return false;

                return true;
            }

            function findCard(a) {
                let node = a;
                const candidates = [];

                for (let depth = 0; depth < 10 && node; depth++) {
                    if (looksLikeVideoCard(node)) {
                        const r = node.getBoundingClientRect();
                        const raw = cleanText(node.innerText || "");

                        candidates.push({
                            node,
                            depth,
                            area: r.width * r.height,
                            rawLen: raw.length
                        });
                    }

                    node = node.parentElement;
                }

                if (candidates.length === 0) {
                    return a;
                }

                // 优先选择文本较完整、但仍然是卡片尺寸的容器
                candidates.sort((a, b) => {
                    if (b.rawLen !== a.rawLen) return b.rawLen - a.rawLen;
                    return a.area - b.area;
                });

                return candidates[0].node;
            }

            function findCover(card, a) {
                const img = card.querySelector("img") || a.querySelector("img");
                if (!img) return "";

                return (
                    img.currentSrc ||
                    img.src ||
                    img.getAttribute("src") ||
                    img.getAttribute("data-src") ||
                    ""
                );
            }

            function badTitleLine(s) {
                if (!s) return true;

                const badExact = new Set([
                    "作品",
                    "合集",
                    "短剧",
                    "推荐",
                    "喜欢",
                    "搜索",
                    "日期筛选",
                    "置顶",
                    "更多",
                    "分享",
                    "评论"
                ]);

                if (badExact.has(s)) return true;
                if (/^\\d+(\\.\\d+)?(万|亿|w|W|k|K)?$/.test(s)) return true;
                if (s.includes("用户协议")) return true;
                if (s.includes("隐私政策")) return true;
                if (s.includes("联系我们")) return true;
                if (s.includes("营业执照")) return true;

                return false;
            }

            function guessTitle(rawText, a, card) {
                const attrCandidates = [
                    a.getAttribute("aria-label"),
                    a.getAttribute("title"),
                    card.getAttribute("aria-label"),
                    card.getAttribute("title")
                ]
                    .filter(Boolean)
                    .map(cleanText)
                    .filter(Boolean);

                for (const c of attrCandidates) {
                    if (!badTitleLine(c) && c.length >= 2) {
                        return c.slice(0, 200);
                    }
                }

                let raw = rawText || "";

                // 常见文本：
                // 置顶 7467 12年前写的投资感悟...
                // 1.6万 见招拆招
                raw = raw.replace(/置顶/g, " ");
                raw = raw.replace(/^\\s*(\\d+(\\.\\d+)?(万|亿|w|W|k|K)?\\s*)+/, " ");

                const lines = raw
                    .split(/\\n| {2,}/)
                    .map(cleanText)
                    .filter(Boolean);

                for (let line of lines) {
                    line = line.replace(/^\\s*(\\d+(\\.\\d+)?(万|亿|w|W|k|K)?\\s*)+/, " ");
                    line = cleanText(line);

                    if (!badTitleLine(line) && line.length >= 2) {
                        return line.slice(0, 200);
                    }
                }

                return cleanText(rawText).slice(0, 200);
            }

            function guessLike(rawText) {
                const text = cleanText(rawText);

                // 优先取卡片文本开头的数字：
                // 9387 标题...
                // 1.6万 标题...
                // 置顶 7467 标题...
                const m = text.match(/^(?:置顶\\s*)?(\\d+(?:\\.\\d+)?(?:万|亿|w|W|k|K)?)/);

                if (m) return m[1];

                const fallback = text.match(/(\\d+(?:\\.\\d+)?(?:万|亿|w|W|k|K)?)/);
                return fallback ? fallback[1] : "";
            }

            const anchors = Array.from(document.querySelectorAll('a[href*="/video/"]'));
            const seen = new Set();
            const rows = [];

            for (const a of anchors) {
                const href = absUrl(a.getAttribute("href") || a.href || "");
                const m = href.match(/\\/video\\/(\\d+)/);

                if (!m) continue;

                const videoId = m[1];

                if (seen.has(videoId)) continue;
                if (!hasVisibleStyle(a)) continue;

                const card = findCard(a);
                const cardRect = card.getBoundingClientRect();

                if (!looksLikeVideoCard(card)) continue;

                const visibleInViewport = inViewport(cardRect);

                if (viewportOnly && !visibleInViewport) {
                    continue;
                }

                const rawText = cleanText(card.innerText || a.innerText || "");
                const title = guessTitle(rawText, a, card);
                const cover = findCover(card, a);

                // 过滤页面底部协议、隐私政策等污染文本
                if (rawText.includes("用户协议") || rawText.includes("隐私政策")) continue;
                if (!href.includes("/video/")) continue;

                seen.add(videoId);

                rows.push({
                    order_index: rows.length + 1,
                    video_id: videoId,
                    href,
                    title_hint: title,
                    raw_text: rawText,
                    like_hint: guessLike(rawText),
                    is_pinned: rawText.includes("置顶"),
                    cover,
                    rect: rectObj(cardRect),
                    visible_in_viewport: visibleInViewport
                });
            }

            return {
                viewport: {
                    width: window.innerWidth,
                    height: window.innerHeight
                },
                total_video_links_in_dom: anchors.length,
                rows
            };
        }
        """,
        {"viewportOnly": viewport_only},
    )

    return result


def save_results(result, metadata):
    now = datetime.now().strftime("%Y%m%d_%H%M%S")

    videos = result.get("rows", [])

    payload = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "author_url": AUTHOR_URL,
        "final_url": metadata.get("final_url", ""),
        "page_title": metadata.get("page_title", ""),
        "response_status": metadata.get("response_status", ""),
        "mode": metadata.get("mode", ""),
        "viewport": result.get("viewport", {}),
        "total_video_links_in_dom": result.get("total_video_links_in_dom", 0),
        "count": len(videos),
        "videos": videos,
    }

    latest_json = DATA_DIR / "author_videos_latest.json"
    latest_csv = DATA_DIR / "author_videos_latest.csv"

    timestamp_json = DATA_DIR / f"author_videos_{now}.json"
    timestamp_csv = DATA_DIR / f"author_videos_{now}.csv"

    for path in [latest_json, timestamp_json]:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    fieldnames = [
        "order_index",
        "video_id",
        "href",
        "title_hint",
        "raw_text",
        "like_hint",
        "is_pinned",
        "cover",
        "visible_in_viewport",
        "rect",
    ]

    for path in [latest_csv, timestamp_csv]:
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for v in videos:
                row = dict(v)
                row["rect"] = json.dumps(row.get("rect", {}), ensure_ascii=False)
                writer.writerow(row)

    print(f"\n已保存 JSON：{latest_json}")
    print(f"已保存 CSV ：{latest_csv}")
    print(f"本次归档 JSON：{timestamp_json}")
    print(f"本次归档 CSV ：{timestamp_csv}")


def save_debug_files(page, result, metadata):
    now = datetime.now().strftime("%Y%m%d_%H%M%S")

    latest_candidates = DEBUG_DIR / "debug_candidates_latest.json"
    latest_html = DEBUG_DIR / "debug_page_latest.html"
    latest_png = DEBUG_DIR / "debug_screenshot_latest.png"

    timestamp_candidates = DEBUG_DIR / f"debug_candidates_{now}.json"
    timestamp_html = DEBUG_DIR / f"debug_page_{now}.html"
    timestamp_png = DEBUG_DIR / f"debug_screenshot_{now}.png"

    debug_payload = {
        "debug_at": datetime.now().isoformat(timespec="seconds"),
        "metadata": metadata,
        "result": result,
    }

    for path in [latest_candidates, timestamp_candidates]:
        path.write_text(
            json.dumps(debug_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    html = page.content()

    for path in [latest_html, timestamp_html]:
        path.write_text(html, encoding="utf-8")

    for path in [latest_png, timestamp_png]:
        page.screenshot(path=str(path), full_page=False)

    print(f"\n[debug] 候选信息：{latest_candidates}")
    print(f"[debug] 页面 HTML：{latest_html}")
    print(f"[debug] 页面截图：{latest_png}")


def scan_author_initial_only(wait_ms=3000, keep_open=False, debug=False, all_initial_dom=False):
    """
    打开博主主页，只读取初始加载的视频信息。
    默认只取当前视口可见卡片。
    不滚动，不触发加载更多。
    """
    ensure_dirs()

    with sync_playwright() as p:
        context = launch_douyin_context(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()

        print("\n正在打开博主主页...")

        response_status = ""

        try:
            response = page.goto(AUTHOR_URL, wait_until="domcontentloaded", timeout=60000)
            if response:
                response_status = response.status
        except PlaywrightTimeoutError:
            print("页面打开超时。请观察浏览器是否出现登录/验证/网络问题。")

        print("页面已打开，等待首批视频卡片加载...")

        try:
            page.wait_for_selector('a[href*="/video/"]', timeout=30000)
        except PlaywrightTimeoutError:
            print("\n30 秒内没有找到视频链接。")
            print("可能原因：未登录、页面加载失败、抖音要求验证、页面结构变化。")
            input("请在浏览器里处理后，按 Enter 继续尝试提取...")

        # 只等待首批异步渲染，不滚动页面
        page.wait_for_timeout(wait_ms)

        viewport_only = not all_initial_dom

        result = extract_videos_from_page(
            page,
            viewport_only=viewport_only,
        )

        metadata = {
            "final_url": page.url,
            "page_title": page.title(),
            "response_status": response_status,
            "mode": "current_viewport_only" if viewport_only else "all_initial_dom_no_scroll",
        }

        print("\n页面状态：")
        print(f"  response_status: {metadata['response_status']}")
        print(f"  final_url: {metadata['final_url']}")
        print(f"  page_title: {metadata['page_title']}")
        print(f"  mode: {metadata['mode']}")
        print(f"  DOM 中 video 链接总数: {result.get('total_video_links_in_dom')}")
        print(f"  本次有效视频数: {len(result.get('rows', []))}")

        save_results(result, metadata)

        if debug:
            save_debug_files(page, result, metadata)

        print("\n视频预览：")

        for v in result.get("rows", []):
            print(f"\n[{v['order_index']}] video_id: {v['video_id']}")
            print(f"    href: {v['href']}")
            print(f"    title_hint: {v['title_hint']}")
            print(f"    like_hint: {v['like_hint']}")
            print(f"    is_pinned: {v['is_pinned']}")
            print(f"    raw_text: {v['raw_text'][:200]}")
            print(f"    cover: {v['cover']}")

        if keep_open:
            input("\nkeep_open=True，按 Enter 后关闭浏览器...")

        context.close()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--login",
        action="store_true",
        help="首次登录：打开专用浏览器，手动登录抖音并保存登录态。",
    )

    parser.add_argument(
        "--scan",
        action="store_true",
        help="扫描博主主页当前视口可见的视频信息，不滚动加载更多。",
    )

    parser.add_argument(
        "--all-initial-dom",
        action="store_true",
        help="不滚动，但提取当前 DOM 里已有的所有视频卡片；默认只提取当前视口可见卡片。",
    )

    parser.add_argument(
        "--wait-ms",
        type=int,
        default=3000,
        help="页面打开后等待首批视频渲染的时间，默认 3000ms。不滚动页面。",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="保存候选链接、页面 HTML、截图到 debug 目录。",
    )

    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="扫描结束后不立刻关闭浏览器，方便观察页面。",
    )

    args = parser.parse_args()

    if not args.login and not args.scan:
        print("未指定参数，默认执行 --scan。首次使用建议先执行 --login。")
        args.scan = True

    if args.login:
        login_once()

    if args.scan:
        scan_author_initial_only(
            wait_ms=args.wait_ms,
            keep_open=args.keep_open,
            debug=args.debug,
            all_initial_dom=args.all_initial_dom,
        )


if __name__ == "__main__":
    main()
