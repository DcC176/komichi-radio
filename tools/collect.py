# -*- coding: utf-8 -*-
"""二十四时小路电台 · 节目单采集脚本

从 B 站系列接口拉取全部直播回放，汇总时长与弹幕量，输出前端可直接消费的节目单。

用法：
    python tools/collect.py            # 增量（有缓存则跳过已采集项）
    python tools/collect.py --refresh  # 强制重新采集

产物：
    data/programs.json   节目单（可读，供人工核对）
    data/programs.js     同上，包装为 window.PROGRAMS，便于 file:// 直接打开

注：弹幕量取自视频级 stat.danmaku（权威且准确）。分P 无独立弹幕计数接口，
    按时长占比分摊；comment.bilibili.com 的 XML 接口实测硬截断在 9600 条，
    不可用于统计，故不采用。
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------- 配置

MID = "1512246445"
SERIES_ID = "5157110"
UP_NAME = "四时小路Komichi"
UP_SPACE = "https://space.bilibili.com/%s" % MID
SERIES_URL = "%s/lists/%s?type=series" % (UP_SPACE, SERIES_ID)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "tools", ".cache")
DATA = os.path.join(ROOT, "data")

REQUEST_GAP = 0.6          # 请求间隔（秒），避免触发风控

# 分类规则：按顺序匹配，命中即停。
# 注意：这里只是初筛，允许人工在 tools/overrides.json 中覆盖。
CATEGORY_RULES = [
    ("联动", ["联动", "派对", "组合", "会晤", "喝饮料", "一起玩", "前辈", "好多人", "模仿", "大家"]),
    ("游戏", ["游戏", "港诡", "锈湖", "魔女之家", "俄罗斯方块", "冰火", "搏斗", "捉", "寻找",
              "捡垃圾", "封锁协议", "VR公司", "万言堂", "宠物", "萌宠"]),
    ("电台", ["电台", "音乐"]),
    ("唱歌", ["唱"]),
    ("杂谈", ["闲聊", "聊天", "提问", "狡辩", "二创", "解析", "看看"]),
    ("特别回", ["特别回"]),
]

CATEGORY_ORDER = ["唱歌", "杂谈", "游戏", "联动", "电台", "特别回", "其他"]


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- HTTP

def decompress(raw, encoding):
    """弹幕接口无视 Accept-Encoding，固定返回 deflate；此处兼容 gzip/deflate 并容错。"""
    if not encoding:
        return raw
    enc = encoding.lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw


def fetch(url, referer, binary=False, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = decompress(r.read(), r.headers.get("Content-Encoding"))
    return raw if binary else json.loads(raw.decode("utf-8", "replace"))


def cached_json(name, url, referer, refresh=False):
    path = os.path.join(CACHE, name + ".json")
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    d = fetch(url, referer)
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    time.sleep(REQUEST_GAP)
    return d


# ---------------------------------------------------------------- 采集

def fetch_series(refresh=False):
    """系列列表，分页拉全。"""
    items = []
    pn = 1
    while True:
        name = "series_p%d" % pn
        url = ("https://api.bilibili.com/x/series/archives?mid=%s&series_id=%s"
               "&only_normal=true&sort=desc&pn=%d&ps=30" % (MID, SERIES_ID, pn))
        d = cached_json(name, url, UP_SPACE, refresh)
        if d.get("code") != 0:
            log("  ! 系列接口返回 code=%s msg=%s" % (d.get("code"), d.get("message")))
            break
        data = d["data"]
        items.extend(data["archives"])
        page = data["page"]
        log("  系列第 %d 页：%d 条（累计 %d / 共 %d）"
            % (pn, len(data["archives"]), len(items), page["total"]))
        if len(items) >= page["total"] or not data["archives"]:
            break
        pn += 1
    return items


def fetch_detail(bvid, refresh=False):
    """视频详情：分P、封面、统计。"""
    url = "https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid
    d = cached_json("view_%s" % bvid, url, "https://www.bilibili.com", refresh)
    if d.get("code") != 0:
        return None
    return d["data"]


# ---------------------------------------------------------------- 处理

def classify(title):
    t = title.replace("【直播回放】", "")
    for name, keys in CATEGORY_RULES:
        for k in keys:
            if k in t:
                return name
    return "其他"


def clean_title(title):
    t = re.sub(r"^【直播回放】", "", title).strip()
    t = re.sub(r"\s*\d{4}年\d+月\d+日\d+点场\s*$", "", t).strip()
    return t


def load_overrides():
    """人工校正：tools/overrides.json 形如 {"BV1xx": {"category": "杂谈"}}"""
    path = os.path.join(ROOT, "tools", "overrides.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def to_https(url):
    if not url:
        return ""
    return url.replace("http://", "https://", 1)


def thumb(url, size):
    """B 站图床支持 @宽w_高h_裁剪 后缀，返回压缩缩略图。"""
    u = to_https(url)
    return "%s@%s" % (u, size) if u else ""


def build(refresh=False):
    overrides = load_overrides()
    log("[1/3] 拉取系列列表")
    archives = fetch_series(refresh)
    log("  合计 %d 条" % len(archives))

    log("[2/3] 拉取详情与弹幕统计")
    programs = []
    for i, a in enumerate(archives, 1):
        bvid = a["bvid"]
        detail = fetch_detail(bvid, refresh)
        if not detail:
            log("  ! %d/%d %s 详情获取失败，跳过" % (i, len(archives), bvid))
            continue

        title = detail["title"]
        category = overrides.get(bvid, {}).get("category") or classify(title)

        total_dur = sum(p["duration"] for p in detail["pages"]) or 1
        video_dm = detail["stat"].get("danmaku", 0)

        parts = []
        for p in detail["pages"]:
            # 分P 没有独立的弹幕计数接口，按时长占比分摊视频级总量
            parts.append({
                "cid": p["cid"],
                "page": p["page"],
                "part": clean_title(p.get("part") or title),
                "duration": p["duration"],
                "dm_total": int(round(video_dm * p["duration"] / total_dur)),
            })

        dm_total = video_dm
        programs.append({
            "bvid": bvid,
            "aid": detail["aid"],
            "title": clean_title(title),
            "raw_title": title,
            "category": category,
            "pubdate": detail["pubdate"],
            "date": datetime.fromtimestamp(detail["pubdate"], tz).strftime("%Y-%m-%d %H:%M"),
            "duration": total_dur,
            "parts": parts,
            "view": detail["stat"].get("view", 0),
            "danmaku": detail["stat"].get("danmaku", 0),
            "reply": detail["stat"].get("reply", 0),
            "like": detail["stat"].get("like", 0),
            "cover": to_https(detail.get("pic", "")),
            "thumb": thumb(detail.get("pic", ""), "320w_200h_1c.webp"),
            "url": "https://www.bilibili.com/video/%s" % bvid,
            "dm_total": dm_total,
            "dm_per_hour": round(dm_total / (total_dur / 3600.0), 1) if total_dur else 0,
        })
        log("  %2d/%d  %-2s  %5.1fh  %2d分P  弹幕%6d  %s"
            % (i, len(archives), category, total_dur / 3600.0,
               len(parts), dm_total, clean_title(title)[:34]))

    log("[3/3] 写入产物")
    programs.sort(key=lambda p: -p["pubdate"])

    # 精彩度评分：弹幕密度为主，辅以互动量与新鲜度（仅用于展示排序与随机权重）
    dens = sorted(p["dm_per_hour"] for p in programs)
    n = len(dens) or 1
    now = time.time()
    for p in programs:
        rank = sum(1 for d in dens if d <= p["dm_per_hour"]) / float(n)
        fresh = pow(2.718281828, -((now - p["pubdate"]) / 86400.0) / 30.0)
        p["score"] = round((0.6 * rank + 0.4 * fresh) * 100, 1)
        p["dm_rank"] = round(rank * 100, 1)

    meta = {
        "source": SERIES_URL,
        "up_name": UP_NAME,
        "up_space": UP_SPACE,
        "mid": MID,
        "series_id": SERIES_ID,
        "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(programs),
        "part_count": sum(len(p["parts"]) for p in programs),
        "total_duration": sum(p["duration"] for p in programs),
        "total_danmaku": sum(p["dm_total"] for p in programs),
        "categories": CATEGORY_ORDER,
    }

    payload = {"meta": meta, "programs": programs}

    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "programs.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA, "programs.js"), "w", encoding="utf-8") as f:
        f.write("// 由 tools/collect.py 自动生成，请勿手工编辑\n")
        f.write("window.PROGRAMS = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    log("  完成：%d 个节目 / %d 个片段 / %.1f 小时 / 弹幕 %d 条"
        % (meta["count"], meta["part_count"],
           meta["total_duration"] / 3600.0, meta["total_danmaku"]))

    from collections import Counter
    c = Counter(p["category"] for p in programs)
    log("  分类分布：" + "  ".join("%s=%d" % (k, c[k]) for k in CATEGORY_ORDER if c[k]))
    other = [p["bvid"] for p in programs if p["category"] == "其他"]
    if other:
        log("  待人工校正（其他）：%s" % ", ".join(other))


tz = timezone(timedelta(hours=8))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新采集")
    args = ap.parse_args()
    try:
        build(args.refresh)
    except urllib.error.HTTPError as e:
        log("HTTP 错误：%s %s" % (e.code, e.reason))
        sys.exit(1)
