#!/usr/bin/env python3
"""
iptv-search.com 批量直播源爬虫 + ffmpeg 异步并发测速 + 本地缓存
================================================================
流程: 爬取 → 读取缓存 → 并发测速(仅测未缓存) → 按码率保留前 N 个 → 输出
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

# 频道列表文件
CHANNEL_FILE = "demo.txt"

# 每频道取几页搜索结果（每页约20条），0 = 不限制
PAGES = 1

# 每频道最多爬几条，0 = 不限制
MAX_LINKS = 10

# 测速后每频道保留前 N 个，0 = 不测速（全部保留）
TOP_N = 5

# ffmpeg 测试超时（秒）
FFMPEG_TIMEOUT = 10

# ffmpeg 读取秒数 (建议 5-8 秒)
FFMPEG_DURATION = 6

# 测速并发数 (GitHub Actions 建议 4-6，本地可 10+)
FFMPEG_CONCURRENCY = 5

# 输出目录
OUTPUT_DIR = "output"

# ─── 缓存配置 ────────────────────────────────────────────────
ENABLE_CACHE = True          # 是否启用测速缓存
CACHE_FILE = "speed_cache.json"
CACHE_EXPIRE_HOURS = 72      # 缓存过期时间（小时）

# ============================================================

BASE_URL = "https://iptv-search.com"

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

DELAY_FAST = (0.3, 0.8)
DELAY_SLOW = (0.8, 2.0)

def get_ua(): return random.choice(UA_LIST)

def get_headers():
    return {
        "User-Agent": get_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

def delay_fast(): time.sleep(random.uniform(*DELAY_FAST))
def delay_slow(): time.sleep(random.uniform(*DELAY_SLOW))

# ─── 频道文件解析 ─────────────────────────────────────────────
def parse_channel_file(filepath):
    groups = []
    current = None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            if ",#genre#" in line.lower():
                name = line.split(",")[0].strip()
                name = re.sub(r'^[^\w]+', '', name).strip()
                current = {"group": name, "channels": []}
                groups.append(current)
            elif current and line:
                current["channels"].append(line)
    return groups

# ─── 名称清洗 ─────────────────────────────────────────────────
def normalize_name(name):
    if not name: return name
    name = name.strip()
    cn_num = {'一':'1','二':'2','三':'3','四':'4','五':'5','六':'6','七':'7','八':'8','九':'9','十':'10','十一':'11','十二':'12','十三':'13','十四':'14','十五':'15','十六':'16','十七':'17'}
    def cn_to_num(m):
        num = m.group(1)
        return f'CCTV-{num}' if num.isdigit() else f'CCTV-{cn_num.get(num, num)}'
    name = re.sub(r'^央视(\d|[一二三四五六七八九十]+)[台频道]*$', cn_to_num, name)
    name = re.sub(r'^CCTV(\d)', r'CCTV-\1', name, flags=re.IGNORECASE)
    name = re.sub(r'^(CCTV-\d+)[频道]+$', r'\1', name)
    for suffix in ['咪咕', '港澳版', '港澳', '高码', '高码率']:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name.strip()

# ─── API 爬取逻辑 (完全保留原版) ──────────────────────────────
def search_channels(keyword, limit=20):
    try:
        r = requests.get(f"{BASE_URL}/api/search", params={"q": keyword, "limit": limit}, headers=get_headers(), timeout=15)
        if r.status_code != 200: return []
        data = r.json()
    except Exception: return []
    seen, out = set(), []
    for it in data.get("itemListElement", []):
        n, u = it.get("name",""), it.get("url","")
        if n and u and n not in seen:
            seen.add(n)
            out.append({"name": n, "url": u})
    return out

def get_hash(url):
    try:
        r = requests.get(url, headers=get_headers(), timeout=15)
        if r.status_code != 200: return None
        m = re.search(r"CURRENT_CHANNEL_HASH\s*=\s*['\"]([^'\"]+)['\"]", r.text)
        return m.group(1) if m else None
    except Exception: return None

def get_stream(hash_val):
    ua = get_ua()
    try:
        r = requests.get(f"{BASE_URL}/api/play/link", params={"hash": hash_val}, headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200: return None
        d = r.json()
        if not d.get("success") or not d.get("play_link"): return None
        r = requests.get(d["play_link"], headers={"User-Agent": ua}, allow_redirects=False, timeout=15)
        if r.status_code in (301,302,303,307,308):
            loc = r.headers.get("Location","")
            if loc.startswith("http"): return loc
        ct = r.headers.get("content-type","")
        if "mpegurl" in ct or r.text.strip().startswith("#EXTM3U"): return r.url
    except Exception: pass
    return None

QUALITY_ORDER = {'4k': 10, '2160p': 10, '1080p': 9, '1080': 9, '高清': 9, 'hd': 9, 'fhd': 9, '720p': 8, '720': 8, '高码': 7, '高码率': 7, '480p': 5, '480': 5, '标清': 5, 'sd': 5}

def quality_score(name):
    lower = name.lower()
    score = max((s for kw, s in QUALITY_ORDER.items() if kw in lower), default=0)
    if '综合' in name: score += 1
    return score

def crawl_channel(keyword, pages=1, max_links=0):
    limit = 100 if pages <= 0 else pages * 20
    has_link, has_page = max_links > 0, pages > 0
    kw = keyword.replace("-","").replace(" ","")
    channels = search_channels(kw, limit=limit)
    if not channels: return []

    escaped = re.escape(keyword)
    flex = escaped.replace(r"\-", r"[-\s]?").replace(r"\ ", r"[-\s]?")
    pat = re.compile(rf"^{flex}(\s*综合)?$", re.IGNORECASE)
    exact = [c for c in channels if pat.match(c["name"])]
    cands = exact if exact else channels
    cands.sort(key=lambda c: quality_score(c["name"]), reverse=True)

    results, seen, checked = [], set(), 0
    for ch in cands:
        if has_link and len(results) >= max_links: break
        if has_page and checked >= limit: break
        checked += 1
        print(f"    [{checked}] {ch['name']}...", end=" ", flush=True)
        delay_fast()
        h = get_hash(ch["url"])
        if not h: print("✗", flush=True); continue
        delay_slow()
        u = get_stream(h)
        if not u: print("✗", flush=True); continue
        if u not in seen:
            seen.add(u)
            results.append({"name": normalize_name(keyword), "url": u})
            print("✓", flush=True)
        else: print("=", flush=True)
    return results

# ─── 缓存管理 (整合 import asy.txt 逻辑) ──────────────────────
CACHE_EXPIRE_SECONDS = CACHE_EXPIRE_HOURS * 3600

def load_cache():
    if not ENABLE_CACHE or not os.path.exists(CACHE_FILE): return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        now = time.time()
        valid_cache = {url: data for url, data in cache.items() 
                       if isinstance(data, dict) and (now - data.get("timestamp", 0)) < CACHE_EXPIRE_SECONDS}
        print(f"📂 缓存加载完成，有效条目数: {len(valid_cache)}", flush=True)
        return valid_cache
    except Exception: return {}

def save_cache(cache):
    if not ENABLE_CACHE: return
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存缓存失败: {e}", flush=True)

# ─── ffmpeg 异步测速核心 (整合最优逻辑) ───────────────────────
async def test_stream_async(url, timeout=10, duration=6):
    """异步执行 ffmpeg，使用 -c copy 测真实码率"""
    extra = []
    if url.startswith("rtsp://"): extra = ["-rtsp_transport", "tcp"]
    
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info", "-stats_period", "0.5",
        "-rw_timeout", str(timeout * 1000000),
        "-analyzeduration", "3000000", "-probesize", "3000000",
        *extra, "-i", url, "-t", str(duration),
        "-c", "copy", "-f", "null", "-"
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout + duration + 8)
        except asyncio.TimeoutError:
            proc.kill()
            return False, 0, "timeout"
            
        stderr = stderr_bytes.decode("utf-8", errors="ignore")

        # 致命错误检测 (只看前20行)
        head_lines = "\n".join(stderr.split("\n")[:20])
        for pat in [r"Connection refused", r"Connection timed out", r"Server returned 403", 
                    r"Server returned 404", r"Server returned 5\d\d", r"Protocol not found"]:
            if re.search(pat, head_lines, re.IGNORECASE):
                return False, 0, pat

        # 解析码率
        bitrate = 0
        bitrate_matches = re.findall(r"bitrate=\s*([\d.]+)\s*kbits/s", stderr)
        if bitrate_matches:
            bitrate = float(bitrate_matches[-1])
            
        if bitrate == 0:
            size_match = re.search(r"L?size=\s*([\d]+)\s*kB", stderr)
            time_match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", stderr)
            if size_match and time_match:
                size_kb = int(size_match.group(1))
                h, m, s = time_match.groups()
                media_sec = int(h) * 3600 + int(m) * 60 + float(s)
                if media_sec > 0: bitrate = (size_kb * 8) / media_sec

        frame_match = re.search(r"frame=\s*(\d+)", stderr)
        frames = int(frame_match.group(1)) if frame_match else 0

        if 0 < bitrate < 50:
            return False, bitrate, f"bitrate too low ({bitrate:.0f}kbps)"

        if (proc.returncode == 0 or frames > 10) and (bitrate > 0 or frames > 10):
            return True, bitrate, f"{bitrate:.0f}kbps"
        else:
            return False, 0, f"exit={proc.returncode}"

    except Exception as e:
        return False, 0, str(e)

async def speed_filter_async(items, top_n=3):
    """并发测速 + 缓存机制 + 按码率排序"""
    if top_n <= 0: return items

    by_name = defaultdict(list)
    for item in items: by_name[item["name"]].append(item)

    cache = load_cache()
    new_cache_updates = {}
    
    print(f"\n🚀 ffmpeg 异步测速 (并发:{FFMPEG_CONCURRENCY}, 每频道保留码率前{top_n})...", flush=True)
    result = []
    total_tested = 0
    total_ok = 0

    sem = asyncio.Semaphore(FFMPEG_CONCURRENCY)

    async def test_one(item):
        nonlocal total_tested, total_ok
        url = item["url"]
        
        # 1. 检查缓存
        if url in cache:
            c_data = cache[url]
            if c_data.get("ok"):
                total_ok += 1
                return {**item, "_bitrate": c_data.get("bitrate", 100), "_from_cache": True}
            else:
                return None # 缓存中是失败的，直接跳过

        # 2. 缓存未命中，加锁测速
        async with sem:
            total_tested += 1
            # 非致命错误重试 1 次
            ok, bitrate, reason = await test_stream_async(url, FFMPEG_TIMEOUT, FFMPEG_DURATION)
            if not ok and "timeout" not in reason and "403" not in reason and "404" not in reason:
                await asyncio.sleep(1)
                ok, bitrate, reason = await test_stream_async(url, FFMPEG_TIMEOUT, FFMPEG_DURATION)

            # 3. 更新缓存字典
            new_cache_updates[url] = {
                "ok": ok, "bitrate": bitrate, "timestamp": time.time()
            }

            if ok:
                total_ok += 1
                return {**item, "_bitrate": bitrate, "_from_cache": False}
            return None

    for name, ch_items in by_name.items():
        print(f"  频道组: {name}", flush=True)
        
        # 并发创建任务
        tasks = [asyncio.create_task(test_one(it)) for it in ch_items]
        tested = []
        
        for coro in asyncio.as_completed(tasks):
            res = await coro
            if res:
                src = "(缓存)" if res.pop("_from_cache") else ""
                print(f"    ✓ {res['url'][:40]}... {res['_bitrate']:.0f}kbps {src}", flush=True)
                tested.append(res)
            # 如果找到的稳定源已经足够，可以提前取消剩余任务 (优化)
            if len(tested) >= top_n * 2: # 多测几个用来排序，但不需要全测
                for t in tasks:
                    if not t.done(): t.cancel()
                break

        # 按码率降序排列，取前 top_n
        tested.sort(key=lambda x: x["_bitrate"], reverse=True)
        stable = tested[:top_n]

        if stable:
            print(f"    → 保留 {len(stable)} 条 (码率: {stable[0]['_bitrate']:.0f} ~ {stable[-1]['_bitrate']:.0f} kbps)", flush=True)
            for it in stable: it.pop("_bitrate", None)
            result.extend(stable)

    # 保存新缓存
    if ENABLE_CACHE and new_cache_updates:
        cache.update(new_cache_updates)
        save_cache(cache)

    print(f"\n测速完成: {total_ok} 个可用源 (含缓存复用)", flush=True)
    return result

# ─── 输出 ──────────────────────────────────────────────────────
def save_output(results, outdir):
    if os.path.exists(outdir): shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)
    txt_path = os.path.join(outdir, "output.txt")
    m3u_path = os.path.join(outdir, "output.m3u")
    with open(txt_path, "w", encoding="utf-8") as f:
        for g in results:
            f.write(f"{g['group']},#genre#\n")
            for ch in g["channels"]: f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for g in results:
            for ch in g["channels"]:
                f.write(f'#EXTINF:-1 group-title="{g["group"]}",{ch["name"]}\n{ch["url"]}\n')
    return txt_path, m3u_path

# ─── 主流程 ────────────────────────────────────────────────────
async def main():
    if not os.path.exists(CHANNEL_FILE):
        print(f"文件不存在: {CHANNEL_FILE}", flush=True)
        sys.exit(1)

    groups = parse_channel_file(CHANNEL_FILE)
    total = sum(len(g["channels"]) for g in groups)
    print(f"频道: {total} | 配置: {PAGES if PAGES>0 else '不限'}页 / {MAX_LINKS if MAX_LINKS>0 else '不限'}条 / 保留前{TOP_N} / 并发{FFMPEG_CONCURRENCY}", flush=True)

    # ── 爬取 ──
    results = []
    n = 0
    for g in groups:
        gr = {"group": g["group"], "channels": []}
        for kw in g["channels"]:
            n += 1
            print(f"[{n}/{total}] {kw}", end=" ", flush=True)
            try:
                items = crawl_channel(kw, pages=PAGES, max_links=MAX_LINKS)
            except Exception as e:
                print(f"❌ {e}", flush=True)
                items = []
            if items:
                print(f"✅ {len(items)}", flush=True)
                gr["channels"].extend(items)
            else:
                print("⚠️", flush=True)
        if gr["channels"]: results.append(gr)

    crawled = sum(len(g["channels"]) for g in results)
    print(f"\n爬取: {len(results)}组 {crawled}条", flush=True)
    if crawled == 0: return

    # ── 异步测速 ──
    if TOP_N > 0:
        all_ch = [ch for g in results for ch in g["channels"]]
        stable = await speed_filter_async(all_ch, top_n=TOP_N)
        stable_urls = {ch["url"] for ch in stable}
        results = [{"group": g["group"], "channels": [ch for ch in g["channels"] if ch["url"] in stable_urls]} 
                   for g in results if any(ch["url"] in stable_urls for ch in g["channels"])]
        print(f"测速后保留: {sum(len(g['channels']) for g in results)} 条", flush=True)

    # ── 输出 ──
    txt, m3u = save_output(results, OUTPUT_DIR)
    print(f"\n已保存:\n  {txt}\n  {m3u}", flush=True)

if __name__ == "__main__":
    # 兼容 Windows 和 Linux 的 asyncio 事件循环
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
