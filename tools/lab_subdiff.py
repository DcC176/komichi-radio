# -*- coding: utf-8 -*-
"""画面标注差分检测（无需 OCR）。

原理
    主播把歌名以固定样式的字幕打在画面中部偏右（实测：画面高度 ~48%、水平起点 ~50%）。
    歌名变化时，那块区域的像素会明显改变；歌内则基本稳定（只有轻微动画/光影）。
    于是「字幕区像素差分的峰值」= 歌曲切换点。

    这比 OCR 轻得多：只用 ffmpeg 抓帧 + PIL/numpy 做区域差分，不需要任何模型。

验证
    已知真值：554 画面仍是「不开灯俱乐部」，560 已是「戒烟」→ 557 附近应有差分峰值。
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", ".frames")
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import find_ffmpeg, get_json  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def video_url(bvid, cid):
    d = get_json("https://api.bilibili.com/x/player/playurl"
                 "?bvid=%s&cid=%s&fnval=1&qn=32" % (bvid, cid),
                 "https://www.bilibili.com/video/%s" % bvid)
    return d["data"]["durl"][0]["url"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--cid", required=True)
    ap.add_argument("--from", dest="t0", type=float, required=True)
    ap.add_argument("--to", dest="t1", type=float, required=True)
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--crop", default="crop=iw*0.52:ih*0.12:iw*0.46:ih*0.42",
                    help="字幕区（默认中部偏右）")
    ap.add_argument("--ffmpeg")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    url = video_url(args.bvid, args.cid)
    os.makedirs(OUT, exist_ok=True)
    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA

    ts = []
    t = args.t0
    while t <= args.t1 + 1e-6:
        ts.append(t)
        t += args.step

    prev = None
    rows = []
    for t in ts:
        dst = os.path.join(OUT, "sub_%s_%06d.png" % (args.cid, int(t * 10)))
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-headers", hdr,
                            "-ss", str(max(0, t - 0.5)), "-i", url,
                            "-probesize", "2000000", "-analyzeduration", "2000000",
                            "-frames:v", "1", "-ss", "0.5",
                            "-vf", args.crop + ",scale=320:-1,format=gray",
                            "-y", dst], capture_output=True, timeout=280)
        if not os.path.exists(dst):
            rows.append((t, None))
            continue
        img = np.asarray(Image.open(dst), dtype=np.float32)
        if prev is not None and prev.shape == img.shape:
            diff = float(np.mean(np.abs(img - prev)))
        else:
            diff = None
        rows.append((t, diff))
        prev = img
        print("  %6.1f s  diff=%s" % (t, "%.2f" % diff if diff is not None else "-"))

    print("\n=== 差分峰值（相对邻域）===")
    for i in range(2, len(rows) - 2):
        t, d = rows[i]
        if d is None:
            continue
        nb = [rows[j][1] for j in range(i - 2, i + 3)
              if j != i and rows[j][1] is not None]
        if nb and d > max(nb) * 1.15 and d > 2.0:
            print("  t=%6.1f  diff=%.2f  (邻域最大 %.2f)" % (t, d, max(nb)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
