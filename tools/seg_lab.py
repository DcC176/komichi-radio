# -*- coding: utf-8 -*-
"""分段规则离线实验台：读 tools/.offline/<cid>.json，试各种判据/阈值，打印结果。

用法
    python tools/seg_lab.py 42117366217            # 概览
    python tools/seg_lab.py 42117366217 --curves   # 打印逐秒曲线（压缩显示）
    python tools/seg_lab.py 42117366217 --try      # 试当前 auto_segments 的规则
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFLINE = os.path.join(ROOT, "tools", ".offline")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_segments import (otsu, smooth, silence_zones, bass_steps,  # noqa: E402
                           split_long, pad_boundaries, snap_boundaries,
                           refine_by_bass, detect)


def load(cid):
    with open(os.path.join(OFFLINE, "%s.json" % cid), encoding="utf-8") as f:
        return json.load(f)


def show_curves(rows, step=1):
    """把逐秒曲线按 10 秒一格压缩打印：rms / bass_ratio"""
    print("  t(min)  rms      bass    crest  zcr")
    for r in rows[::step]:
        if r["t"] % 30:
            continue
        print("  %6.1f  %7.1f  %6.1f  %6.1f  %6.0f"
              % (r["t"] / 60.0, r["rms_full"], r["bass_ratio"],
                 r["crest"] or 0, r["zcr"] or 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cid")
    ap.add_argument("--curves", action="store_true")
    ap.add_argument("--run", action="store_true", help="跑当前 auto_segments 的规则并打印结果")
    ap.add_argument("--min-seg", type=float, default=45.0)
    ap.add_argument("--min-gap", type=float, default=50.0)
    ap.add_argument("--max-seg", type=float, default=720.0)
    ap.add_argument("--snap", type=float, default=90.0)
    args = ap.parse_args()

    d = load(args.cid)
    rows = d["rows"]
    print("cid %s  %d 秒 (%.1f 分钟)" % (args.cid, len(rows), len(rows) / 60.0))

    if args.curves:
        show_curves(rows)
        return 0

    sil = silence_zones(rows)
    ins, outs = bass_steps(rows)
    print("\n持续静音区 (%d 个)：%s" % (len(sil),
          " / ".join("%d-%d(%ds)" % (a, b, b - a) for a, b in sil[:20])))
    print("\n伴奏进入点 (%d 个)：%s" % (len(ins), " ".join(map(str, ins))))
    print("\n伴奏退出点 (%d 个)：%s" % (len(outs), " ".join(map(str, outs))))

    if args.run:
        segs, thr, bthr, changed = detect(rows, args.min_seg, args.min_gap,
                                          args.max_seg, args.snap)
        print("\n响度阈 %.2f / 伴奏阈 %.2f / 修正 %d 段" % (thr, bthr, changed))
        for i, (a, b) in enumerate(segs):
            print("  %2d  %5d - %5d  (%4d s = %.1f min)" % (i + 1, a, b, b - a, (b - a) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
