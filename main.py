#!/usr/bin/env python3
"""
tonkiang.us 批量直播源爬虫
===========================
根据频道列表文件，批量从 tonkiang.us 爬取各频道的直播链接。
支持自定义爬取页数、每频道最大链接数，内置反爬延迟策略。

用法:
  python3 cctv1_scraper.py                          # 按 demo.txt 爬取，每频道1页
  python3 cctv1_scraper.py -f demo.txt              # 指定频道文件
  python3 cctv1_scraper.py -p 2                     # 每频道爬2页
  python3 cctv1_scraper.py -n 5                     # 每频道最多5条
  python3 cctv1_scraper.py -k "CCTV-1"              # 只爬单个频道
  python3 cctv1_scraper.py -f demo.txt -p 1 -n 3    # 每频道1页、最多3条

频道文件格式 (与 txt2tvlive 兼容):
  📺央视频道,#genre#
  CCTV-1
  CCTV-2
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse

from bs4 import BeautifulSoup

# ============================================================
#  ★ 配置区（可按需修改）
# ============================================================

# 网站地址
BASE_URL = "https://tonkiang.us"

# 频道列表文件路径（相对于脚本目录）
CHANNEL_FILE = "demo.txt"

# 每个频道最多爬取页数（1页约30条结果）
MAX_PAGES = 1

# 每个频道最多保留几条链接（0 = 不限制）
MAX_LINKS = 0

# 输出文件名前缀
OUTPUT_NAME = "output"

# ── 反爬延迟配置（秒） ──
# 频道与频道之间的等待时间（随机取区间内值）
DELAY_BETWEEN_CHANNELS_MIN = 4
DELAY_BETWEEN_CHANNELS_MAX = 8

# 翻页前的等待时间
DELAY_BEFORE_PAGING_MIN = 3
DELAY_BEFORE_PAGING_MAX = 6

# 每次浏览器操作之间的等待时间
DELAY_BROWSER_ACTION_MIN = 2
DELAY_BROWSER_ACTION_MAX = 5

# 页面加载后的等待时间
DELAY_PAGE_LOAD_MIN = 2
DELAY_PAGE_LOAD_MAX = 4

# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = os.path.join(SCRIPT_DIR, ".openclaw", "tmp")


# ─── 浏览器工具 ──────────────────────────────────────────────

def run(cmd, timeout=15):
    """执行 shell 命令，返回 stdout。"""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def browser_eval(js):
    """在浏览器中执行 JS，返回结果（自动处理 JSON 引号）。"""
    js_escaped = js.replace('"', '\\"')
    raw = run(f'agent-browser eval "{js_escaped}"', timeout=20)
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


# ─── 随机延迟（反爬核心） ────────────────────────────────────

def human_delay(min_s=None, max_s=None):
    """模拟人类操作的随机延迟，避免固定节奏被识别为爬虫。"""
    if min_s is None:
        min_s = DELAY_BROWSER_ACTION_MIN
    if max_s is None:
        max_s = DELAY_BROWSER_ACTION_MAX
    time.sleep(random.uniform(min_s, max_s))


# ─── HTML 解析 ────────────────────────────────────────────────

def parse_results(html):
    """
    解析搜索结果页面的 HTML。
    返回 (items, total_count)。
    
    注意：网站的 CSS 类名是动态变化的（反爬措施），
    所以这里基于 HTML 标签结构而非固定类名来解析。
    
    每个结果的结构：
      <div class="resultplus">
        <div class="channel">
          <div class="tip" style="float: left;">频道名</div>
        </div>
        <div class="xxx">            ← 类名动态变化
          <tba class="xxx"></tba>    ← 第1个 tba：复制按钮（空）
          <tba class="yyy">URL</tba> ← 第2个 tba：直播链接
        </div>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    result_divs = soup.find_all("div", class_="resultplus")

    total_count = 0
    items = []

    for i, div in enumerate(result_divs):
        # 第一个 div 是统计头部，格式如 "About 1766 results"
        if i == 0:
            channel_div = div.find("div", class_="channel")
            if channel_div:
                text = channel_div.get_text(strip=True)
                m = re.search(r"(\d+)\s*results?", text)
                if m:
                    total_count = int(m.group(1))
            continue

        # ── 提取频道名称 ──
        channel_div = div.find("div", class_="channel")
        if not channel_div:
            continue
        # 频道名在 style="float: left;" 的 tip div 中
        name_div = channel_div.find(
            "div", class_="tip", style=lambda s: s and "float: left" in s
        )
        if not name_div:
            continue
        name = name_div.get_text(strip=True)

        # ── 方法1: 从 <tba> 标签提取链接 ──
        # 跳过空的 tba（复制按钮），取包含 http 的那个
        found = False
        for tba in div.find_all("tba"):
            url = tba.get_text(strip=True)
            if url and url.startswith("http"):
                items.append({"name": name, "url": url})
                found = True
                break
        if found:
            continue

        # ── 方法2: 从 onclick 属性提取链接 ──
        # 函数名也是动态的，直接用正则匹配 URL
        for elem in div.find_all(onclick=True):
            onclick = elem.get("onclick", "")
            m = re.search(r'https?://[^\s"\']+', onclick)
            if m:
                url = m.group(0).replace("&amp;", "&")
                items.append({"name": name, "url": url})
                break

    return items, total_count


def extract_page_hash(html):
    """从翻页链接中提取 l 参数（hash），用于后续翻页请求。"""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("a", href=re.compile(r"page=2"))
    if link:
        m = re.search(r"l=([a-f0-9]+)", link["href"])
        if m:
            return m.group(1)
    return ""


# ─── 频道文件解析 ─────────────────────────────────────────────

def parse_channel_file(filepath):
    """
    解析频道列表文件。
    返回分组列表: [{"group": "央视", "channels": ["CCTV-1", ...]}, ...]
    
    文件格式：
      分组名,#genre#        ← 分组标题行
      CCTV-1                ← 频道名（每行一个）
      CCTV-2
    """
    groups = []
    current_group = None

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 分组行：包含 ,#genre#
            if ",#genre#" in line.lower():
                group_name = line.split(",")[0].strip()
                # 去掉 emoji 前缀（保留中文名）
                group_name = re.sub(r'^[^\w]+', '', group_name).strip()
                current_group = {"group": group_name, "channels": []}
                groups.append(current_group)
            elif current_group is not None:
                # 频道名
                if line:
                    current_group["channels"].append(line)

    return groups


# ─── 单频道爬取 ────────────────────────────────────────────────

def search_channel(keyword, max_pages=1, max_links=0):
    """
    搜索单个频道，返回直播链接列表。
    
    参数:
      keyword:   频道名（如 "CCTV-1"）
      max_pages: 最多爬几页
      max_links: 最多返回几条（0=不限）
    """
    # ── 打开网站首页 ──
    run(f'agent-browser open "{BASE_URL}"', timeout=30)
    human_delay(DELAY_PAGE_LOAD_MIN, DELAY_PAGE_LOAD_MAX)

    # ── 查找搜索输入框 ──
    snapshot = run("agent-browser snapshot -i", timeout=10)
    search_ref = None
    for line in snapshot.split("\n"):
        if "textbox" in line.lower():
            m = re.search(r"\[ref=(e\d+)\]", line)
            if m:
                search_ref = f"@{m.group(1)}"
                break

    if not search_ref:
        return []

    # ── 填写关键词并搜索 ──
    run(f'agent-browser fill {search_ref} "{keyword}"')
    human_delay(0.5, 1.5)
    run("agent-browser press Enter")
    human_delay(DELAY_PAGE_LOAD_MIN, DELAY_PAGE_LOAD_MAX)

    # ── 解析第 1 页 ──
    html = browser_eval("document.documentElement.outerHTML")
    if not html or len(html) < 100:
        return []

    items, total = parse_results(html)
    page_hash = extract_page_hash(html)
    total_pages = (total + 29) // 30 if total > 0 else 1
    actual_pages = min(max_pages, total_pages)

    all_items = list(items)
    seen_urls = {item["url"] for item in items}

    # ── 翻页 ──
    for page in range(2, actual_pages + 1):
        human_delay(DELAY_BEFORE_PAGING_MIN, DELAY_BEFORE_PAGING_MAX)

        if page_hash:
            page_url = f"{BASE_URL}/?page={page}&iptv={urllib.parse.quote(keyword)}&l={page_hash}"
        else:
            page_url = f"{BASE_URL}/?page={page}&iptv={keyword}"

        run(f'agent-browser open "{page_url}"', timeout=30)
        human_delay(DELAY_PAGE_LOAD_MIN, DELAY_PAGE_LOAD_MAX)

        html = browser_eval("document.documentElement.outerHTML")
        if not html or len(html) < 100:
            break

        items, _ = parse_results(html)
        for item in items:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_items.append(item)

        if not items:
            break

    # ── 过滤精确匹配 ──
    # CCTV-1 → 匹配 CCTV1, CCTV-1, CCTV1综合 等
    # 但排除 CCTV10, CCTV11, CCTV13 等
    escaped = re.escape(keyword)
    # 允许关键词中的 - 和空格互换匹配
    flexible = escaped.replace(r"\-", r"[-\s]?").replace(r"\ ", r"[-\s]?")
    pattern = re.compile(rf"^{flexible}(\s*综合)?(\s*\(.*\))?$", re.IGNORECASE)
    filtered = [item for item in all_items if pattern.match(item["name"])]

    # 过滤后为空则用全部结果
    if not filtered:
        filtered = all_items

    # 限制数量
    if max_links > 0:
        filtered = filtered[:max_links]

    return filtered


# ─── 输出保存 ──────────────────────────────────────────────────

def save_txt2tvlive(results, filename="output.txt"):
    """
    保存为 txt2tvlive 格式（可直接导入相关 IPTV 工具）:
      分组名,#genre#
      频道名,链接
    """
    with open(filename, "w", encoding="utf-8") as f:
        for group in results:
            f.write(f"{group['group']},#genre#\n")
            for ch in group["channels"]:
                f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")
    print(f"\n已保存: {filename}")


def save_m3u(results, filename="output.m3u"):
    """保存为 m3u 播放列表格式（带分组信息）。"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for group in results:
            for ch in group["channels"]:
                f.write(f'#EXTINF:-1 group-title="{group["group"]}",{ch["name"]}\n')
                f.write(f'{ch["url"]}\n')
    print(f"已保存: {filename}")


# ─── 主流程 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="tonkiang.us 批量直播源爬虫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 cctv1_scraper.py                     # 按 demo.txt 爬取
  python3 cctv1_scraper.py -f my_channels.txt  # 指定频道文件
  python3 cctv1_scraper.py -p 2 -n 5           # 每频道2页、最多5条
  python3 cctv1_scraper.py -k "CCTV-1"         # 只爬单个频道
        """,
    )
    parser.add_argument("-f", "--file", default=CHANNEL_FILE, help=f"频道列表文件 (默认: {CHANNEL_FILE})")
    parser.add_argument("-p", "--pages", type=int, default=MAX_PAGES, help=f"每频道爬几页 (默认: {MAX_PAGES})")
    parser.add_argument("-n", "--max-links", type=int, default=MAX_LINKS, help=f"每频道最多几条,0不限 (默认: {MAX_LINKS})")
    parser.add_argument("-k", "--keyword", default="", help="只爬单个频道 (如: CCTV-1)")
    parser.add_argument("-o", "--output", default=OUTPUT_NAME, help=f"输出文件名前缀 (默认: {OUTPUT_NAME})")
    args = parser.parse_args()

    # ── 解析频道列表 ──
    if args.keyword:
        # 单频道模式
        groups = [{"group": "自定义", "channels": [args.keyword]}]
    else:
        if not os.path.exists(args.file):
            print(f"文件不存在: {args.file}")
            sys.exit(1)
        groups = parse_channel_file(args.file)

    total_channels = sum(len(g["channels"]) for g in groups)
    print(f"频道文件: {args.file if not args.keyword else '单频道模式'}")
    print(f"分组数: {len(groups)} | 频道总数: {total_channels}")
    print(f"每频道: 最多 {args.pages} 页 | 最多 {args.max_links or '不限'} 条")
    print(f"延迟配置: 频道间 {DELAY_BETWEEN_CHANNELS_MIN}-{DELAY_BETWEEN_CHANNELS_MAX}s | "
          f"翻页间 {DELAY_BEFORE_PAGING_MIN}-{DELAY_BEFORE_PAGING_MAX}s")
    print("=" * 60)

    # ── 打开浏览器 ──
    run(f'agent-browser open "{BASE_URL}"', timeout=30)
    human_delay(3, 5)

    results = []
    channel_count = 0

    for group in groups:
        group_result = {"group": group["group"], "channels": []}

        for keyword in group["channels"]:
            channel_count += 1
            print(f"\n[{channel_count}/{total_channels}] 搜索: {keyword}")

            try:
                items = search_channel(
                    keyword,
                    max_pages=args.pages,
                    max_links=args.max_links,
                )
            except Exception as e:
                print(f"  ❌ 错误: {e}")
                items = []

            if items:
                print(f"  ✅ 找到 {len(items)} 条")
                for item in items:
                    group_result["channels"].append({
                        "name": item["name"],
                        "url": item["url"],
                    })
            else:
                print(f"  ⚠️  未找到结果")

            # 频道间随机延迟（反爬关键）
            if channel_count < total_channels:
                delay = random.uniform(DELAY_BETWEEN_CHANNELS_MIN, DELAY_BETWEEN_CHANNELS_MAX)
                print(f"  ⏳ 等待 {delay:.1f}s...")
                time.sleep(delay)

        if group_result["channels"]:
            results.append(group_result)

    # ── 关闭浏览器 ──
    run("agent-browser close", timeout=5)

    # ── 统计并保存 ──
    total_links = sum(len(g["channels"]) for g in results)
    print("\n" + "=" * 60)
    print(f"爬取完成! {len(results)} 个分组, {total_links} 条链接")

    if total_links > 0:
        save_txt2tvlive(results, f"{args.output}.txt")
        save_m3u(results, f"{args.output}.m3u")
    else:
        print("无结果可保存")


if __name__ == "__main__":
    main()
