# -*- coding: utf-8 -*-
"""试「直接按伴奏进入点切分」的方案，看是否贴近人工读帧真值。

已知真值（cid 42117366217，人工读帧）：
    戒烟 起 ~557，止 ~1012
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import bass_steps, silence_zones, _med  # noqa: E402


def load(cid):
    with open(os.path.join(ROOT, "tools", ".offline", "%s.json" % cid),
              encoding="utf-8") as f:
        return json.load(f)


def cut_by_enters(rows, min_seg=60, max_seg=600):
    """直接用伴奏进入点切分整片。

    思路：伴奏进入 = 一首歌开始。于是把「进入点」当作候选边界，
    按「尽量长但不超 max_seg，且不短于 min_seg」的规则挑切点。
    没有进入点的长区间再交给静音区兜底。
    """
    ins, outs = bass_steps(rows)
    ins = sorted(t for t in ins if 20 <= t <= rows[-1]["t"] - 20)
    sil = silence_zones(rows)
    t0, t1 = 0, rows[-1]["t"] + 1

    segs = []
    prev = t0
    i = 0
    while i < len(ins):
        t = ins[i]
        if t - prev >= min_seg:
            segs.append([prev, t])
            prev = t
        i += 1
    if t1 - prev >= min_seg:
        segs.append([prev, t1])
    return segs, ins, outs, sil


def main():
    d = load("42117366217")
    rows = d["rows"]
    for min_seg, max_seg in ((60, 600), (75, 480), (90, 420), (45, 720)):
        segs, ins, outs, sil = cut_by_enters(rows, min_seg, max_seg)
        print("\n=== min_seg=%d max_seg=%d → %d 段 ===" % (min_seg, max_seg, len(segs)))
        for i, (a, b) in enumerate(segs):
            flag = ""
            if a <= 557 <= b or a <= 1012 <= b:
                flag = "   <-- 含真值边界"
            print("  %2d  %5d - %5d  (%4d s = %.1f min)%s"
                  % (i + 1, a, b, b - a, (b - a) / 60, flag))


if __name__ == "__main__":
    main()
