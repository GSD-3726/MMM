#!/usr/bin/env python3
"""
iptv-search.com 批量直播源爬虫
================================
纯 HTTP 请求，无需浏览器，可在 GitHub Actions 中运行。
数据源: https://iptv-search.com

用法:
  python3 main.py                          # 按 demo.txt 爬取
  python3 main.py -f demo.txt              # 指定频道文件
  python3 main.py -p 2                     # 每频道取前2页结果
  python3 main.py -n 5                     # 每频道最多5条
  python3 main.py -k "CCTV-1"             # 只爬单个频道
  python3 main.py -p 0 -n 0               # 不限制

参数说明:
  -p 页数    每频道取几页搜索结果（每页约20条），0=不限制（默认1）
  -n 条数    每频道最多保留几条流地址，0=不限制（默认3）
"""

import argparse
import json
import os
import re
import sys
import time
import random

import requests

# ============================================================
#  ★ 配置区
# ============================================================

# 数据源网站
BASE_URL = "https://iptv-search.com"

# 频道列表文件
CHANNEL_FILE = "demo.txt"

# 每频道取几页搜索结果（每页约20条），0 = 不限制
PAGES = 1

# 每频道最多保留几条流地址，0 = 不限制
MAX_LINKS = 3

# 输出文件名前缀
OUTPUT_NAME = "output"

# 请求间隔（秒）
DELAY_MIN = 0.5
DELAY_MAX = 1.5

# HTTP 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ============================================================


def delay():
    """随机延迟。"""
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# ─── 频道文件解析 ─────────────────────────────────────────────

def parse_channel_file(filepath):
    """解析频道列表文件，返回分组列表。"""
    groups = []
    current_group = None

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if ",#genre#" in line.lower():
                group_name = line.split(",")[0].strip()
                group_name = re.sub(r'^[^\w]+', '', group_name).strip()
                current_group = {"group": group_name, "channels": []}
                groups.append(current_group)
            elif current_group is not None:
                if line:
                    current_group["channels"].append(line)

    return groups


# ─── API 调用 ──────────────────────────────────────────────────

def search_channels(keyword, limit=20):
    """
    搜索频道，返回去重后的频道列表。
    每项: {"name": str, "url": str, "category": str}
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/api/search",
            params={"q": keyword, "limit": limit},
            headers=HEADERS,
            timeout=15,
        )
        print(f"    API 状态: {resp.status_code}", flush=True)
        if resp.status_code != 200:
            print(f"    API 响应: {resp.text[:200]}", flush=True)
            return []
        data = resp.json()
    except Exception as e:
        print(f"    搜索 API 错误: {e}", flush=True)
        return []

    seen = set()
    channels = []
    for item in data.get("itemListElement", []):
        name = item.get("name", "")
        url = item.get("url", "")
        category = item.get("description", "")
        if name and url and name not in seen:
            seen.add(name)
            channels.append({"name": name, "url": url, "category": category})

    return channels


def get_channel_hash(channel_url):
    """访问频道页面，提取 CURRENT_CHANNEL_HASH。"""
    try:
        resp = requests.get(channel_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"    频道页状态: {resp.status_code}", flush=True)
            return None
        m = re.search(r"CURRENT_CHANNEL_HASH\s*=\s*['\"]([^'\"]+)['\"]", resp.text)
        if m:
            return m.group(1)
        else:
            print(f"    频道页无 hash，HTML 长度: {len(resp.text)}", flush=True)
    except Exception as e:
        print(f"    获取 hash 错误: {e}", flush=True)
    return None


def get_stream_url(hash_val):
    """
    通过 hash 获取实际流媒体 URL。
    调用 /api/play/link → 跟踪重定向 → 返回最终 m3u8 URL。
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/api/play/link",
            params={"hash": hash_val},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"    play/link 状态: {resp.status_code} 响应: {resp.text[:100]}", flush=True)
            return None
        data = resp.json()

        if not data.get("success") or not data.get("play_link"):
            return None

        # 不能带 Accept 头，否则 404
        resp = requests.get(
            data["play_link"],
            headers={"User-Agent": HEADERS["User-Agent"]},
            allow_redirects=True,
            timeout=15,
        )
        content_type = resp.headers.get("content-type", "")

        if "mpegurl" in content_type or resp.text.strip().startswith("#EXTM3U"):
            return resp.url

    except Exception as e:
        print(f"    获取流地址错误: {e}", flush=True)

    return None


# ─── 单频道爬取 ────────────────────────────────────────────────

def search_single_channel(keyword, pages=1, max_links=0):
    """
    搜索单个频道，返回流地址列表。

    停止条件（满足任一即停）:
      - 已获取的流地址数 >= max_links（max_links>0 时生效）
      - 已处理的候选频道数 >= pages*20（pages>0 时生效）
      - 两个都设为 0 则不限制

    参数:
      keyword:   频道名
      pages:     取几页搜索结果（每页约20条），0=不限制
      max_links: 最多返回几条流地址，0=不限制
    """
    # 计算搜索数量上限
    if pages <= 0:
        search_limit = 100  # API 最大值
    else:
        search_limit = pages * 20

    # 是否启用各限制
    has_link_limit = max_links > 0
    has_page_limit = pages > 0

    # 搜索关键词预处理：API 不识别横杠，去掉后搜索
    search_kw = keyword.replace("-", "").replace(" ", "")

    # 搜索频道
    channels = search_channels(search_kw, limit=search_limit)
    if not channels:
        return []

    # 精确匹配
    escaped = re.escape(keyword)
    flexible = escaped.replace(r"\-", r"[-\s]?").replace(r"\ ", r"[-\s]?")
    pattern = re.compile(rf"^{flexible}(\s*综合)?$", re.IGNORECASE)

    exact = [ch for ch in channels if pattern.match(ch["name"])]
    candidates = exact if exact else channels

    results = []
    seen_urls = set()
    checked = 0  # 已检查的候选频道数

    for ch in candidates:
        # ── 停止条件1: 条数已满 ──
        if has_link_limit and len(results) >= max_links:
            break

        # ── 停止条件2: 页数已满 ──
        if has_page_limit and checked >= search_limit:
            break

        checked += 1

        # 获取 hash
        delay()
        hash_val = get_channel_hash(ch["url"])
        if not hash_val:
            continue

        # 获取流地址
        delay()
        stream_url = get_stream_url(hash_val)
        if not stream_url:
            continue

        if stream_url not in seen_urls:
            seen_urls.add(stream_url)
            results.append({
                "name": ch["name"],
                "url": stream_url,
                "category": ch["category"],
            })

    return results


# ─── 输出保存 ──────────────────────────────────────────────────

def save_txt2tvlive(results, filename="output.txt"):
    """保存为 txt2tvlive 格式。"""
    with open(filename, "w", encoding="utf-8") as f:
        for group in results:
            f.write(f"{group['group']},#genre#\n")
            for ch in group["channels"]:
                f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")
    print(f"\n已保存: {filename}", flush=True)


def save_m3u(results, filename="output.m3u"):
    """保存为 m3u 格式。"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for group in results:
            for ch in group["channels"]:
                f.write(f'#EXTINF:-1 group-title="{group["group"]}",{ch["name"]}\n')
                f.write(f'{ch["url"]}\n')
    print(f"已保存: {filename}", flush=True)


# ─── 主流程 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="iptv-search.com 批量直播源爬虫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-f", "--file", default=CHANNEL_FILE, help=f"频道列表文件 (默认: {CHANNEL_FILE})")
    parser.add_argument("-p", "--pages", type=int, default=PAGES, help=f"每频道取几页结果,0不限 (默认: {PAGES})")
    parser.add_argument("-n", "--max-links", type=int, default=MAX_LINKS, help=f"每频道最多几条,0不限 (默认: {MAX_LINKS})")
    parser.add_argument("-k", "--keyword", default="", help="只爬单个频道")
    parser.add_argument("-o", "--output", default=OUTPUT_NAME, help=f"输出文件名前缀 (默认: {OUTPUT_NAME})")
    args = parser.parse_args()

    # 解析频道列表
    if args.keyword:
        groups = [{"group": "自定义", "channels": [args.keyword]}]
    else:
        if not os.path.exists(args.file):
            print(f"文件不存在: {args.file}", flush=True)
            sys.exit(1)
        groups = parse_channel_file(args.file)

    total_channels = sum(len(g["channels"]) for g in groups)
    print(f"数据源: {BASE_URL}", flush=True)
    print(f"频道文件: {args.file if not args.keyword else '单频道模式'}", flush=True)
    print(f"分组数: {len(groups)} | 频道总数: {total_channels}", flush=True)
    print(f"配置: 每频道 {args.pages if args.pages > 0 else '不限'} 页 | 每频道最多 {args.max_links if args.max_links > 0 else '不限'} 条", flush=True)
    print("=" * 60, flush=True)

    results = []
    channel_count = 0

    for group in groups:
        group_result = {"group": group["group"], "channels": []}

        for keyword in group["channels"]:
            channel_count += 1
            print(f"\n[{channel_count}/{total_channels}] 搜索: {keyword}", flush=True)

            try:
                items = search_single_channel(keyword, pages=args.pages, max_links=args.max_links)
            except Exception as e:
                print(f"  ❌ 错误: {e}", flush=True)
                items = []

            if items:
                print(f"  ✅ 找到 {len(items)} 条", flush=True)
                for item in items:
                    print(f"     {item['name']}: {item['url'][:80]}...", flush=True)
                    group_result["channels"].append({
                        "name": item["name"],
                        "url": item["url"],
                    })
            else:
                print(f"  ⚠️  未找到结果", flush=True)

        if group_result["channels"]:
            results.append(group_result)

    # 统计
    total_links = sum(len(g["channels"]) for g in results)
    print("\n" + "=" * 60, flush=True)
    print(f"爬取完成! {len(results)} 个分组, {total_links} 条链接", flush=True)

    if total_links > 0:
        save_txt2tvlive(results, f"{args.output}.txt")
        save_m3u(results, f"{args.output}.m3u")
    else:
        print("无结果可保存", flush=True)


if __name__ == "__main__":
    main()
