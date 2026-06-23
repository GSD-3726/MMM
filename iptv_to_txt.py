"""
IPTV源爬取 → 全部IP频道列表TXT生成器
流程：遍历列表页全部IP → 逐个进入详情页 → TXT下载 → 合并输出
"""
from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth
import re
import time
import sys

# ===================== 可自定义参数区 =====================
CRAWL_TYPE = "酒店"        # 组播 / 酒店 / 咪咕 / 其他 / 全部
CRAWL_PROVINCE = "山东"    # 山东/安徽/北京/四川/浙江/湖北/河南/江苏/广东/湖南/全部
PAGE_SIZE = 6              # 仅支持 3 / 6 / 10
TOTAL_PAGES = 3
OUTPUT_FILE = "iptv_channels.txt"
# 请求间隔（秒），避免给网站造成过大压力
DELAY_BETWEEN_PAGES = 3   # 翻页间隔
DELAY_BETWEEN_IPS = 4     # IP详情页间隔

# ===================== udpxy 组播转单播 =====================
# 填写你的 udpxy 服务器地址，如 "http://192.168.1.100:4022"
# 留空 "" 则不转换，保留原始链接
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


def classify_channel(name: str) -> str:
    n = name.strip().upper()
    if re.match(r"^(CCTV|CETV|CGTN)", n):
        return "央视频道"
    if "卫视" in name:
        return "卫视频道"
    return "其他频道"


def convert_multicast_url(url: str) -> str:
    """
    将组播URL通过udpxy转为单播可播放URL
    支持格式：
      http://网关IP:端口/rtp/组播IP:端口  →  http://udpxy/rtp/组播IP:端口
      http://网关IP:端口/udp/组播IP:端口  →  http://udpxy/udp/组播IP:端口
      rtp://组播IP:端口                   →  http://udpxy/rtp/组播IP:端口
      udp://组播IP:端口                   →  http://udpxy/udp/组播IP:端口
    """
    if not UDPXY_SERVER:
        return url

    # 已经包含 /rtp/ 或 /udp/ 路径 → 替换网关部分
    match = re.search(r'(/[ru]tp/)([\d.]+:\d+)', url)
    if match:
        proto_path = match.group(1)   # /rtp/ 或 /udp/
        stream_addr = match.group(2)  # 组播IP:端口
        base = UDPXY_SERVER.rstrip('/')
        return f"{base}{proto_path}{stream_addr}"

    # rtp:// 或 udp:// 开头
    match = re.match(r'(rtp|udp)://([\d.]+:\d+)', url)
    if match:
        proto = match.group(1)
        stream_addr = match.group(2)
        base = UDPXY_SERVER.rstrip('/')
        return f"{base}/{proto}/{stream_addr}"

    # 非组播URL，原样返回
    return url


# ────────────── 第一步：遍历列表页，收集全部IP ──────────────

def collect_all_ips(page: Page) -> list[dict]:
    """遍历列表页，收集所有IP条目（含详情页跳转参数）"""
    all_ips = []
    for pg in range(1, TOTAL_PAGES + 1):
        url = f"{LIST_URL}?t={t_value}&province={province_value}&limit={PAGE_SIZE}&page={pg}"
        print(f"📃 列表页 {pg}/{TOTAL_PAGES}: {url}")
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_selector("table", timeout=15000)
        page.wait_for_timeout(2000)

        rows = page.query_selector_all("table tr")[1:]
        if not rows:
            print(f"   ⚠️ 第{pg}页无数据，停止翻页")
            break

        for row in rows:
            tds = row.query_selector_all("td")
            if len(tds) < 6:
                continue
            ip_text = tds[0].inner_text().strip()
            status = tds[5].inner_text().strip()
            if status in ("暂时失效", "失效"):
                print(f"   ⏭️ 跳过 {ip_text} (状态: {status})")
                continue
            link = tds[0].query_selector("a")
            if link:
                all_ips.append({"ip": ip_text, "element": link, "page": pg})

        print(f"   ✅ 本页收集 {len(rows)} 个IP，累计 {len(all_ips)} 个")
        if pg < TOTAL_PAGES:
            time.sleep(DELAY_BETWEEN_PAGES)

    return all_ips


# ────────────── 第二步：逐个IP获取频道列表 ──────────────

def fetch_channels_for_ip(page: Page, ip_info: dict, index: int, total: int) -> list[dict]:
    """从列表页点击IP → 详情页 → 频道列表 → TXT下载，返回频道列表"""
    ip = ip_info["ip"]
    print(f"\n{'─'*50}")
    print(f"🔍 [{index}/{total}] IP: {ip}")

    # 回到列表页（因为需要从列表页点击进入详情页）
    pg = ip_info["page"]
    list_url = f"{LIST_URL}?t={t_value}&province={province_value}&limit={PAGE_SIZE}&page={pg}"
    print(f"   📃 加载列表页 (第{pg}页)...")
    page.goto(list_url, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_selector("table", timeout=15000)
    page.wait_for_timeout(1500)

    # 重新找到对应IP的链接并点击（按IP文本匹配）
    rows = page.query_selector_all("table tr")[1:]
    target_link = None
    for row in rows:
        tds = row.query_selector_all("td")
        if len(tds) >= 1:
            cell_text = tds[0].inner_text().strip()
            if cell_text == ip:
                target_link = tds[0].query_selector("a")
                break

    if not target_link:
        print(f"   ❌ 未找到IP {ip} 的链接")
        return []

    # 点击进入详情页
    print(f"   📄 进入详情页...")
    target_link.click()
    page.wait_for_timeout(3000)

    # 点击"查看频道列表"
    ch_link = page.query_selector('a:has-text("查看频道列表")')
    if not ch_link:
        print(f"   ❌ 未找到频道列表入口")
        return []
    print(f"   📋 加载频道列表...")
    ch_link.click()
    page.wait_for_timeout(4000)

    # 通过TXT下载接口获取全部频道
    txt_link = page.query_selector('a:has-text("TXT下载")')
    if not txt_link:
        print(f"   ❌ 未找到TXT下载链接")
        return []

    href = txt_link.get_attribute("href")
    txt_url = f"{LIST_URL}{href}"
    print(f"   📥 下载频道数据...")
    resp = page.request.get(txt_url)

    if resp.status != 200:
        print(f"   ❌ TXT下载失败 (HTTP {resp.status})")
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

    print(f"   ✅ 完成 [{index}/{total}] - {ip}: {len(channels)} 个频道")
    return channels


# ────────────── 第三步：合并输出 ──────────────

def format_txt(all_channels: dict[str, list[dict]]) -> str:
    """
    all_channels: {"央视频道": [...], "卫视频道": [...], "其他频道": [...]}
    按分类输出，同类内按频道名排序
    """
    lines = []
    for genre in ["央视频道", "卫视频道", "其他频道"]:
        items = all_channels.get(genre, [])
        if not items:
            continue
        # 按频道名去重（保留顺序）
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
    print("=" * 60)
    print(f"🚀 IPTV频道爬取器")
    print(f"   类型={CRAWL_TYPE} | 省份={CRAWL_PROVINCE}")
    print(f"   页数={TOTAL_PAGES} | 每页={PAGE_SIZE}条")
    print(f"   预计IP数: {TOTAL_PAGES * PAGE_SIZE}")
    if UDPXY_SERVER:
        print(f"   udpxy代理: {UDPXY_SERVER} (组播→单播)")
    else:
        print(f"   udpxy代理: 未配置 (保留原始链接)")
    print("=" * 60)

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

        # 第一步：收集全部IP
        print("\n📋 第一步：收集IP列表")
        ip_list = collect_all_ips(page)
        print(f"\n📊 共收集 {len(ip_list)} 个有效IP\n")

        if not ip_list:
            print("❌ 没有找到有效IP，退出")
            browser.close()
            return

        # 第二步：逐个获取频道
        print("📥 第二步：逐个获取频道列表")
        all_groups = {"央视频道": [], "卫视频道": [], "其他频道": []}
        success_count = 0

        for i, ip_info in enumerate(ip_list, 1):
            channels = fetch_channels_for_ip(page, ip_info, i, len(ip_list))
            if channels:
                success_count += 1
                for ch in channels:
                    cat = classify_channel(ch["name"])
                    all_groups[cat].append(ch)

            # IP间延迟
            if i < len(ip_list):
                time.sleep(DELAY_BETWEEN_IPS)

        browser.close()

    # 第三步：合并输出
    total_channels = sum(len(v) for v in all_groups.values())
    print(f"\n{'=' * 60}")
    print(f"📊 统计: {success_count}/{len(ip_list)} 个IP成功，共 {total_channels} 个频道")

    if total_channels == 0:
        print("❌ 没有获取到任何频道数据")
        sys.exit(1)

    txt_content = format_txt(all_groups)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(txt_content)

    print(f"💾 已保存到 {OUTPUT_FILE}")

    # 打印摘要
    for genre in ["央视频道", "卫视频道", "其他频道"]:
        items = all_groups.get(genre, [])
        if items:
            seen = set(f"{c['name']}|{c['url']}" for c in items)
            print(f"   {genre}: {len(seen)} 个频道")

    print(f"\n✅ 完成!")


if __name__ == "__main__":
    main()
