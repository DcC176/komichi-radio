# -*- coding: utf-8 -*-
"""分段规则实验台（离线）：直接吃 tools/.feat 里的逐秒特征，不重新下载音频。

用法
    python tools/seg_lab2.py <cid>                 # 当前规则 vs 新规则
    python tools/seg_lab2.py <cid> --curves        # 打印逐秒曲线（30 秒一格）
    python tools/seg_lab2.py <cid> --gaps          # 打印每个间隙的长度与「说话度」
    python tools/seg_lab2.py --list                # 列出缓存里有特征的 cid

为什么要离线实验：改一个阈值就要重下音频、重跑 ffmpeg，几小时就没了。
逐秒特征已经缓存在 .feat 里，规则怎么改都是秒级出结果 —— 先在这里试，
试好了再搬进 auto_segments.py。

「说话度」是什么：歌与歌之间主播通常会说几句。人声响（rms 过阈值）但
**没有伴奏**（25-100Hz 相对能量低于阈值）—— 这正是 detect 的双判据里
mask=False 的原因之一。反过来，一首歌内部的换气/间奏是「整体安静」：
rms 也不过阈值。两者都让 mask 变 False，但含义相反，所以要分开处理。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEAT = os.path.join(ROOT, "tools", ".feat")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_segments import (otsu, smooth, detect, split_long, pad_boundaries,  # noqa: E402
                           snap_boundaries, refine_by_bass)

# 人工读帧的真值：cid -> [(歌名, 起点, 终点)]
TRUTH = {
    "42117366217": [("戒烟", 557, 1012)],
}
TOL = 20


def feat_cids():
    out = {}
    for fn in os.listdir(FEAT):
        m = fn.rsplit("_", 2)
        if len(m) != 3 or not fn.startswith("chunk_"):
            continue
        cid = m[0][len("chunk_"):]
        try:
            start = int(m[1])
        except ValueError:
            continue
        out.setdefault(cid, []).append((start, fn))
    return out


def load_rows(cid):
    """把该 cid 的所有分块特征按秒拼起来。"""
    idx = feat_cids().get(str(cid))
    if not idx:
        raise SystemExit("缓存里没有 cid %s 的特征（.feat 下共 %d 个 cid）"
                         % (cid, len(feat_cids())))
    rows = []
    for start, fn in sorted(idx):
        with open(os.path.join(FEAT, fn), encoding="utf-8") as f:
            d = json.load(f)
        for r in d.get("rows") or []:
            rows.append(r)
    rows.sort(key=lambda r: r["t"])
    # 去重（分块之间可能有一秒重叠）
    out, seen = [], set()
    for r in rows:
        if r["t"] in seen:
            continue
        seen.add(r["t"])
        out.append(r)
    return out


def gap_kind(rows, a, b, thr, bthr):
    """判断 [a,b) 这段空档是「说话」还是「安静」。

    返回 (长度, 说话占比, 平均 rms, 平均 bass)。
    说话 = 人声响（rms ≥ thr）但没伴奏（bass < bthr）。
    """
    seg = [r for r in rows if a <= r["t"] < b]
    if not seg:
        return (b - a, 0.0, -99.0, -99.0)
    talk = sum(1 for r in seg if r["rms_full"] >= thr and r["bass_ratio"] < bthr)
    return (b - a, talk / float(len(seg)),
            sum(r["rms_full"] for r in seg) / len(seg),
            sum(r["bass_ratio"] for r in seg) / len(seg))


def detect2(rows, min_seg=45.0, min_gap=50.0, max_seg=720.0, snap=90.0,
            talk_gap=14.0, talk_ratio=0.5):
    """候选新规则：空档按「性质」决定合并还是切开。

    与 detect 的差别只有一处 —— 中间长度的空白（talk_gap ~ min_gap 之间）：
        · 说话型（人声响但没伴奏，占比 ≥ talk_ratio）→ 切开：这是歌与歌之间
        · 安静型                                    → 合并：这是歌内换气/间奏
    其余步骤（外扩 / 吸附 / 超长切分）完全一致，保证可比。
    """
    if len(rows) < 30:
        return [], None, None, 0
    f = [r["rms_full"] for r in rows]
    br = [r["bass_ratio"] for r in rows]
    sm = smooth(f, 11)
    smb = smooth(br, 11)
    vals = [v for v in sm if v > -70]
    if len(vals) < 20:
        return [], None, None, 0
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
        if not merged:
            merged.append(list(r))
            continue
        a, b = merged[-1][1], r[0]
        L, talk, _, _ = gap_kind(rows, a, b, thr, bthr)
        if L < min_gap and not (L >= talk_gap and talk >= talk_ratio):
            merged[-1][1] = r[1]          # 安静或很短：合并
        else:
            merged.append(list(r))
    kept = [r for r in merged if r[1] - r[0] >= min_seg]

    changed = 0
    if snap > 0:
        kept, n0 = refine_by_bass(kept, rows, min_seg=min_seg, max_seg=max_seg)
        changed += n0
    split = split_long(kept, rows, max_seg)
    if snap > 0:
        split, n1 = pad_boundaries(split, rows, radius=snap, min_seg=min_seg)
        split, n2 = snap_boundaries(split, rows, radius=max(30, snap // 2),
                                    min_seg=min_seg)
        changed = max(changed, n1, n2)
    return split, thr, bthr, changed


def talk_gaps(rows, a, b, thr, bthr, min_len=12, ratio=0.5):
    """在 [a,b) 内找「说话型空档」：人声响但没伴奏，长度够、占比够。

    返回 [(起点, 终点)]。歌与歌之间主播通常要报幕/聊几句，就是这种空档；
    一首歌内部的换气是「整体安静」（rms 也低），不会被算进来。
    """
    out = []
    run = None
    for r in rows:
        if not (a <= r["t"] < b):
            continue
        if r["rms_full"] >= thr and r["bass_ratio"] < bthr:
            if run is None:
                run = r["t"]
        else:
            if run is not None:
                _close(out, rows, run, r["t"], thr, bthr, min_len, ratio)
                run = None
    if run is not None:
        _close(out, rows, run, b, thr, bthr, min_len, ratio)
    return out


def _close(out, rows, s, e, thr, bthr, min_len, ratio):
    seg = [r for r in rows if s <= r["t"] < e]
    if not seg or e - s < min_len:
        return
    talk = sum(1 for r in seg if r["rms_full"] >= thr and r["bass_ratio"] < bthr)
    if talk / float(len(seg)) >= ratio:
        out.append((s, e))


def split_by_talk(segs, rows, thr, bthr, long_seg=480, min_piece=120,
                  min_len=12, ratio=0.5):
    """长段内部按「说话型空档」再切 —— 只动长段，短段一个都不会多出来。

    与「全局放宽 min_gap」的区别：放宽 min_gap 会让**所有**场次的段都变碎
    （实测短段从 20 涨到 100），而这里只在段长超过 long_seg 时才找切点，
    副作用可控：本来就是「几首歌连成一段」的地方才需要被切开。
    """
    out = []
    for a, b in segs:
        if b - a <= long_seg:
            out.append([a, b])
            continue
        zones = talk_gaps(rows, a, b, thr, bthr, min_len=min_len, ratio=ratio)
        prev = a
        for s, e in zones:
            # 切在空档中间：两边都不伤到歌的头尾
            c = (s + e) // 2
            if c - prev >= min_piece and b - c >= min_piece:
                out.append([prev, c])
                prev = c
        out.append([prev, b])
    return out


def detect3(rows, min_seg=45.0, min_gap=50.0, max_seg=720.0, snap=90.0,
            long_seg=480, min_piece=120, min_len=12, ratio=0.5):
    """当前规则 + 长段按说话空档再切（detect2 是「全局放宽」，这是「只动长段」）。"""
    segs, thr, bthr, changed = detect(rows, min_seg, min_gap, max_seg, snap)
    if not segs:
        return segs, thr, bthr, changed
    segs = split_by_talk(segs, rows, thr, bthr, long_seg=long_seg,
                         min_piece=min_piece, min_len=min_len, ratio=ratio)
    return segs, thr, bthr, changed


def stats(name, segs, rows_len):
    lens = [b - a for a, b in segs]
    if not lens:
        print("  %-8s 0 段" % name)
        return
    lens_sorted = sorted(lens)
    med = lens_sorted[len(lens_sorted) // 2]
    print("  %-8s %3d 段  中位 %5.1f 分  最长 %5.1f 分  覆盖 %3.0f%%"
          % (name, len(segs), med / 60.0, max(lens) / 60.0,
             sum(lens) * 100.0 / max(rows_len, 1)))


def truth_check(segs, cid):
    for name, a, b in (TRUTH.get(str(cid)) or []):
        whole = [r for r in segs if r[0] <= a + TOL and r[1] >= b - TOL]
        cuts = sorted({r[0] for r in segs if a + TOL < r[0] < b - TOL}
                      | {r[1] for r in segs if a + TOL < r[1] < b - TOL})
        if whole:
            print("  ✅「%s」完整落在段 %d–%d 内（真值 %d–%d）" % (name, whole[0][0], whole[0][1], a, b))
        else:
            print("  ❌「%s」没有被完整覆盖（真值 %d–%d），歌内切点：%s"
                  % (name, a, b, cuts or "无"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cid", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--curves", action="store_true")
    ap.add_argument("--gaps", action="store_true")
    ap.add_argument("--min-gap", type=float, default=50.0)
    ap.add_argument("--talk-gap", type=float, default=14.0)
    ap.add_argument("--talk-ratio", type=float, default=0.5)
    args = ap.parse_args()

    if args.list:
        for cid in sorted(feat_cids()):
            print(cid)
        return 0
    if not args.cid:
        ap.error("给一个 cid，或用 --list 列出全部")

    rows = load_rows(args.cid)
    print("cid %s：%d 秒（%.1f 分钟）" % (args.cid, len(rows), len(rows) / 60.0))

    if args.curves:
        print("  t(min)   rms    bass   crest   zcr")
        for r in rows:
            if r["t"] % 30:
                continue
            print("  %6.1f  %6.1f  %6.1f  %6.1f  %5.0f"
                  % (r["t"] / 60.0, r["rms_full"], r["bass_ratio"],
                     r["crest"] or 0, r["zcr"] or 0))
        return 0

    sm = smooth([r["rms_full"] for r in rows], 11)
    smb = smooth([r["bass_ratio"] for r in rows], 11)
    thr = otsu([v for v in sm if v > -70])
    bthr = otsu([v for v in smb if v > -90])
    print("阈值：rms %.1f dB / bass %.1f dB" % (thr, bthr))

    if args.gaps:
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
        print("\n空档（前一处结束 → 后一处开始）：")
        for x, y in zip(runs, runs[1:]):
            L, talk, mr, mb = gap_kind(rows, x[1], y[0], thr, bthr)
            kind = "说话" if (L >= args.talk_gap and talk >= args.talk_ratio) else (
                "安静" if L >= args.talk_gap else "很短")
            print("  %5d–%5d  长 %3d 秒  说话占比 %.2f  rms %6.1f  bass %6.1f  → %s"
                  % (x[1], y[0], L, talk, mr, mb, kind))
        return 0

    cur, _, _, _ = detect(rows, 45.0, args.min_gap, 720.0, 90.0)
    new, _, _, _ = detect2(rows, min_gap=args.min_gap, talk_gap=args.talk_gap,
                           talk_ratio=args.talk_ratio)
    print("\n当前规则：")
    stats("detect", cur, len(rows))
    print("候选规则（空档按说话/安静分开处理）：")
    stats("detect2", new, len(rows))
    print("\n真值核对：")
    print("  当前：")
    truth_check(cur, args.cid)
    print("  候选：")
    truth_check(new, args.cid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
