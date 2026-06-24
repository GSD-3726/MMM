#!/usr/bin/env python3
"""
iptv-search.com 批量直播源爬虫 + ffmpeg 异步测速 + 本地缓存
"""

import asyncio
import json
import os
import re
import sys
import time
import random
import shutil
import requests
from collections import defaultdict

# ============================================================
#  ★ 配置区
# ============================================================
CHANNEL_FILE = "demo.txt"          # 频道列表文件路径
PAGES = 10                          # 每频道抓取搜索结果页数
MAX_LINKS = 20                     # 每频道最多提取直播源条数
TOP_N = 5                          # 测速后每频道保留最优源数量
FFMPEG_TIMEOUT = 20                # ffmpeg测速超时时间（秒）
FFMPEG_DURATION = 3                # ffmpeg测速探测时长（秒）
FFMPEG_CONCURRENCY = 3             # ffmpeg测速并发数
OUTPUT_DIR = "output"              # 结果输出目录

ENABLE_CACHE = True                # 是否启用测速结果缓存
CACHE_FILE = "speed_cache.json"    # 测速缓存文件路径
CACHE_EXPIRE_HOURS = 72            # 测速缓存有效时长（小时）

MAX_RETRY = 2                      # 请求失败最大重试次数
RETRY_WAIT = 5                     # 重试等待时间基数（秒）
REQUEST_INTERVAL = (1.5, 2.5)      # 普通请求随机间隔范围（秒）
DETAIL_INTERVAL = (2.0, 3.5)       # 详情页请求随机间隔范围（秒）
# ============================================================

BASE_URL = "https://iptv-search.com"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

CHANNEL_SUFFIX_MAP = {
    '10': '科教', '13': '新闻', '14': '少儿', '15': '音乐', '5+': 'HD',
}
EXTRA_SUFFIX_MAP = {
    '6': '电影', '7': '国防军事', '11': '戏曲', '12': '社会与法',
}

main_session = requests.Session()

def get_common_headers():
    return {"User-Agent": DEFAULT_UA, "Referer": f"{BASE_URL}/"}

def get_api_headers():
    h = get_common_headers()
    h["Accept"] = "application/json, text/javascript, */*; q=0.01"
    return h

def safe_request(url, is_api=False, **kwargs):
    headers = get_api_headers() if is_api else get_common_headers()
    for i in range(MAX_RETRY + 1):
        try:
            if i == 0:
                time.sleep(random.uniform(*REQUEST_INTERVAL))
            else:
                time.sleep(RETRY_WAIT * (i + 1))
            
            r = main_session.get(url, headers=headers, timeout=20, **kwargs)
            if r.status_code == 503 and i < MAX_RETRY:
                print(f"    ⚠️  503超限，等待后重试({i+1}/{MAX_RETRY})", flush=True)
                continue
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if i < MAX_RETRY:
                continue
    return None

# ─── 基础工具函数 ─────────────────────────────────────────────
def parse_channel_file(filepath):
    groups, current = [], None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            if ",#genre#" in line.lower():
                current = {"group": line.split(",")[0].strip(), "channels": []}
                groups.append(current)
            elif current:
                current["channels"].append(line)
    return groups

def normalize_name(name):
    if not name: return name
    name = name.strip().upper()
    m = re.match(r'^CCTV-?(\d+\+?)', name)
    return f"CCTV-{m.group(1)}" if m else name

def extract_cctv_number(keyword):
    m = re.match(r'^CCTV-?(\d+\+?)', keyword.strip().upper())
    return m.group(1) if m else ''

def generate_search_variants(keyword):
    num = extract_cctv_number(keyword)
    if not num:
        return [keyword.replace('-', '').upper()]
    base = f"CCTV{num}"
    variants = [base]
    if num in CHANNEL_SUFFIX_MAP:
        variants.append(f"{base}{CHANNEL_SUFFIX_MAP[num]}")
    if num in EXTRA_SUFFIX_MAP:
        variants.append(f"{base}{EXTRA_SUFFIX_MAP[num]}")
    return list(dict.fromkeys(variants))

def is_exact_match(channel_name, target_num):
    name_clean = channel_name.lower().replace('-', '').replace(' ', '')
    pattern = re.compile(rf'^cctv{re.escape(target_num.lower())}(?!\d)')
    return bool(pattern.search(name_clean))

# ─── 爬取核心三步 ─────────────────────────────────────────────
def search_channels(keyword, limit=20):
    r = safe_request(
        f"{BASE_URL}/api/search",
        is_api=True,
        params={"q": keyword, "limit": limit}
    )
    if not r or r.status_code != 200:
        return []
    try:
        resp = r.json()
    except Exception:
        return []
    
    items = resp.get("itemListElement", [])
    if not items and isinstance(resp.get("data", {}).get("results"), list):
        items = resp["data"]["results"]
    
    result, seen = [], set()
    for it in items:
        n, u = it.get("name", ""), it.get("url", "")
        if n and u and n not in seen:
            if u.startswith("/"): u = BASE_URL + u
            seen.add(n)
            result.append({"name": n, "url": u})
    return result

def get_hash(detail_url):
    time.sleep(random.uniform(*DETAIL_INTERVAL))
    r = safe_request(detail_url)
    if not r or r.status_code != 200:
        return None
    m = re.search(r"CURRENT_CHANNEL_HASH\s*=\s*['\"]([^'\"]+)['\"]", r.text)
    return m.group(1) if m else None

def get_stream(hash_val):
    """直接返回播放链接，VLC/FFmpeg均可正常播放"""
    try:
        r = main_session.get(
            f"{BASE_URL}/api/play/link",
            params={"hash": hash_val},
            headers={**get_common_headers(), "Accept": "application/json"},
            timeout=15
        )
        if r.status_code != 200:
            return None
        resp = r.json()
        if resp.get("success") and resp.get("play_link"):
            return resp["play_link"]
    except Exception:
        pass
    return None

# ─── 单频道爬取与排序 ─────────────────────────────────────────────
QUALITY_ORDER = {'4k':10, '1080p':9, '高清':9, 'hd':9, '720p':8, '高码':7, '标清':5}

def quality_score(name):
    lower = name.lower()
    score = max((s for kw, s in QUALITY_ORDER.items() if kw in lower), default=0)
    return score + 1 if '综合' in name else score

def crawl_channel(keyword, pages=1, max_links=0):
    limit = 100 if pages <= 0 else pages * 20
    has_link = max_links > 0
    target_num = extract_cctv_number(keyword)

    variants = generate_search_variants(keyword)
    all_channels, url_seen = [], set()
    for v in variants:
        for ch in search_channels(v, limit=limit):
            if ch["url"] not in url_seen:
                url_seen.add(ch["url"])
                all_channels.append(ch)
        if len(all_channels) >= limit: break

    if not all_channels: return []
    
    if target_num:
        matched = [ch for ch in all_channels if is_exact_match(ch["name"], target_num)]
    else:
        matched = all_channels
    
    cands = matched if matched else all_channels
    cands.sort(key=lambda c: quality_score(c["name"]), reverse=True)

    results, seen, checked = [], set(), 0
    for ch in cands:
        if has_link and len(results) >= max_links: break
        checked += 1
        print(f"    [{checked}] {ch['name']}...", end=" ", flush=True)
        h = get_hash(ch["url"])
        if not h:
            print("✗(无Hash)", flush=True)
            continue
        u = get_stream(h)
        if not u:
            print("✗(无流)", flush=True)
            continue
        if u not in seen:
            seen.add(u)
            results.append({"name": normalize_name(keyword), "url": u})
            print("✓", flush=True)
        else:
            print("=(重复)", flush=True)
    return results

# ─── 缓存与异步测速 ──────────────────────────────────────
CACHE_EXPIRE_SECONDS = CACHE_EXPIRE_HOURS * 3600

def load_cache():
    if not ENABLE_CACHE or not os.path.exists(CACHE_FILE): return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        now = time.time()
        return {u:d for u,d in cache.items()
                if isinstance(d,dict) and now - d.get("timestamp",0) < CACHE_EXPIRE_SECONDS}
    except Exception:
        return {}

def save_cache(cache):
    if not ENABLE_CACHE: return
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception: pass

async def test_stream_async(url, timeout=10, duration=3):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-rw_timeout", str(timeout * 1000000),
        "-analyzeduration", "1500000", "-probesize", "1500000",
        "-i", url, "-t", str(duration),
        "-c", "copy", "-f", "null", "-"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout + duration + 3)
        except asyncio.TimeoutError:
            proc.kill()
            return False, 0
        
        stderr = stderr_bytes.decode("utf-8", errors="ignore")
        bitrate = 0
        matches = re.findall(r"bitrate=\s*([\d.]+)\s*kbits/s", stderr)
        if matches: bitrate = float(matches[-1])
        return (True, bitrate) if bitrate > 50 else (False, bitrate)
    except Exception:
        return False, 0

async def speed_filter_async(items, top_n=2):
    if top_n <= 0: return items
    by_name = defaultdict(list)
    for item in items: by_name[item["name"]].append(item)

    cache = load_cache()
    updates = {}
    result = []
    sem = asyncio.Semaphore(FFMPEG_CONCURRENCY)

    async def test_one(item):
        url = item["url"]
        if url in cache and cache[url].get("ok"):
            return {**item, "_br": cache[url].get("bitrate", 100)}
        async with sem:
            ok, br = await test_stream_async(url, FFMPEG_TIMEOUT, FFMPEG_DURATION)
            updates[url] = {"ok": ok, "bitrate": br, "timestamp": time.time()}
            return {**item, "_br": br} if ok else None

    for name, chs in by_name.items():
        print(f"  测速: {name}", flush=True)
        tasks = [asyncio.create_task(test_one(c)) for c in chs]
        tested = []
        for coro in asyncio.as_completed(tasks):
            res = await coro
            if res: tested.append(res)
        tested.sort(key=lambda x: x["_br"], reverse=True)
        top = tested[:top_n]
        for it in top: it.pop("_br", None)
        result.extend(top)
    
    if updates:
        cache.update(updates)
        save_cache(cache)
    return result

# ─── 输出与主流程 ──────────────────────────────────────────────
def save_output(results, outdir):
    if os.path.exists(outdir): shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)
    txt = os.path.join(outdir, "output.txt")
    m3u = os.path.join(outdir, "output.m3u")
    with open(txt, "w", encoding="utf-8") as f:
        for g in results:
            f.write(f"{g['group']},#genre#\n")
            for ch in g["channels"]: f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")
    with open(m3u, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for g in results:
            for ch in g["channels"]:
                f.write(f'#EXTINF:-1 group-title="{g["group"]}",{ch["name"]}\n{ch["url"]}\n')
    return txt, m3u

async def main():
    if not os.path.exists(CHANNEL_FILE):
        print(f"文件不存在: {CHANNEL_FILE}"); sys.exit(1)

    print("初始化会话...", flush=True)
    try:
        main_session.get(BASE_URL, headers=get_common_headers(), timeout=15)
    except Exception:
        pass

    groups = parse_channel_file(CHANNEL_FILE)
    total = sum(len(g["channels"]) for g in groups)
    print(f"待爬频道: {total} 个 | 每频道最多 {MAX_LINKS} 条 | 保留前 {TOP_N} 条", flush=True)

    results = []
    n = 0
    for g in groups:
        gr = {"group": g["group"], "channels": []}
        for kw in g["channels"]:
            n += 1
            print(f"\n[{n}/{total}] {kw}", flush=True)
            try:
                items = crawl_channel(kw, pages=PAGES, max_links=MAX_LINKS)
            except Exception as e:
                print(f"❌ 异常: {e}"); items = []
            if items:
                print(f"✅ 成功 {len(items)} 条")
                gr["channels"].extend(items)
            else:
                print("⚠️ 无结果")
        if gr["channels"]: results.append(gr)

    crawled = sum(len(g["channels"]) for g in results)
    print(f"\n爬取完成: {len(results)} 组，共 {crawled} 条", flush=True)
    if crawled == 0:
        print("未获取到有效源"); return

    if TOP_N > 0:
        all_ch = [ch for g in results for ch in g["channels"]]
        stable = await speed_filter_async(all_ch, top_n=TOP_N)
        urls = {ch["url"] for ch in stable}
        results = [
            {"group":g["group"], "channels":[c for c in g["channels"] if c["url"] in urls]}
            for g in results if any(c["url"] in urls for c in g["channels"])
        ]

    txt, m3u = save_output(results, OUTPUT_DIR)
    print(f"\n结果已保存:\n  TXT: {txt}\n  M3U: {m3u}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
