# -*- coding: utf-8 -*-
"""从已抽好的帧重新算歌名变化点（不重新下载视频），用于调合并参数。

读 tools/.subdiff/<cid>_frames/ 下已抽的帧（如果存在），
否则读 tools/.subdiff/<cid>.json 里的 diffs 重新合并。
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", ".subdiff")


def recompute(cid, files_dir, step, merge_gap, thr_ratio, thr_abs):
    files = sorted(f for f in os.listdir(files_dir)
                   if f.startswith("f_") and f.endswith(".png"))
    diffs = []
    prev = None
    for i, fn in enumerate(files):
        img = np.asarray(Image.open(os.path.join(files_dir, fn)), dtype=np.float32)
        t = (i + 1) * step
        if prev is not None and prev.shape == img.shape:
            diffs.append((t, float(np.mean(np.abs(img - prev)))))
        prev = img
    peaks = []
    for i in range(2, len(diffs) - 2):
        t, d = diffs[i]
        nb = [diffs[j][1] for j in range(i - 2, i + 3) if j != i]
        if nb and d > max(nb) * thr_ratio and d > thr_abs:
            peaks.append(t)
    dm = dict(diffs)
    merged = []
    for t in peaks:
        if merged and t - merged[-1] < merge_gap:
            if dm.get(t, 0) > dm.get(merged[-1], 0):
                merged[-1] = t
        else:
            merged.append(t)
    return diffs, merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", default="42117366217")
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--merge-gap", type=float, default=45.0)
    ap.add_argument("--thr-ratio", type=float, default=1.5)
    ap.add_argument("--thr-abs", type=float, default=8.0)
    args = ap.parse_args()

    fd = os.path.join(OUT, args.cid)
    if not os.path.isdir(fd):
        print("缺帧目录 %s" % fd)
        return 1
    diffs, peaks = recompute(args.cid, fd, args.step, args.merge_gap,
                             args.thr_ratio, args.thr_abs)
    print("帧数 %d，歌名变化点 %d 个：" % (len(diffs) + 1, len(peaks)))
    print("  " + " ".join(map(str, peaks)))
    print("\n段长：")
    prev = 0
    for t in peaks:
        print("  %5d - %5d  (%4d s = %.1f min)" % (prev, t, t - prev, (t - prev) / 60))
        prev = t
    print("  %5d -  末尾" % prev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
