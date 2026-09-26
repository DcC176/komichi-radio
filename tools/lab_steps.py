# -*- coding: utf-8 -*-
"""分解验证：mask → pad → snap 各步单独对边界的影响，以及 split_long 的破坏。

真值：戒烟 557~1012（人工读帧）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import (smooth, otsu, silence_zones, pad_boundaries,  # noqa: E402
                           snap_boundaries, split_long, bass_steps)


def load(cid):
    with open(os.path.join(ROOT, "tools", ".offline", "%s.json" % cid),
              encoding="utf-8") as f:
        return json.load(f)["rows"]


def mask_blocks(rows, min_seg=45, min_gap=50):
    f = [r["rms_full"] for r in rows]
    br = [r["bass_ratio"] for r in rows]
    sm, smb = smooth(f, 11), smooth(br, 11)
    vals = [v for v in sm if v > -70]
    thr = otsu(vals)
    bvals = [v for v in smb if v > -90]
    bthr = otsu(bvals) if len(bvals) >= 20 else -90.0
    mask = [(sm[i] > thr and smb[i] > bthr) for i in range(len(rows))]
    runs = []
    i = 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j + 1 < len(mask) and mask[j + 1]:
                j += 1
            runs.append([rows[i]["t"], rows[j]["t"] + 1])
            i = j + 1
        else:
            i += 1
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < min_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if r[1] - r[0] >= min_seg], thr, bthr


def show(title, segs):
    print("\n=== %s（%d 段）===" % (title, len(segs)))
    for i, (a, b) in enumerate(segs):
        hit = ""
        if a <= 557 < b:
            hit += "  [含557]"
        if a <= 1012 < b:
            hit += "  [含1012]"
        print("  %2d  %5d - %5d  (%4d s)%s" % (i + 1, a, b, b - a, hit))


def main():
    rows = load("42117366217")
    blocks, thr, bthr = mask_blocks(rows)
    print("阈：响度 %.2f / 伴奏 %.2f" % (thr, bthr))
    show("① mask 合并后的块（种子）", blocks)

    sil = silence_zones(rows)
    padded, _ = pad_boundaries([list(x) for x in blocks], rows, radius=180, min_seg=45)
    show("② 外扩到静音（radius=180）", padded)

    snapped, _ = snap_boundaries([list(x) for x in padded], rows, radius=45, min_seg=45)
    show("③ 吸附到伴奏进出（radius=45）", snapped)

    split = split_long([list(x) for x in padded], rows, 720)
    show("④ split_long(720) 作用在 ② 上 —— 看它把长段切在哪", split)


if __name__ == "__main__":
    main()
