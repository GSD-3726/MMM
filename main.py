import json
import os
import re
import sys
import time
import random
import subprocess
import shutil
import requests

# ============================================================
#  ★ 配置区
# ============================================================

# 频道列表文件
CHANNEL_FILE = "demo.txt"

# 每频道取几页搜索结果（每页约20条），0 = 不限制
PAGES = 2

# 每频道最多爬几条，0 = 不限制
MAX_LINKS = 20

# 测速后每频道保留前 N 个，0 = 不测速（全部保留）
TOP_N = 0

# ffmpeg 测试超时（秒）
FFMPEG_TIMEOUT = 10

# ffmpeg 读取秒数
FFMPEG_DURATION = 5

# 输出目录
OUTPUT_DIR = "output"

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


def get_ua():
    return random.choice(UA_LIST)


def get_headers():
    return {
        "User-Agent": get_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def delay_fast():
    time.sleep(random.uniform(*DELAY_FAST))


def delay_slow():
    time.sleep(random.uniform(*DELAY_SLOW))


# ─── 频道文件 ─────────────────────────────────────────────────

def parse_channel_file(filepath):
    groups = []
    current = None
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
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
    if not name:
        return name
    name = name.strip()
    cn_num = {
        '一':'1','二':'2','三':'3','四':'4','五':'5',
        '六':'6','七':'7','八':'8','九':'9','十':'10',
        '十一':'11','十二':'12','十三':'13','十四':'14',
        '十五':'15','十六':'16','十七':'17',
    }
    def cn_to_num(m):
        num = m.group(1)
        return f'CCTV-{num}' if num.isdigit() else f'CCTV-{cn_num.get(num, num)}'
    name = re.sub(r'^央视(\d|[一二三四五六七八九十]+)[台频道]*$', cn_to_num, name)
    name = re.sub(r'^CCTV(\d)', r'CCTV-\1', name, flags=re.IGNORECASE)
    name = re.sub(r'^(CCTV-\d+)[频道]+$', r'\1', name)

    # 保留质量标记，去掉冗余后缀
    # 高清/HD/1080p 等保留，咪咕/港澳版 等去掉
    for suffix in ['咪咕', '港澳版', '港澳', '高码', '高码率']:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break

    return name.strip()


# ─── API ──────────────────────────────────────────────────────

def search_channels(keyword, limit=20):
    try:
        r = requests.get(f"{BASE_URL}/api/search",
            params={"q": keyword, "limit": limit},
            headers=get_headers(), timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
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
        if r.status_code != 200:
            return None
        m = re.search(r"CURRENT_CHANNEL_HASH\s*=\s*['\"]([^'\"]+)['\"]", r.text)
        return m.group(1) if m else None
    except Exception:
        return None


def get_stream(hash_val):
    ua = get_ua()
    try:
        r = requests.get(f"{BASE_URL}/api/play/link",
            params={"hash": hash_val},
            headers={"User-Agent": ua}, timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
        if not d.get("success") or not d.get("play_link"):
            return None
        r = requests.get(d["play_link"],
            headers={"User-Agent": ua},
            allow_redirects=False, timeout=15)
        if r.status_code in (301,302,303,307,308):
            loc = r.headers.get("Location","")
            if loc.startswith("http"):
                return loc
        ct = r.headers.get("content-type","")
        if "mpegurl" in ct or r.text.strip().startswith("#EXTM3U"):
            return r.url
    except Exception:
        pass
    return None


# ─── 单频道爬取 ────────────────────────────────────────────────

# 质量优先级（数字越大越优先）
QUALITY_ORDER = {
    '4k': 10, '2160p': 10,
    '1080p': 9, '1080': 9, '高清': 9, 'hd': 9, 'fhd': 9,
    '720p': 8, '720': 8,
    '高码': 7, '高码率': 7,
    '480p': 5, '480': 5, '标清': 5, 'sd': 5,
}


def quality_score(name):
    """根据频道名中的质量关键词打分，分数越高越优先。"""
    lower = name.lower()
    score = 0
    for kw, s in QUALITY_ORDER.items():
        if kw in lower:
            score = max(score, s)
    # 「综合」通常是主频道，加一点分
    if '综合' in name:
        score += 1
    return score


def crawl_channel(keyword, pages=1, max_links=0):
    limit = 100 if pages <= 0 else pages * 20
    has_link = max_links > 0
    has_page = pages > 0

    kw = keyword.replace("-","").replace(" ","")
    channels = search_channels(kw, limit=limit)
    if not channels:
        return []

    escaped = re.escape(keyword)
    flex = escaped.replace(r"\-", r"[-\s]?").replace(r"\ ", r"[-\s]?")
    pat = re.compile(rf"^{flex}(\s*综合)?$", re.IGNORECASE)
    exact = [c for c in channels if pat.match(c["name"])]
    cands = exact if exact else channels

    # 按质量排序（高分辨率优先）
    cands.sort(key=lambda c: quality_score(c["name"]), reverse=True)

    results, seen, checked = [], set(), 0
    for ch in cands:
        if has_link and len(results) >= max_links:
            break
        if has_page and checked >= limit:
            break
        checked += 1

        print(f"    [{checked}] {ch['name']}...", end=" ", flush=True)
        delay_fast()
        h = get_hash(ch["url"])
        if not h:
            print("✗", flush=True)
            continue
        delay_slow()
        u = get_stream(h)
        if not u:
            print("✗", flush=True)
            continue
        if u not in seen:
            seen.add(u)
            results.append({"name": normalize_name(keyword), "url": u})
            print("✓", flush=True)
        else:
            print("=", flush=True)
    return results


# ─── ffmpeg 测速 ───────────────────────────────────────────────

def test_stream(url, timeout=10, duration=5):
    """
    用 ffmpeg 测速，判断流是否稳定不卡。
    返回 (是否可用, 响应时间秒, 原因)

    判断逻辑:
      1. ffmpeg 能读取 duration 秒且 returncode=0 → 稳定
      2. 检查 stderr 中的卡顿指标:
         - "buffer underflow" / "stall" → 卡顿
         - "dropping frame" → 丢帧
         - "Error decoding" → 解码错误
      3. 计算实际下载字节数 vs 期望值（粗略判断是否够快）
    """
    # 根据协议调整参数
    extra = []
    if url.startswith("udp://") or url.startswith("rtp://"):
        extra = ["-fifo_size", "1000000", "-overrun_nonfatal", "1"]
    elif url.startswith("rtsp://"):
        extra = ["-rtsp_transport", "tcp"]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-rw_timeout", str(timeout * 1000000),
        "-analyzeduration", "2000000",
        "-probesize", "1000000",
        *extra,
        "-i", url,
        "-t", str(duration),
        "-f", "null", "-",
    ]

    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + duration + 5)
        elapsed = time.time() - t0
        stderr = r.stderr.decode("utf-8", errors="ignore")

        # 检查卡顿指标
        stall_words = ["buffer underflow", "stall", "dropping frame",
                       "Error decoding", "Invalid data", "Connection timed out",
                       "Server returned 4", "Server returned 5"]
        for w in stall_words:
            if w.lower() in stderr.lower():
                return False, 0, w

        if r.returncode == 0:
            return True, elapsed, "ok"
        else:
            return False, 0, f"exit={r.returncode}"

    except subprocess.TimeoutExpired:
        return False, 0, "timeout"
    except Exception as e:
        return False, 0, str(e)


def speed_filter(items, top_n=3):
    """
    测速，每频道保留稳定的前 top_n 个。
    按响应时间排序（越快越稳定）。
    """
    if top_n <= 0:
        return items

    # 按频道名分组
    by_name = {}
    for item in items:
        by_name.setdefault(item["name"], []).append(item)

    print(f"\nffmpeg 测速 (每频道前{top_n})...", flush=True)
    result = []
    total_tested = 0
    total_ok = 0

    for name, ch_items in by_name.items():
        stable = []
        for i, item in enumerate(ch_items):
            total_tested += 1
            print(f"  {name} [{i+1}/{len(ch_items)}] ", end="", flush=True)
            ok, t, reason = test_stream(
                item["url"],
                timeout=FFMPEG_TIMEOUT,
                duration=FFMPEG_DURATION)
            if ok:
                stable.append(item)
                total_ok += 1
                print(f"✓ {t:.1f}s", flush=True)
                if len(stable) >= top_n:
                    break
            else:
                print(f"✗ {reason}", flush=True)

        result.extend(stable)

    print(f"测速完成: {total_ok}/{total_tested} 通过", flush=True)
    return result


# ─── 输出 ──────────────────────────────────────────────────────

def save_output(results, outdir):
    # 清空输出目录
    if os.path.exists(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)

    txt_path = os.path.join(outdir, "output.txt")
    m3u_path = os.path.join(outdir, "output.m3u")

    with open(txt_path, "w", encoding="utf-8") as f:
        for g in results:
            f.write(f"{g['group']},#genre#\n")
            for ch in g["channels"]:
                f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")

    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for g in results:
            for ch in g["channels"]:
                f.write(f'#EXTINF:-1 group-title="{g["group"]}",{ch["name"]}\n')
                f.write(f'{ch["url"]}\n')

    return txt_path, m3u_path


# ─── 主流程 ────────────────────────────────────────────────────

def main():
    if not os.path.exists(CHANNEL_FILE):
        print(f"文件不存在: {CHANNEL_FILE}", flush=True)
        sys.exit(1)

    groups = parse_channel_file(CHANNEL_FILE)
    total = sum(len(g["channels"]) for g in groups)
    print(f"频道: {total} | 配置: {PAGES if PAGES>0 else '不限'}页/{MAX_LINKS if MAX_LINKS>0 else '不限'}条/{TOP_N if TOP_N>0 else '不测速'}测速", flush=True)

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
        if gr["channels"]:
            results.append(gr)

    crawled = sum(len(g["channels"]) for g in results)
    print(f"\n爬取: {len(results)}组 {crawled}条", flush=True)

    if crawled == 0:
        print("无结果", flush=True)
        return

    # ── 测速 ──
    if TOP_N > 0:
        all_ch = []
        for g in results:
            all_ch.extend(g["channels"])

        stable = speed_filter(all_ch, top_n=TOP_N)
        stable_urls = {ch["url"] for ch in stable}

        new_results = []
        for g in results:
            filtered = [ch for ch in g["channels"] if ch["url"] in stable_urls]
            if filtered:
                new_results.append({"group": g["group"], "channels": filtered})
        results = new_results

        final = sum(len(g["channels"]) for g in results)
        print(f"测速: 保留 {final} 条", flush=True)

    # ── 输出 ──
    txt, m3u = save_output(results, OUTPUT_DIR)
    print(f"\n已保存:", flush=True)
    print(f"  {txt}", flush=True)
    print(f"  {m3u}", flush=True)


if __name__ == "__main__":
    main()
