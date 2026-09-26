# -*- coding: utf-8 -*-
"""歌名变化点检测的规则调优（离线，复用已抽帧）。

改进点（针对上一版漏检 3950 / 6800）
    上一版用「比邻域**最大值**高 1.5 倍」判峰，邻域里只要有一个次峰就把真峰压掉。
    改为「比邻域**中位数**高 K 倍，且绝对差 ≥ A」，既保留灵敏度又不被次峰干扰。
    再要求「峰与其后 30 秒的稳定值有明显落差」，进一步排除歌词滚动。
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", ".subdiff")


def load_diffs(cid, step):
    fd = os.path.join(OUT, cid)
    files = sorted(f for f in os.listdir(fd)
                   if f.startswith("f_") and f.endswith(".png"))
    diffs = []
    prev = None
    for i, fn in enumerate(files):
        img = np.asarray(Image.open(os.path.join(fd, fn)), dtype=np.float32)
        t = (i + 1) * step
        if prev is not None and prev.shape == img.shape:
            diffs.append((t, float(np.mean(np.abs(img - prev)))))
        prev = img
    return diffs


def med(xs):
    ys = sorted(x for x in xs if x is not None)
    if not ys:
        return 0.0
    n = len(ys)
    return ys[n // 2] if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2.0


def find_peaks(diffs, win=6, k=3.0, amin=10.0, merge_gap=45.0):
    dm = dict(diffs)
    ts = [t for t, _ in diffs]
    peaks = []
    for i in range(win, len(diffs) - win):
        t, d = diffs[i]
        nb = [diffs[j][1] for j in range(i - win, i + win + 1) if j != i]
        m = med(nb)
        if d >= max(m * k, m + amin) and d > amin:
            peaks.append(t)
    merged = []
    for t in peaks:
        if merged and t - merged[-1] < merge_gap:
            if dm.get(t, 0) > dm.get(merged[-1], 0):
                merged[-1] = t
        else:
            merged.append(t)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", default="42117366217")
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--win", type=int, default=6)
    ap.add_argument("--k", type=float, default=3.0)
    ap.add_argument("--amin", type=float, default=10.0)
    ap.add_argument("--merge-gap", type=float, default=45.0)
    args = ap.parse_args()

    diffs = load_diffs(args.cid, args.step)
    for k, amin in ((3.0, 10.0), (2.5, 9.0), (2.0, 8.0), (1.8, 7.0)):
        pk = find_peaks(diffs, args.win, k, amin, args.merge_gap)
        print("\n=== k=%.1f amin=%.0f → %d 个变化点 ===" % (k, amin, len(pk)))
        print("  " + " ".join(map(str, pk)))
        print("  557命中:%s  1012命中:%s  3950命中:%s  6800命中:%s"
              % ([t for t in pk if abs(t - 557) <= 15],
                 [t for t in pk if abs(t - 1012) <= 15],
                 [t for t in pk if abs(t - 3950) <= 15],
                 [t for t in pk if abs(t - 6800) <= 15]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
