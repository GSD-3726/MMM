#!/usr/bin/env python3
"""
咪咕直播源抓取工具（Python 版）
基于 https://github.com/GSD-3726/migu_video 改写
两个数据源：
  1. 咪咕官方 API（央视+部分卫视）→ 需要签名+ddCalcu加密
  2. pro.fengcaizb.com（卫视+地方台）→ AES 解密流地址

输出：migu_iptv.txt（TVBox 格式）+ migu_iptv.m3u（播放器格式）
"""

import requests
import hashlib
import json
import time
import os
import sys
import gzip
import base64
from datetime import datetime
from io import BytesIO

try:
    from Crypto.Cipher import AES as CryptoAES
except ImportError:
    try:
        from Cryptodome.Cipher import AES as CryptoAES
    except ImportError:
        print("[!] 需要安装 pycryptodome: pip install pycryptodome")
        sys.exit(1)

# ============================================================
# 配置
# ============================================================
# 画质: 2=标清, 3=高清, 4=蓝光(需VIP), 9=4K(需VIP)
RATE_TYPE = 3
# 是否开启 HDR
ENABLE_HDR = True
# 是否开启 H265
ENABLE_H265 = True
# 请求超时（秒）
TIMEOUT = 8

# 输出文件
OUTPUT_TXT = "migu_iptv.txt"
OUTPUT_M3U = "migu_iptv.m3u"

# ============================================================
# 域名白名单（来自原项目 datas.js）
# ============================================================
DOMAIN_WHITELIST = [
    "hlsztemgsplive.miguvideo.com:8080",
    "hlsbkmgsplive.miguvideo.com",
    "tvpull.dxhmt.cn:9081",
    "play.kankanlive.com",
    "liveplay-srs.voc.com.cn",
    "ali-xwl.cztv.com",
    "l.cztvcloud.com",
    "masterpull.hljtv.com",
    "stream.hrbtv.net",
    "rmplive.hljtv.com",
    "gxlive.snrtv.com",
    "play-a2.quklive.com",
    "nklive.nbs.cn",
    "stream.thmz.com",
    "jwcdnqx.hebyun.com.cn",
    "hlsal-ldvt.qing.mgtv.com",
]

# ============================================================
# AES 解密（用于 pro.fengcaizb.com 的数据）
# ============================================================
KEY_ARRAY = bytes([121, 111, 117, 33, 106, 101, 64, 49, 57, 114, 114, 36, 50, 48, 121, 35])
IV_ARRAY = bytes([65, 114, 101, 121, 111, 117, 124, 62, 127, 110, 54, 38, 13, 97, 110, 63])


def aes_decrypt(base64_data):
    """AES-128-CBC 解密"""
    data = base64.b64decode(base64_data)
    cipher = CryptoAES.new(KEY_ARRAY, CryptoAES.MODE_CBC, IV_ARRAY)
    decrypted = cipher.decrypt(data)
    # 去除 PKCS7 padding
    pad_len = decrypted[-1]
    return decrypted[:-pad_len].decode("utf-8")


# ============================================================
# MD5 工具
# ============================================================
def md5_hex(s):
    """返回小写 hex MD5"""
    return hashlib.md5(s.encode("utf-8")).hexdigest().lower()


# ============================================================
# ddCalcu 加密（URL 签名）
# ============================================================

# 720p 模式的 ddCalcu
def get_ddcalcu_720p(pu_data, program_id):
    """旧版 720p ddCalcu 签名"""
    if not pu_data or program_id is None:
        return ""
    keys = "cdabyzwxkl"
    ddcalcu = []
    pu_len = len(pu_data)
    for i in range(pu_len // 2):
        ddcalcu.append(pu_data[pu_len - i - 1])
        ddcalcu.append(pu_data[i])
        if i == 1:
            ddcalcu.append("v")
        elif i == 2:
            # JS: keys[parseInt(getDateString(new Date())[2])]
            # getDateString 返回 'yyyymmdd'，[2] 是年份第3位
            year_str = str(datetime.now().year)
            idx = int(year_str[2]) if len(year_str) > 2 else 0
            ddcalcu.append(keys[idx % len(keys)])
        elif i == 3:
            pid_str = str(program_id)
            if len(pid_str) > 6:
                idx = int(pid_str[6]) if pid_str[6].isdigit() else 0
                ddcalcu.append(keys[idx % len(keys)])
        elif i == 4:
            ddcalcu.append("a")
    return "".join(ddcalcu)


def get_ddcalcu_url_720p(pu_data_url, program_id):
    """旧版 720p 加密链接"""
    if not pu_data_url or program_id is None:
        return ""
    pu_data = pu_data_url.split("&puData=")[1] if "&puData=" in pu_data_url else ""
    ddcalcu = get_ddcalcu_720p(pu_data, program_id)
    return f"{pu_data_url}&ddCalcu={ddcalcu}&sv=10004&ct=android"


# 标准模式的 ddCalcu
ANDROID_KEYS = "cdabyzwxkl"
ANDROID_WORDS_DEFAULT = ["v", "a", "0", "a"]


def get_ddcalcu(pu_data, program_id, client_type, rate_type, user_id=""):
    """标准 ddCalcu 签名"""
    if not pu_data or program_id is None:
        return ""
    keys = ANDROID_KEYS
    words = list(ANDROID_WORDS_DEFAULT)
    third_replace_index = 6  # android

    if user_id:
        uid = str(user_id)
        if len(uid) > 7:
            c = uid[7]
            if c.isdigit():
                idx = int(c)
                if idx < len(keys):
                    words[0] = keys[idx]
    if client_type == "android" and rate_type == 2:
        words[0] = "v"
    if user_id and 3 < len(str(user_id)) <= 8:
        words[0] = "e"

    pu_len = len(pu_data)
    ddcalcu = []
    for i in range(pu_len // 2):
        ddcalcu.append(pu_data[pu_len - i - 1])
        ddcalcu.append(pu_data[i])
        if i == 1:
            ddcalcu.append(words[0])
        elif i == 2:
            # JS: keys[parseInt(getDateString(new Date())[0])]
            # getDateString 返回 'yyyymmdd'，[0] 是年第1位
            year_str = str(datetime.now().year)
            idx = int(year_str[0]) if year_str else 0
            ddcalcu.append(keys[idx % len(keys)])
        elif i == 3:
            pid_str = str(program_id)
            if len(pid_str) > third_replace_index:
                c = pid_str[third_replace_index]
                if c.isdigit():
                    ddcalcu.append(keys[int(c) % len(keys)])
        elif i == 4:
            ddcalcu.append(words[1])
    return "".join(ddcalcu)


def get_ddcalcu_url(pu_data_url, program_id, client_type, rate_type, user_id=""):
    """标准加密链接"""
    if not pu_data_url or program_id is None:
        return ""
    pu_data = pu_data_url.split("&puData=")[1] if "&puData=" in pu_data_url else ""
    ddcalcu = get_ddcalcu(pu_data, program_id, client_type, rate_type, user_id)
    suffix = "&sv=10004&ct=android"
    return f"{pu_data_url}&ddCalcu={ddcalcu}{suffix}"


# ============================================================
# 咪咕 API
# ============================================================

def fetch_json(url, headers=None, timeout=TIMEOUT):
    """GET 请求返回 JSON"""
    try:
        resp = requests.get(url, headers=headers or {}, timeout=timeout, allow_redirects=False)
        return resp.json()
    except Exception as e:
        print(f"[!] 请求失败 {url}: {e}")
        return None


def fetch_redirect(url, headers=None, timeout=TIMEOUT):
    """GET 请求返回 302 Location"""
    try:
        resp = requests.get(url, headers=headers or {}, timeout=timeout, allow_redirects=False)
        return resp.headers.get("Location", "")
    except Exception as e:
        print(f"[!] 请求失败: {e}")
        return ""


def get_category_list():
    """获取咪咕频道分类列表"""
    url = "https://program-sc.miguvideo.com/live/v2/tv-data/1ff892f2b5ab4a79be6e25b69d2f5d05"
    data = fetch_json(url)
    if not data or "body" not in data:
        print("[!] 获取分类列表失败")
        return []
    live_list = data["body"].get("liveList", [])
    # 过滤"热门"
    live_list = [item for item in live_list if item.get("name") != "热门"]
    # 央视排第一
    live_list.sort(key=lambda x: 0 if x.get("name") == "央视" else 1)
    return live_list


def get_channels_by_category(voms_id):
    """获取分类下的频道列表"""
    url = f"https://program-sc.miguvideo.com/live/v2/tv-data/{voms_id}"
    data = fetch_json(url)
    if not data or "body" not in data:
        return []
    return data["body"].get("dataList", [])


def get_migu_play_url_720p(pid):
    """获取咪咕 720p 播放链接（无需登录）"""
    timestamp = str(int(time.time() * 1000))
    app_version = "2600034600"
    client_id = md5_hex(str(int(time.time() * 1000)))

    headers = {
        "AppVersion": app_version,
        "TerminalId": "android",
        "X-UP-CLIENT-CHANNEL-ID": f"{app_version}-99000-201600010010028",
        "ClientId": client_id,
    }
    # CCTV5 和 5+ 开启 flv 后不能回放
    if pid not in ("641886683", "641886773"):
        headers["appCode"] = "miguvideo_default_android"

    # 签名
    sign_str = timestamp + pid + app_version[:8]
    md5_val = md5_hex(sign_str)
    salt = str(int(time.time() * 1000) % 1000000).zfill(6) + "25"
    suffix = "2cac4f2c6c3346a5b34e085725ef7e33migu" + salt[:4]
    sign = md5_hex(md5_val + suffix)

    rate_type = RATE_TYPE
    hdr_str = "&4kvivid=true&2Kvivid=true&vivid=2" if ENABLE_HDR else ""
    h265_str = "&h265N=true" if ENABLE_H265 else ""

    base_url = "https://play.miguvideo.com/playurl/v1/play/playurl"
    params = (f"?sign={sign}&rateType={rate_type}&contId={pid}&timestamp={timestamp}"
              f"&salt={salt}&flvEnable=true&super4k=true{h265_str}{hdr_str}")

    resp = fetch_json(base_url + params, headers=headers)
    if not resp:
        return ""

    url_info = resp.get("body", {}).get("urlInfo") or {}
    play_url = url_info.get("url", "")

    if not play_url:
        # 尝试降级到标清
        params = (f"?sign={sign}&rateType=2&contId={pid}&timestamp={timestamp}"
                  f"&salt={salt}&flvEnable=true&super4k=true{h265_str}{hdr_str}")
        resp = fetch_json(base_url + params, headers=headers)
        if resp:
            url_info = resp.get("body", {}).get("urlInfo") or {}
            play_url = url_info.get("url", "")

    if not play_url:
        return ""

    # ddCalcu 加密
    cont_id = resp.get("body", {}).get("content", {}).get("contId", pid)
    result_url = get_ddcalcu_url_720p(play_url, cont_id)

    # 跟随 302 获取实际地址
    location = fetch_redirect(result_url)
    if location and not location.startswith("http://bofang"):
        return location

    return result_url


def get_migu_play_url(pid, user_id="", user_token=""):
    """获取咪咕播放链接（标准模式，需要登录才能高清+）"""
    timestamp = str(int(time.time() * 1000))
    app_version = "26000370"

    headers = {
        "AppVersion": "2600037000",
        "TerminalId": "android",
        "X-UP-CLIENT-CHANNEL-ID": "2600037000-99000-200300220100002",
    }
    if pid not in ("641886683", "641886773"):
        headers["appCode"] = "miguvideo_default_android"

    if RATE_TYPE > 2 and user_id and user_token:
        headers["UserId"] = user_id
        headers["UserToken"] = user_token

    sign_str = timestamp + pid + app_version
    md5_val = md5_hex(sign_str)
    suffix = "3ce941cc3cbc40528bfd1c64f9fdf6c0migu0123"
    sign = md5_hex(md5_val + suffix)
    salt = "1230024"

    hdr_str = "&4kvivid=true&2Kvivid=true&vivid=2" if ENABLE_HDR else ""
    h265_str = "&h265N=true" if ENABLE_H265 else ""
    rate_type = RATE_TYPE

    base_url = "https://play.miguvideo.com/playurl/v1/play/playurl"
    params = (f"?sign={sign}&rateType={rate_type}&contId={pid}&timestamp={timestamp}"
              f"&salt={salt}&flvEnable=true&super4k=true"
              f"{'&ott=true' if rate_type == 9 else ''}{h265_str}{hdr_str}")

    resp = fetch_json(base_url + params, headers=headers)
    if not resp:
        return ""

    rid = resp.get("rid", "")
    url_info = resp.get("body", {}).get("urlInfo") or {}
    play_url = url_info.get("url", "")

    # 会员降级处理
    if rid == "TIPS_NEED_MEMBER" or not play_url:
        alt_rate = min(int(url_info.get("rateType", 4)), 4) if url_info.get("rateType") else 3
        params = (f"?sign={sign}&rateType={alt_rate}&contId={pid}&timestamp={timestamp}"
                  f"&salt={salt}&flvEnable=true&super4k=true{h265_str}{hdr_str}")
        resp = fetch_json(base_url + params, headers=headers)
        if resp:
            url_info = resp.get("body", {}).get("urlInfo") or {}
            play_url = url_info.get("url", "")

        if not play_url:
            params = (f"?sign={sign}&rateType=3&contId={pid}&timestamp={timestamp}"
                      f"&salt={salt}&flvEnable=true&super4k=true{h265_str}{hdr_str}")
            resp = fetch_json(base_url + params, headers=headers)
            if resp:
                url_info = resp.get("body", {}).get("urlInfo") or {}
                play_url = url_info.get("url", "")

    if not play_url:
        return ""

    cont_id = resp.get("body", {}).get("content", {}).get("contId", pid)
    result_url = get_ddcalcu_url(play_url, cont_id, "android", rate_type, user_id)

    location = fetch_redirect(result_url)
    if location and not location.startswith("http://bofang"):
        return location

    return result_url


# ============================================================
# pro.fengcaizb.com 数据源（卫视+地方台）
# ============================================================

def fetch_fengcaizb():
    """从 pro.fengcaizb.com 获取频道列表"""
    url = "http://pro.fengcaizb.com/channels/pro.gz"
    headers = {"Referer": "http://pro.fengcaizb.com"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"[!] fengcaizb 请求失败: {resp.status_code}")
            return []
        # 解压 gzip
        compressed = BytesIO(resp.content)
        with gzip.GzipFile(fileobj=compressed) as f:
            json_str = f.read().decode("utf-8")
        data = json.loads(json_str)
        return data.get("data", [])
    except Exception as e:
        print(f"[!] fengcaizb 解析失败: {e}")
        return []


def process_fengcaizb_channels(raw_data):
    """处理 fengcaizb 数据，解密 URL 并过滤"""
    channels = []
    for ch in raw_data:
        # 跳过广告
        if ch.get("ct"):
            continue
        title = ch.get("title", "").replace("-", "")
        province = ch.get("province", "其他")
        for encrypted_url in ch.get("urls", []):
            try:
                decrypt_url = aes_decrypt(encrypted_url)
            except Exception:
                continue
            if decrypt_url.startswith("sys_http"):
                decrypt_url = decrypt_url.replace("sys_", "")
            if not decrypt_url.startswith("http"):
                continue
            # 处理特殊字符
            if "$" in decrypt_url:
                decrypt_url = decrypt_url.split("$")[0]
            channels.append({
                "name": title,
                "group": province,
                "url": decrypt_url,
                "logo": "",
            })
            break  # 每个频道只取第一个可用 URL
    return channels


# ============================================================
# 频道归一化
# ============================================================

# 标准央视频道名（有序）
CCTV_STANDARD = [
    "CCTV-1", "CCTV-2", "CCTV-3", "CCTV-4", "CCTV-5", "CCTV-5+",
    "CCTV-6", "CCTV-7", "CCTV-8", "CCTV-9", "CCTV-10", "CCTV-11",
    "CCTV-12", "CCTV-13", "CCTV-14", "CCTV-15", "CCTV-16", "CCTV-17",
]

# 标准卫视频道名
SATELLITE_STANDARD = [
    "湖南卫视", "浙江卫视", "江苏卫视", "东方卫视", "北京卫视",
    "天津卫视", "重庆卫视", "河北卫视", "山西卫视", "辽宁卫视",
    "吉林卫视", "黑龙江卫视", "安徽卫视", "东南卫视", "江西卫视",
    "山东卫视", "河南卫视", "湖北卫视", "广东卫视", "深圳卫视",
    "广西卫视", "四川卫视", "贵州卫视", "云南卫视", "陕西卫视",
    "甘肃卫视", "青海卫视", "海南卫视", "西藏卫视", "新疆卫视",
    "内蒙古卫视", "宁夏卫视", "兵团卫视", "厦门卫视",
]


def normalize_cctv(name):
    """
    将各种 CCTV 变体归一到标准名。
    CCTV1综合, CCTV-1, CCTV1高清, CCTV1超清, CCTV-1 综合 → CCTV-1
    返回: (标准名, 原始名) 或 None
    """
    import re
    upper = name.upper().replace(' ', '').replace('\u3000', '')
    # 去掉常见后缀（长的放前面，避免 '体育' 先于 '体育赛事' 被替换）
    for suffix in ['中文国际', '体育赛事', '国防军事', '社会与法', '农业农村', '奥林匹克',
                   '外语纪录', '阿拉伯语', '西班牙语', '综合', '高清', '超清',
                   '标清', '蓝光', '付费', '频道', '财经', '综艺', '体育', '电影',
                   '电视剧', '纪录', '科教', '戏曲', '新闻', '少儿', '音乐',
                   '欧洲', '美洲', '法语', '俄语', '4K']:
        upper = upper.replace(suffix, '')

    # 匹配 CCTV 数字
    m = re.match(r'^CCTV[\-_]?(\d{1,2})([\+＋Pp]?)$', upper)
    if m:
        num = int(m.group(1))
        plus = m.group(2)
        if 1 <= num <= 17:
            # CCTV-5+ 特殊处理
            if num == 5 and (plus or '+' in name or '＋' in name or 'PLUS' in name.upper()):
                return "CCTV-5+"
            return f"CCTV-{num}"

    # 特殊: CGTN
    if 'CGTN' in upper:
        return 'CGTN'

    return None


def is_satellite_main(name):
    """
    判断是否是卫视主频道（含超清/高清变体）。
    匹配: 湖南卫视, 湖南卫视超清, 湖南卫视高清 等。
    排除: 湖南新闻, 湖南都市, 湖南经视 等。
    返回: 标准名 "XX卫视" 或 None
    """
    name = name.strip()
    # 去掉画质后缀
    clean = name
    for suffix in ['超清', '高清', '标清', '蓝光', '4K', 'HD', 'hd', 'SD', 'sd']:
        clean = clean.replace(suffix, '')
    clean = clean.strip()

    # 必须以 "卫视" 结尾
    if not clean.endswith('卫视'):
        return None

    # 排除 "XX新闻卫视" 这种非主频道格式（但保留 "XX卫视"）
    # 匹配标准列表
    for sat in SATELLITE_STANDARD:
        if clean == sat:
            return sat

    # 不在标准列表中，但以 "卫视" 结尾的也保留
    # 如：三沙卫视、大湾区卫视、中国农林卫视
    return clean


def normalize_and_filter(all_raw):
    """
    对所有原始频道做归一化：
    1. CCTV 合并变体 → CCTV-1 ~ CCTV-17
    2. 卫视只保留主频道
    3. 其他频道保留
    返回: {标准名: {name, group, url, logo}}
    """
    # {标准名: [候选列表]}
    cctv_map = {}  # CCTV-1 → [{...}, ...]
    satellite_map = {}  # 湖南卫视 → [{...}, ...]
    others = {}  # 其他

    for ch in all_raw:
        raw_name = ch["name"]

        # 尝试匹配央视
        std_cctv = normalize_cctv(raw_name)
        if std_cctv:
            if std_cctv not in cctv_map:
                cctv_map[std_cctv] = []
            cctv_map[std_cctv].append(ch)
            continue

        # 尝试匹配卫视主频道（含超清/高清变体）
        sat_name = is_satellite_main(raw_name)
        if sat_name:
            if sat_name not in satellite_map:
                satellite_map[sat_name] = []
            satellite_map[sat_name].append(ch)
            continue

        # 其他频道（地方台、咪咕专属等）
        if raw_name not in others:
            others[raw_name] = ch

    # 合并结果：每个标准名保留所有可用源（去重）
    result = {}

    def dedup_urls(urls):
        """URL 去重，保持顺序"""
        seen = set()
        out = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    # 央视
    for std_name in CCTV_STANDARD:
        if std_name in cctv_map:
            urls = dedup_urls([ch["url"] for ch in cctv_map[std_name] if ch.get("url")])
            logo = cctv_map[std_name][0].get("logo", "")
            result[std_name] = {
                "name": std_name,
                "group": "央视",
                "urls": urls,
                "logo": logo,
            }

    # 卫视
    for std_name in SATELLITE_STANDARD:
        if std_name in satellite_map:
            urls = dedup_urls([ch["url"] for ch in satellite_map[std_name] if ch.get("url")])
            logo = satellite_map[std_name][0].get("logo", "")
            result[std_name] = {
                "name": std_name,
                "group": "卫视",
                "urls": urls,
                "logo": logo,
            }
    # 不在标准列表但以"卫视"结尾的
    for std_name, chs in satellite_map.items():
        if std_name not in result:
            urls = dedup_urls([ch["url"] for ch in chs if ch.get("url")])
            result[std_name] = {
                "name": std_name,
                "group": "卫视",
                "urls": urls,
                "logo": chs[0].get("logo", ""),
            }

    # 其他
    for name, ch in others.items():
        result[name] = {
            "name": name,
            "group": ch.get("group", "其他"),
            "urls": [ch["url"]] if ch.get("url") else [],
            "logo": ch.get("logo", ""),
        }

    return result


# ============================================================
# 输出生成
# ============================================================

def generate_output(migu_channels, fengcai_channels):
    """生成 TXT 和 M3U 文件"""
    all_raw = migu_channels + fengcai_channels
    print(f"    原始频道数: {len(all_raw)}")

    # 归一化 + 过滤
    all_channels = normalize_and_filter(all_raw)

    # 按分类整理
    categories = {}
    for name, ch in all_channels.items():
        group = ch.get("group", "其他")
        if group not in categories:
            categories[group] = []
        categories[group].append(ch)

    # 生成 TXT
    txt_lines = []
    for group in ["央视", "卫视", "咪咕", "其他"]:
        if group in categories:
            txt_lines.append(f"{group},#genre#")
            for ch in categories[group]:
                for url in ch.get("urls", []):
                    txt_lines.append(f"{ch['name']},{url}")
            txt_lines.append("")

    for group, chs in categories.items():
        if group not in ["央视", "卫视", "咪咕", "其他"]:
            txt_lines.append(f"{group},#genre#")
            for ch in chs:
                for url in ch.get("urls", []):
                    txt_lines.append(f"{ch['name']},{url}")
            txt_lines.append("")

    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))
    print(f"[✓] TXT: {OUTPUT_TXT}")

    # 生成 M3U
    m3u_lines = ['#EXTM3U']
    for group in ["央视", "卫视", "咪咕", "其他"]:
        if group in categories:
            for ch in categories[group]:
                logo = ch.get("logo", "")
                logo_str = f' tvg-logo="{logo}"' if logo else ""
                for url in ch.get("urls", []):
                    m3u_lines.append(f'#EXTINF:-1 tvg-id="{ch["name"]}" tvg-name="{ch["name"]}"{logo_str} group-title="{group}",{ch["name"]}')
                    m3u_lines.append(url)

    for group, chs in categories.items():
        if group not in ["央视", "卫视", "咪咕", "其他"]:
            for ch in chs:
                logo = ch.get("logo", "")
                logo_str = f' tvg-logo="{logo}"' if logo else ""
                for url in ch.get("urls", []):
                    m3u_lines.append(f'#EXTINF:-1 tvg-id="{ch["name"]}" tvg-name="{ch["name"]}"{logo_str} group-title="{group}",{ch["name"]}')
                    m3u_lines.append(url)

    with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u_lines))
    print(f"[✓] M3U: {OUTPUT_M3U}")

    return all_channels


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 55)
    print("  咪咕直播源抓取工具 (Python)")
    print("  数据源: 咪咕API + fengcaizb.com")
    print("=" * 55)
    print()

    # ---- 数据源 1: 咪咕 API ----
    print("[1/2] 获取咪咕频道列表...")
    categories = get_category_list()
    if not categories:
        print("[!] 咪咕分类获取失败，跳过")
        migu_channels = []
    else:
        print(f"    找到 {len(categories)} 个分类")
        migu_channels = []
        for cat in categories:
            cat_name = cat.get("name", "其他")
            voms_id = cat.get("vomsID", "")
            if not voms_id:
                continue
            print(f"    [{cat_name}] 获取频道...")
            data_list = get_channels_by_category(voms_id)
            for ch in data_list:
                name = ch.get("name", "")
                pid = ch.get("pID", "")
                logo = ch.get("pics", {}).get("highResolutionH", "")
                if not name or not pid:
                    continue
                print(f"      {name} (pid={pid})...", end=" ", flush=True)
                play_url = get_migu_play_url_720p(pid)
                if play_url:
                    migu_channels.append({
                        "name": name,
                        "group": cat_name,
                        "url": play_url,
                        "logo": logo,
                    })
                    print("✓")
                else:
                    print("✗")
                time.sleep(0.3)  # 控制请求频率

    print(f"    咪咕源: {len(migu_channels)} 个频道")
    print()

    # ---- 数据源 2: fengcaizb ----
    print("[2/2] 获取 fengcaizb 频道列表...")
    raw_fengcai = fetch_fengcaizb()
    if not raw_fengcai:
        print("[!] fengcaizb 数据获取失败，跳过")
        fengcai_channels = []
    else:
        fengcai_channels = process_fengcaizb_channels(raw_fengcai)
        print(f"    fengcaizb 源: {len(fengcai_channels)} 个频道")

    print()

    # ---- 合并输出 ----
    print("[*] 生成输出文件...")
    all_channels = generate_output(migu_channels, fengcai_channels)

    # 统计
    cctv_count = sum(1 for n in all_channels if n.startswith("CCTV-"))
    sat_count = sum(1 for n in all_channels if "卫视" in n)
    other_count = len(all_channels) - cctv_count - sat_count
    total = len(all_channels)

    print()
    print(f"[*] 统计 (归一化后):")
    print(f"    总频道数: {total}")
    print(f"    央视频道: {cctv_count}  (CCTV-1 ~ CCTV-17)")
    print(f"    卫视频道: {sat_count}  (仅主频道)")
    print(f"    其他频道: {other_count}  (地方台/咪咕专属)")
    print()
    print(f"[*] 输出文件: {OUTPUT_TXT}, {OUTPUT_M3U}")
    print("[*] 完成！")


if __name__ == "__main__":
    main()
