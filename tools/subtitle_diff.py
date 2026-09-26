# -*- coding: utf-8 -*-
"""画面歌名标注检测（快速版）：一次 ffmpeg 抽帧 + numpy 差分。

为什么这样做
    逐帧 `-ss` seek 要 10 秒/帧，全片 482 帧要 80 分钟。改成让 ffmpeg 顺序解码、
    按 `fps=1/STEP` 抽帧，一次调用出全部帧，耗时降到分钟级。

原理
    歌名标注（如「戒烟 - 李荣浩」）是固定样式的字幕。歌名变化时字幕区像素明显改变。
    实测同一首歌内 diff≈2~5，切歌时 diff≥25 —— 信噪比 10 倍，无需 OCR。

用法
    python tools/subtitle_diff.py --bvid BV1jshn6vEik --cid 42117366217 --step 5
产物
    tools/.subdiff/<cid>.json   { "t": [...], "diff": [...], "peaks": [...] }
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", ".subdiff")
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import find_ffmpeg, get_json  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 歌名标注在画面中部偏右：水平从 46% 起取 52% 宽，垂直从 42% 起取 12% 高（852x480 下约 443x58）
CROP = "crop=iw*0.52:ih*0.12:iw*0.46:ih*0.42"


def video_url(bvid, cid):
    """取视频地址，带重试与本地缓存（同一个地址 2 小时内有效，避免重复请求）"""
    cache = os.path.join(ROOT, "tools", ".cache", "vurl_%s_%s.txt" % (bvid, cid))
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3600:
        u = open(cache, encoding="utf-8").read().strip()
        if u:
            return u
    last = None
    for i in range(4):
        try:
            d = get_json("https://api.bilibili.com/x/player/playurl"
                         "?bvid=%s&cid=%s&fnval=1&qn=32" % (bvid, cid),
                         "https://www.bilibili.com/video/%s" % bvid)
            if d.get("code") != 0:
                raise RuntimeError("playurl code=%s" % d.get("code"))
            u = d["data"]["durl"][0]["url"]
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                f.write(u)
            return u
        except Exception as e:            # 代理偶发 502
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError("取视频地址失败：%s" % last)


def grab_all(ff, url, cid, step):
    """一次抽帧：每 step 秒一帧，缩到 320 宽灰度，输出到 <cid>/f_%06d.png"""
    d = os.path.join(OUT, cid)
    os.makedirs(d, exist_ok=True)
    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-headers", hdr,
                        "-probesize", "5000000", "-analyzeduration", "5000000",
                        "-i", url,
                        "-vf", "%s,fps=1/%g,scale=320:-1,format=gray" % (CROP, step),
                        "-y", os.path.join(d, "f_%06d.png")],
                       capture_output=True, timeout=3600)
    return sorted(f for f in os.listdir(d) if f.startswith("f_") and f.endswith(".png"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--cid", required=True)
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--ffmpeg")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    os.makedirs(OUT, exist_ok=True)
    url = video_url(args.bvid, args.cid)
    print("抽帧（每 %g 秒 1 帧）…" % args.step)
    files = grab_all(ff, url, args.cid, args.step)
    print("共 %d 帧" % len(files))
    if not files:
        return 1

    diffs = []
    prev = None
    for i, fn in enumerate(files):
        img = np.asarray(Image.open(os.path.join(OUT, args.cid, fn)), dtype=np.float32)
        t = (i + 1) * args.step          # 第 1 帧对应 t=step（fps 抽帧从 step 处开始）
        if prev is not None and prev.shape == img.shape:
            diffs.append((t, float(np.mean(np.abs(img - prev)))))
        prev = img

    # 峰值：明显高于邻域
    peaks = []
    for i in range(2, len(diffs) - 2):
        t, d = diffs[i]
        nb = [diffs[j][1] for j in range(i - 2, i + 3) if j != i]
        if nb and d > max(nb) * 1.5 and d > 8.0:
            peaks.append(t)

    # 合并「淡出旧字 + 淡入新字」产生的连续峰：
    # 一次切换在 5 秒步长下会连出 3~4 帧，间隔约 15 秒；真正的两首歌间隔 ≥45 秒。
    # 所以把间隔 < 45 秒的连续峰并成一个（取其中 diff 最大的那帧的时刻）。
    dm = dict(diffs)
    merged = []
    for t in peaks:
        if merged and t - merged[-1] < 45:
            if dm.get(t, 0) > dm.get(merged[-1], 0):
                merged[-1] = t
        else:
            merged.append(t)
    dedup = merged

    path = os.path.join(OUT, "%s.json" % args.cid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cid": args.cid, "step": args.step, "crop": CROP,
                   "diffs": diffs, "peaks": dedup}, f, ensure_ascii=False)
    print("歌名变化点（%d 个）：%s" % (len(dedup), " ".join(map(str, dedup))))
    print("已写入 %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
