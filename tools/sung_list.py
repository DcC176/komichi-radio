# -*- coding: utf-8 -*-
"""左上角「已唱：…」歌单浮层的差分检测 —— 这是最可靠的歌曲边界信号。

为什么换区域
    上一版裁的是画面中部偏右，那里是**歌词**，每句都变 → 大量假边界。
    左上角「已唱：<歌名列表>」只在**唱完一首歌**时追加一个名字，
    是天然的、和歌边界一一对应的信号（852x480 下约 x:0~210, y:5~22）。

做法
    一次 ffmpeg 抽帧（fps=1/STEP）→ PIL/numpy 对左上角小区域做逐帧差分。
    区域小、内容稳定，切歌时 diff 会出现唯一强峰。
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", ".sunglist")
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import find_ffmpeg, get_json  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 左上角「已唱：…」区域。用相对坐标，避免分辨率变化失效。
# 实测 852x480：文字约在 x 0~210, y 5~22 → 相对 x 0~0.25, y 0.010~0.046
CROP = "crop=iw*0.26:ih*0.045:0:ih*0.008"


def video_url(bvid, cid):
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
        except Exception as e:
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError("取视频地址失败：%s" % last)


def grab(ff, url, cid, step):
    d = os.path.join(OUT, cid)
    os.makedirs(d, exist_ok=True)
    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-headers", hdr,
                    "-probesize", "5000000", "-analyzeduration", "5000000",
                    "-i", url,
                    "-vf", "%s,fps=1/%g,scale=iw*2:ih*2:flags=neighbor,format=gray"
                           % (CROP, step),
                    "-y", os.path.join(d, "g_%06d.png")],
                   capture_output=True, timeout=3600)
    return sorted(f for f in os.listdir(d) if f.startswith("g_") and f.endswith(".png"))


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
    print("抽左上角歌单区帧（每 %g 秒）…" % args.step)
    files = grab(ff, url, args.cid, args.step)
    print("共 %d 帧" % len(files))
    if not files:
        return 1

    diffs = []
    prev = None
    for i, fn in enumerate(files):
        img = np.asarray(Image.open(os.path.join(OUT, args.cid, fn)), dtype=np.float32)
        t = (i + 1) * args.step
        if prev is not None and prev.shape == img.shape:
            # 用「变化像素占比」而不是平均差：文字追加只改少数像素，
            # 平均差会被大片背景稀释；占比对「新增文字」更敏感。
            d = float(np.mean(np.abs(img - prev) > 60))
            diffs.append((t, d))
        prev = img

    path = os.path.join(OUT, "%s.json" % args.cid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cid": args.cid, "step": args.step, "crop": CROP,
                   "diffs": diffs}, f, ensure_ascii=False)
    vals = sorted(v for _, v in diffs)
    print("变化像素占比分位：p50=%.4f p90=%.4f p95=%.4f p99=%.4f max=%.4f"
          % (vals[len(vals) // 2], vals[int(len(vals) * .9)],
             vals[int(len(vals) * .95)], vals[int(len(vals) * .99)], vals[-1]))
    print("已写入 %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
