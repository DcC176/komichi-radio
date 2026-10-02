# -*- coding: utf-8 -*-
"""离线重算全部分段：只吃 tools/.feat 里已缓存的逐秒特征。

用法
    python tools/seg_rebuild.py              # 只对比：打印新旧差异，一个字节都不写
    python tools/seg_rebuild.py --write      # 真的写回 data/segments.js（自动留 .bak）
    python tools/seg_rebuild.py --cid 42117366217

为什么要它：改一次分段规则，在线流程要把每个分P 的音频重新下载 + ffmpeg 解码，
几小时起步；而「逐秒特征」与规则无关、已经缓存在 .feat 里，重算只要几秒。
调参的循环于是从几小时变成几秒 —— 也和 tools/seg_lab2.py 用的是同一批缓存。

与在线流程的一致性：detect / apply_sung_keys / split_by_talk 全部 import
auto_segments，唯一的差别是「下载音频算特征」换成「读缓存」。
没有特征缓存的分P 不会被重算，保持原样（所以不会把没跑过的地方写坏）。
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
FEAT = os.path.join(TOOLS, ".feat")
sys.path.insert(0, TOOLS)

import auto_segments as A  # noqa: E402


def feat_index():
    """{cid: [(start, 文件名), ...]} —— .feat 里每个分块的起点与文件名。"""
    out = {}
    for fn in os.listdir(FEAT):
        if not fn.startswith("chunk_"):
            continue
        parts = fn.rsplit("_", 2)
        if len(parts) != 3:
            continue
        try:
            start = int(parts[1])
        except ValueError:
            continue
        out.setdefault(parts[0][len("chunk_"):], []).append((start, fn))
    return out


def load_rows(idx):
    """把一个 cid 的所有分块特征按秒拼起来（分块之间有重叠，按秒去重）。"""
    rows = {}
    for _start, fn in idx:
        with open(os.path.join(FEAT, fn), encoding="utf-8") as f:
            for r in (json.load(f).get("rows") or []):
                rows[r["t"]] = r
    return [rows[t] for t in sorted(rows)]


def med(xs):
    if not xs:
        return 0.0
    ys = sorted(xs)
    n = len(ys)
    return float(ys[n // 2]) if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写回 data/segments.js")
    ap.add_argument("--cid", help="只重算这一个分P")
    args = ap.parse_args()

    idx_all = feat_index()
    if args.cid:
        if args.cid not in idx_all:
            print("特征缓存里没有 cid %s" % args.cid)
            return 1
        idx_all = {args.cid: idx_all[args.cid]}

    path = os.path.join(ROOT, "data", "segments.js")
    old = A.load_existing_segments(path)          # {cid: [(a,b), ...]}
    d = A.default_args()
    print("规则：min_seg=%g min_gap=%g max_seg=%g snap=%g  CACHE_VERSION=%s"
          % (d.min_seg, d.min_gap, d.max_seg, d.snap, A.CACHE_VERSION))

    cache = {}
    rows_before = rows_after = 0
    long_before = long_after = 0
    med_before, med_after = [], []
    rows_n = 0
    touched = 0
    for cid in sorted(idx_all):
        rows = load_rows(idx_all[cid])
        if len(rows) < 60:
            continue
        keys = A.load_sungkeys(cid) or []
        segs = A.detect(rows, d.min_seg, d.min_gap, d.max_seg, d.snap,
                        keys=keys)[0]
        segs = [[int(a), int(b)] for a, b in segs]
        cache["%s|%g|%g|%g|%s" % (cid, d.min_seg, d.min_gap, d.snap,
                                  A.CACHE_VERSION)] = segs
        rows_n += 1
        if cid in old and old[cid] != segs:
            touched += 1
        ob = old.get(cid) or []
        for tag, lst in (("b", ob), ("a", segs)):
            lens = [b - a for a, b in lst]
            if tag == "b":
                rows_before += len(lens)
                long_before += sum(1 for L in lens if L > A.LONG_SEG)
                med_before.append(med(lens))
            else:
                rows_after += len(lens)
                long_after += sum(1 for L in lens if L > A.LONG_SEG)
                med_after.append(med(lens))

    print("%d 个分P 有特征、可重算（其中 %d 个结果与现状不同）" % (rows_n, touched))
    print("  段数      %5d → %5d" % (rows_before, rows_after))
    print("  超长段    %5d（%.0f%%） → %5d（%.0f%%）"
          % (long_before, long_before * 100.0 / max(rows_before, 1),
             long_after, long_after * 100.0 / max(rows_after, 1)))
    print("  段长中位  %.1f 分钟 → %.1f 分钟" % (med(med_before) / 60.0, med(med_after) / 60.0))

    if not args.write:
        print("\n（未写文件；确认无误后加 --write）")
        return 0
    n = A.write_segments(cache, dry_run=False)
    print("\n已写入，共 %d 个分P" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
