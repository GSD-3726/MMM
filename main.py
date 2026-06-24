import json
import os
import re
import sys
import time
import random
import shutil
import requests

# ============================================================
#  ★ 配置区
# ============================================================

# 频道列表文件
CHANNEL_FILE = "demo.txt"

# 每频道取几页搜索结果（每页约20条），0 = 不限制
PAGES = 1

# 每频道最多爬几条，0 = 不限制
MAX_LINKS = 1

# 输出目录
OUTPUT_DIR = "output"

# 开启调试模式：失败时打印响应内容（定位问题用）
DEBUG = True

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

DELAY_FAST = (0.5, 1.2)
DELAY_SLOW = (1.0, 2.5)

# 全局会话：复用Cookie，提升通过率
session = requests.Session()


def get_ua():
    return random.choice(UA_LIST)


def get_base_headers():
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


# ─── 频道文件解析 ─────────────────────────────────────────────────

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

    for suffix in ['咪咕', '港澳版', '港澳', '高码', '高码率']:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break

    return name.strip()


# ─── 核心爬取逻辑 ──────────────────────────────────────────────────────

def search_channels(keyword, limit=20):
    """搜索频道列表"""
    headers = get_base_headers()
    headers["Referer"] = f"{BASE_URL}/"
    try:
        r = session.get(
            f"{BASE_URL}/api/search",
            params={"q": keyword, "limit": limit},
            headers=headers,
            timeout=15
        )
        if r.status_code != 200:
            if DEBUG:
                print(f"\n[搜索接口异常] 状态码:{r.status_code} 前300字符:{r.text[:300]}")
            return []
        data = r.json()
    except Exception as e:
        if DEBUG:
            print(f"\n[搜索接口报错] {str(e)}")
        return []

    seen, out = set(), []
    # 兼容不同返回结构：优先取itemListElement，其次尝试常见的data/list
    items = data.get("itemListElement", [])
    if not items and isinstance(data.get("data"), list):
        items = data["data"]
    if not items and isinstance(data.get("list"), list):
        items = data["list"]

    for it in items:
        n, u = it.get("name",""), it.get("url","")
        if n and u and n not in seen:
            # 补全相对路径
            if u.startswith("/"):
                u = BASE_URL + u
            seen.add(n)
            out.append({"name": n, "url": u})
    return out


def get_hash(url):
    """从详情页提取频道Hash"""
    headers = get_base_headers()
    headers["Referer"] = f"{BASE_URL}/"
    try:
        r = session.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            if DEBUG:
                print(f"\n[详情页异常] 状态码:{r.status_code}")
            return None
        # 增强正则：兼容不同变量名、引号、空格
        patterns = [
            r"CURRENT_CHANNEL_HASH\s*=\s*['\"]([^'\"]+)['\"]",
            r"channelHash\s*=\s*['\"]([^'\"]+)['\"]",
            r"playHash\s*=\s*['\"]([^'\"]+)['\"]",
            r'hash["\']?\s*:\s*["\']([^"\']+)["\']'
        ]
        for pat in patterns:
            m = re.search(pat, r.text)
            if m:
                return m.group(1)
        if DEBUG:
            print(f"\n[Hash提取失败] 页面前500字符:{r.text[:500]}")
    except Exception as e:
        if DEBUG:
            print(f"\n[详情页报错] {str(e)}")
    return None


def get_stream(hash_val):
    """通过Hash获取真实播放链接"""
    headers = get_base_headers()
    headers["Referer"] = f"{BASE_URL}/"
    headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    try:
        r = session.get(
            f"{BASE_URL}/api/play/link",
            params={"hash": hash_val},
            headers=headers,
            timeout=15
        )
        if r.status_code != 200:
            if DEBUG:
                print(f"\n[播放接口异常] 状态码:{r.status_code} 内容:{r.text[:300]}")
            return None
        d = r.json()

        # 兼容不同返回结构
        success = d.get("success", False) or d.get("code") == 0
        play_link = d.get("play_link") or d.get("url") or d.get("data", {}).get("url")
        if not success or not play_link:
            if DEBUG:
                print(f"\n[播放接口返回失败] {d}")
            return None

        # 跟随重定向获取真实地址
        r = session.get(
            play_link,
            headers={"User-Agent": headers["User-Agent"]},
            allow_redirects=True,
            timeout=15
        )
        # 判断是否为有效m3u8
        ct = r.headers.get("content-type","")
        if "mpegurl" in ct or r.text.strip().startswith("#EXTM3U"):
            return r.url
        # 如果是重定向后的直链也返回
        if r.url != play_link and r.status_code == 200:
            return r.url
    except Exception as e:
        if DEBUG:
            print(f"\n[播放接口报错] {str(e)}")
    return None


# ─── 单频道爬取 ────────────────────────────────────────────────

QUALITY_ORDER = {
    '4k': 10, '2160p': 10,
    '1080p': 9, '1080': 9, '高清': 9, 'hd': 9, 'fhd': 9,
    '720p': 8, '720': 8,
    '高码': 7, '高码率': 7,
    '480p': 5, '480': 5, '标清': 5, 'sd': 5,
}


def quality_score(name):
    lower = name.lower()
    score = 0
    for kw, s in QUALITY_ORDER.items():
        if kw in lower:
            score = max(score, s)
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


# ─── 输出保存 ──────────────────────────────────────────────────────

def save_output(results, outdir):
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
    print(f"频道总数: {total} | 配置: {PAGES if PAGES>0 else '不限'}页 / {MAX_LINKS if MAX_LINKS>0 else '不限'}条 / 已关闭测速", flush=True)

    # 初始化会话：先访问首页拿Cookie
    print("初始化会话...", flush=True)
    try:
        session.get(BASE_URL, headers=get_base_headers(), timeout=15)
    except Exception as e:
        print(f"首页访问失败: {e}", flush=True)

    results = []
    n = 0
    for g in groups:
        gr = {"group": g["group"], "channels": []}
        for kw in g["channels"]:
            n += 1
            print(f"\n[{n}/{total}] {kw}", end=" ", flush=True)
            try:
                items = crawl_channel(kw, pages=PAGES, max_links=MAX_LINKS)
            except Exception as e:
                print(f"❌ {e}", flush=True)
                items = []
            if items:
                print(f"✅ 成功获取 {len(items)} 条", flush=True)
                gr["channels"].extend(items)
            else:
                print("⚠️ 无结果", flush=True)
        if gr["channels"]:
            results.append(gr)

    crawled = sum(len(g["channels"]) for g in results)
    print(f"\n爬取完成: {len(results)}组 共 {crawled} 条链接", flush=True)

    if crawled == 0:
        print("未获取到任何有效链接，请开启DEBUG模式查看具体报错原因", flush=True)
        return

    txt, m3u = save_output(results, OUTPUT_DIR)
    print(f"\n结果已保存:", flush=True)
    print(f"  TXT格式: {txt}", flush=True)
    print(f"  M3U格式: {m3u}", flush=True)


if __name__ == "__main__":
    main()
