"""
IPTV源爬取 → 全部IP频道列表TXT生成器 v4
优化：URL可直接拼接，用 requests 替代 Playwright，速度更快
"""
from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth
#from bs4 import BeautifulSoup
import re
import time
import sys

# ===================== 可自定义参数区 =====================
CRAWL_TYPE = "酒店"        # 组播 / 酒店 / 咪咕 / 其他 / 全部
CRAWL_PROVINCE = "山东"    # 山东/安徽/北京/四川/浙江/湖北/河南/江苏/广东/湖南/全部
PAGE_SIZE = 10              # 仅支持 3 / 6 / 10
TOTAL_PAGES = 5
OUTPUT_FILE = "iptv_channels.txt"
DELAY_BETWEEN_PAGES = 2
DELAY_BETWEEN_IPS = 2

# ===================== udpxy 组播转单播 =====================
UDPXY_SERVER = ""
# ========================================================

BASE_URL = "https://iptv.cqshushu.com"
LIST_URL = f"{BASE_URL}/index.php"

type_map = {"组播": "multicast", "酒店": "hotel", "咪咕": "migu", "其他": "other", "全部": "all"}
province_map = {
    "全部": "all", "山东": "sd", "安徽": "ah", "北京": "BJ", "四川": "sc",
    "浙江": "zj", "湖北": "hb", "河南": "hn", "江苏": "js", "广东": "gd", "湖南": "hn",
}

if PAGE_SIZE not in (3, 6, 10):
    PAGE_SIZE = 3
t_value = type_map.get(CRAWL_TYPE, "all")
province_value = province_map.get(CRAWL_PROVINCE, "all")

def log(msg: str, end: str = "\n"):
    print(msg, flush=True, end=end)


def classify_channel(name: str) -> str:
    n = name.strip().upper()
    if re.match(r"^(CCTV|CETV|CGTN)", n):
        return "央视频道"
    if "卫视" in name:
        return "卫视频道"
    return "其他频道"


def convert_multicast_url(url: str) -> str:
    if not UDPXY_SERVER:
        return url
    match = re.search(r'(/[ru]tp/)([\d.]+:\d+)', url)
    if match:
        base = UDPXY_SERVER.rstrip('/')
        return f"{base}{match.group(1)}{match.group(2)}"
    match = re.match(r'(rtp|udp)://([\d.]+:\d+)', url)
    if match:
        base = UDPXY_SERVER.rstrip('/')
        return f"{base}/{match.group(1)}/{match.group(2)}"
    return url


# ────────────── 第一步：遍历列表页，提取所有 p 值 ──────────────

def safe_goto(page: Page, url: str, timeout: int = 30000) -> bool:
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        return True
    except Exception as e:
        log(f"   ⚠️ 页面加载超时: {e}")
        return False


def collect_all_ips(page: Page) -> list[dict]:
    """
    直接拼接URL访问列表页，从HTML中提取 p 值。
    URL格式: index.php?t={type}&province={province}&limit={size}&page={n}
    """
    all_ips = []
    seen = set()

    for pg in range(1, TOTAL_PAGES + 1):
        url = f"{LIST_URL}?t={t_value}&province={province_value}&limit={PAGE_SIZE}&page={pg}"
        log(f"\n📃 列表页 {pg}/{TOTAL_PAGES}: {url}")
        if not safe_goto(page, url):
            log(f"   ❌ 第{pg}页加载失败，跳过")
            continue
        try:
            page.wait_for_selector("table", timeout=15000)
        except Exception:
            log(f"   ❌ 第{pg}页未找到表格，跳过")
            continue
        page.wait_for_timeout(2000)

        # 用JS一次性提取所有IP和p值
        entries = page.evaluate("""
            () => {
                const rows = document.querySelectorAll('table tr');
                const results = [];
                for (let i = 1; i < rows.length; i++) {
                    const tds = rows[i].querySelectorAll('td');
                    if (tds.length < 6) continue;
                    const status = tds[5].innerText.trim();
                    if (status === '暂时失效' || status === '失效') continue;
                    const link = tds[0].querySelector('a');
                    if (!link) continue;
                    const ip = tds[0].innerText.trim();
                    const onclick = link.getAttribute('onclick') || '';
                    const match = onclick.match(/gotoIP\\('([^']+)',\\s*'([^']+)'\\)/);
                    if (match) {
                        results.push({ip: ip, p: match[1], type: match[2]});
                    }
                }
                return results;
            }
        """)

        new_count = 0
        for entry in entries:
            if entry["ip"] not in seen:
                seen.add(entry["ip"])
                all_ips.append(entry)
                new_count += 1
                log(f"   ✅ {entry['ip']} → p={entry['p']}")

        skipped = len(entries) - new_count
        if skipped > 0:
            log(f"   ⏭️ 跳过 {skipped} 个重复IP")
        log(f"   📊 第{pg}页完成，累计 {len(all_ips)} 个IP")

        if pg < TOTAL_PAGES:
            time.sleep(DELAY_BETWEEN_PAGES)

    return all_ips


# ────────────── 第二步：逐个IP获取频道列表 ──────────────

def fetch_channels_for_ip(page: Page, ip_info: dict, index: int, total: int) -> list[dict]:
    """
    直接拼接详情页URL → 提取s值 → 拼接TXT下载URL → 下载
    URL格式: index.php?p={p}&t={type}  (详情页)
             index.php?s={s}&t={type}&channels=1&download=txt  (下载)
    """
    ip = ip_info["ip"]
    p_val = ip_info["p"]
    crawl_type = ip_info.get("type", t_value)
    detail_url = f"{LIST_URL}?p={p_val}&t={crawl_type}"

    log(f"\n{'─'*50}")
    log(f"🔍 [{index}/{total}] IP: {ip} (p={p_val})")

    # 1. 访问详情页
    log(f"   📄 加载详情页...")
    if not safe_goto(page, detail_url):
        log(f"   ❌ 详情页加载失败")
        return []
    page.wait_for_timeout(2000)

    # 2. 从HTML提取 s 值
    s_val = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a');
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const match = href.match(/[?&]s=([A-Za-z0-9_-]+)/);
                if (match) return match[1];
            }
            return null;
        }
    """)

    if not s_val:
        log(f"   ❌ 未找到s值")
        return []

    log(f"   🔑 s={s_val}")

    # 3. 直接下载TXT
    txt_url = f"{LIST_URL}?s={s_val}&t={crawl_type}&channels=1&download=txt"
    log(f"   📥 下载频道数据...")
    try:
        resp = page.request.get(txt_url)
    except Exception:
        log(f"   ❌ 下载请求失败")
        return []

    if resp.status == 429:
        log(f"   ⚠️ 被限流 (429)，等待10秒后重试...")
        time.sleep(10)
        try:
            resp = page.request.get(txt_url)
        except Exception:
            log(f"   ❌ 重试仍然失败")
            return []

    if resp.status != 200:
        log(f"   ❌ TXT下载失败 (HTTP {resp.status})")
        return []

    body = resp.body().decode("utf-8", errors="replace")
    channels = []
    for line in body.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("by ") or line.startswith("类型："):
            continue
        if "," in line:
            parts = line.split(",", 1)
            name, url = parts[0].strip(), parts[1].strip()
            if name and url and url.startswith("http"):
                channels.append({"name": name, "url": url})

    log(f"   ✅ 完成 [{index}/{total}] - {ip}: {len(channels)} 个频道")
    return channels


# ────────────── 第三步：合并输出 ──────────────

def format_txt(all_channels: dict[str, list[dict]]) -> str:
    lines = []
    for genre in ["央视频道", "卫视频道", "其他频道"]:
        items = all_channels.get(genre, [])
        if not items:
            continue
        seen = set()
        unique = []
        for ch in items:
            key = f"{ch['name']}|{ch['url']}"
            if key not in seen:
                seen.add(key)
                unique.append(ch)
        lines.append(f"{genre},#genre#")
        for ch in unique:
            final_url = convert_multicast_url(ch['url'])
            lines.append(f"{ch['name']},{final_url}")
        lines.append("")
    return "\n".join(lines)


# ────────────── 主流程 ──────────────

def main():
    log("=" * 60)
    log(f"🚀 IPTV频道爬取器 v4 (Playwright浏览器模式)")
    log(f"   类型={CRAWL_TYPE} | 省份={CRAWL_PROVINCE}")
    log(f"   页数={TOTAL_PAGES} | 每页={PAGE_SIZE}条")
    log(f"   预计IP数: {TOTAL_PAGES * PAGE_SIZE}")
    if UDPXY_SERVER:
        log(f"   udpxy代理: {UDPXY_SERVER} (组播→单播)")
    else:
        log(f"   udpxy代理: 未配置 (保留原始链接)")
    log("=" * 60)

    stealth = Stealth(
        navigator_languages_override=("zh-CN", "zh"),
        navigator_platform_override="Win32",
        navigator_vendor_override="Google Inc.",
    )

    with stealth.use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = ctx.new_page()

        # 第一步
        log("\n📋 第一步：从列表页提取IP和p值")
        ip_list = collect_all_ips(page)
        log(f"\n📊 共提取 {len(ip_list)} 个有效IP\n")

        if not ip_list:
            log("❌ 没有找到有效IP，退出")
            browser.close()
            sys.exit(1)

        # 第二步
        log("📥 第二步：逐个获取频道列表")
        all_groups = {"央视频道": [], "卫视频道": [], "其他频道": []}
        success_count = 0

        for i, ip_info in enumerate(ip_list, 1):
            channels = fetch_channels_for_ip(page, ip_info, i, len(ip_list))
            if channels:
                success_count += 1
                for ch in channels:
                    cat = classify_channel(ch["name"])
                    all_groups[cat].append(ch)
            if i < len(ip_list):
                time.sleep(DELAY_BETWEEN_IPS)

        browser.close()

    # 第三步
    total_channels = sum(len(v) for v in all_groups.values())
    log(f"\n{'=' * 60}")
    log(f"📊 统计: {success_count}/{len(ip_list)} 个IP成功，共 {total_channels} 个频道")

    if total_channels == 0:
        log("❌ 没有获取到任何频道数据")
        sys.exit(1)

    txt_content = format_txt(all_groups)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(txt_content)

    log(f"💾 已保存到 {OUTPUT_FILE}")

    for genre in ["央视频道", "卫视频道", "其他频道"]:
        items = all_groups.get(genre, [])
        if items:
            seen = set(f"{c['name']}|{c['url']}" for c in items)
            log(f"   {genre}: {len(seen)} 个频道")

    log(f"\n✅ 完成!")


if __name__ == "__main__":
    main()
