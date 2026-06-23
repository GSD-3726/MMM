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
DELAY_BETWEEN_PAGES = 3
DELAY_BETWEEN_IPS = 4

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


def safe_goto(page: Page, url: str, timeout: int = 30000) -> bool:
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        return True
    except Exception as e:
        print(f"   ⚠️ 页面加载超时: {e}")
        return False


# ────────────── 第一步：遍历列表页，逐个点击IP获取详情页URL ──────────────

def collect_all_ips(page: Page) -> list[dict]:
    """
    每页加载后，逐个点击IP链接进入详情页，记录URL后 go_back 返回。
    每次 go_back 后重新查询当前页的链接（避免 stale element）。
    """
    all_ips = []

    for pg in range(1, TOTAL_PAGES + 1):
        url = f"{LIST_URL}?t={t_value}&province={province_value}&limit={PAGE_SIZE}&page={pg}"
        print(f"\n📃 列表页 {pg}/{TOTAL_PAGES}: {url}")
        if not safe_goto(page, url):
            print(f"   ❌ 第{pg}页加载失败，跳过")
            continue
        try:
            page.wait_for_selector("table", timeout=15000)
        except Exception:
            print(f"   ❌ 第{pg}页未找到表格，跳过")
            continue
        page.wait_for_timeout(2000)

        # 统计本页有效IP数量（通过JS获取，避免stale）
        ip_count = page.evaluate("""
            () => {
                const rows = document.querySelectorAll('table tr');
                let count = 0;
                for (let i = 1; i < rows.length; i++) {
                    const tds = rows[i].querySelectorAll('td');
                    if (tds.length < 6) continue;
                    const status = tds[5].innerText.trim();
                    if (status === '暂时失效' || status === '失效') continue;
                    const link = tds[0].querySelector('a');
                    if (link) count++;
                }
                return count;
            }
        """)
        print(f"   📋 本页 {ip_count} 个有效IP，逐个点击获取URL...")

        # 逐个点击：每次点第1个链接（因为点完go_back后页面刷新，索引重置）
        processed = 0
        while processed < ip_count:
            # 用JS获取第 processed+1 个有效链接的IP文本
            ip_text = page.evaluate("""
                (skipIndex) => {
                    const rows = document.querySelectorAll('table tr');
                    let count = 0;
                    for (let i = 1; i < rows.length; i++) {
                        const tds = rows[i].querySelectorAll('td');
                        if (tds.length < 6) continue;
                        const status = tds[5].innerText.trim();
                        if (status === '暂时失效' || status === '失效') continue;
                        const link = tds[0].querySelector('a');
                        if (link) {
                            if (count === skipIndex) return tds[0].innerText.trim();
                            count++;
                        }
                    }
                    return null;
                }
            """, processed)

            if not ip_text:
                break

            # 点击第 processed+1 个有效链接
            try:
                page.evaluate("""
                    (skipIndex) => {
                        const rows = document.querySelectorAll('table tr');
                        let count = 0;
                        for (let i = 1; i < rows.length; i++) {
                            const tds = rows[i].querySelectorAll('td');
                            if (tds.length < 6) continue;
                            const status = tds[5].innerText.trim();
                            if (status === '暂时失效' || status === '失效') continue;
                            const link = tds[0].querySelector('a');
                            if (link) {
                                if (count === skipIndex) { link.click(); return true; }
                                count++;
                            }
                        }
                        return false;
                    }
                """, processed)
                page.wait_for_timeout(3000)
                detail_url = page.url
                print(f"   [{processed+1}/{ip_count}] ✅ {ip_text}")
                all_ips.append({"ip": ip_text, "detail_url": detail_url, "page": pg})
            except Exception as e:
                print(f"   [{processed+1}/{ip_count}] ⚠️ {ip_text} 点击失败: {e}")

            # 返回列表页
            try:
                page.go_back()
                page.wait_for_timeout(2500)
                page.wait_for_selector("table", timeout=10000)
                page.wait_for_timeout(1000)
            except Exception:
                # 恢复：重新加载列表页
                print(f"   ⚠️ 返回失败，重新加载列表页...")
                safe_goto(page, url)
                try:
                    page.wait_for_selector("table", timeout=15000)
                    page.wait_for_timeout(1500)
                except Exception:
                    print(f"   ❌ 列表页恢复失败，跳过本页剩余IP")
                    break

            processed += 1

        print(f"   📊 第{pg}页完成，累计 {len(all_ips)} 个IP")
        if pg < TOTAL_PAGES:
            time.sleep(DELAY_BETWEEN_PAGES)

    return all_ips


# ────────────── 第二步：逐个IP获取频道列表 ──────────────

def fetch_channels_for_ip(page: Page, ip_info: dict, index: int, total: int) -> list[dict]:
    ip = ip_info["ip"]
    detail_url = ip_info["detail_url"]
    print(f"\n{'─'*50}")
    print(f"🔍 [{index}/{total}] IP: {ip}")

    print(f"   📄 进入详情页...")
    if not safe_goto(page, detail_url):
        print(f"   ❌ 详情页加载失败")
        return []
    page.wait_for_timeout(2000)

    ch_link = page.query_selector('a:has-text("查看频道列表")')
    if not ch_link:
        print(f"   ❌ 未找到频道列表入口")
        return []
    print(f"   📋 加载频道列表...")
    try:
        ch_link.click()
        page.wait_for_timeout(4000)
    except Exception:
        print(f"   ❌ 频道列表加载失败")
        return []

    txt_link = page.query_selector('a:has-text("TXT下载")')
    if not txt_link:
        print(f"   ❌ 未找到TXT下载链接")
        return []

    href = txt_link.get_attribute("href")
    txt_url = f"{LIST_URL}{href}"
    print(f"   📥 下载频道数据...")
    try:
        resp = page.request.get(txt_url)
    except Exception:
        print(f"   ❌ 下载请求失败")
        return []

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

        # 第一步
        print("\n📋 第一步：收集IP列表")
        ip_list = collect_all_ips(page)
        print(f"\n📊 共收集 {len(ip_list)} 个有效IP\n")

        if not ip_list:
            print("❌ 没有找到有效IP，退出")
            browser.close()
            return

        # 第二步
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
            if i < len(ip_list):
                time.sleep(DELAY_BETWEEN_IPS)

        browser.close()

    # 第三步
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

    for genre in ["央视频道", "卫视频道", "其他频道"]:
        items = all_groups.get(genre, [])
        if items:
            seen = set(f"{c['name']}|{c['url']}" for c in items)
            print(f"   {genre}: {len(seen)} 个频道")

    print(f"\n✅ 完成!")


if __name__ == "__main__":
    main()
