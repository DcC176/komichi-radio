# -*- coding: utf-8 -*-
"""给分段结果打分：段有多长、一段里是不是塞了好几首、边界贴不贴内容切换点。

用法：
    python tools/eval_segments.py                # 全库汇总 + 问题最多的 10 个分P
    python tools/eval_segments.py --top 20
    python tools/eval_segments.py --cid 40755528179

为什么要这个脚本：分段好坏不能靠「看着差不多」。能拿到的客观依据只有三样 ——
  ① 本场歌单（data/setlists.js，按演唱顺序，人工读画面浮层整理）
  ② 分段内容标签（data/labels.js，人工读帧，键是「内容开始」的秒数）
  ③ 分P 时长（一段不该长得离谱）
三者都能算出可比较的数字，改算法前后各跑一次，才知道改对了没有。

核心指标：
  · 段长中位数 —— 一首歌约 3.8 分钟，中位数明显偏大就说明「几首连成一段」
  · 超长段（>8 分钟）占比 —— 这类段几乎肯定是多首歌（或整段背景音乐没切开）
  · 歌单吻合 —— 有歌单时「歌数 / 段数」，>1.5 说明切得不够细
  · 边界偏移 —— 段起点离最近的内容切换点（labels 的键）有多少秒，越小越准
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONG_MIN = 228          # 一首歌约 3.8 分钟（前端估算「约 N 首」用的同一个数）
LONG_SEG = 480          # 超过 8 分钟的段，基本不可能是单独一首
MUSIC_CATS = ("唱歌", "电台")   # 与 app.js 的 MUSIC_CATS 一致


def load_js(path, name):
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return {}
    # 先剥掉 /* … */ 注释：setlists.js 的注释里就带了一份 `window.SETLISTS = {…}`
    # 的示例，不去注释的话正则会从示例开始匹配，把整段注释当成 JSON 来解析。
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    m = re.search(r"window\.%s\s*=\s*(\{.*\})\s*;?\s*$" % name, txt, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def load_parts():
    """(cid -> (时长, 标题, 分类))，优先用实时清单那份。

    分类必须带上：只有「唱歌 / 电台」这类才谈得上「一首歌一段」。
    游戏、杂谈、恐怖游戏整场都在说话，本来就是连续内容，段长 11 分钟
    是 max_seg 硬切的正常结果 —— 把它们混进统计里，指标会误导人。
    """
    out = {}
    for fn in ("programs_cache.json", "programs.json"):
        p = os.path.join(ROOT, "data", fn)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for prog in (data.get("programs") or []):
            for part in (prog.get("parts") or []):
                cid = str(part.get("cid"))
                if cid:
                    out[cid] = (int(part.get("duration") or 0), prog.get("title", ""),
                                prog.get("category", ""))
        if out:
            break
    return out


def med(xs):
    if not xs:
        return 0.0
    ys = sorted(xs)
    n = len(ys)
    return float(ys[n // 2]) if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10, help="列出问题最多的前 N 个分P")
    ap.add_argument("--cid", help="只看这一个分P")
    args = ap.parse_args()

    segs = load_js(os.path.join(ROOT, "data", "segments.js"), "SEGMENTS")
    songs = load_js(os.path.join(ROOT, "data", "setlists.js"), "SETLISTS")
    labels = load_js(os.path.join(ROOT, "data", "labels.js"), "SEGLABELS")
    parts = load_parts()
    if not segs:
        print("data/segments.js 里没有分段数据")
        return 1

    rows = []
    for cid, ss in segs.items():
        ss = sorted(ss or [], key=lambda s: s["start"])
        if not ss:
            continue
        if args.cid and cid != args.cid:
            continue
        lens = [s["end"] - s["start"] for s in ss]
        dur = (parts.get(cid) or (0, ""))[0]
        n_song = len(songs.get(cid) or [])
        # 段起点离最近的内容切换点（labels 的键）有多少秒 —— 只有 5 个 cid 有真值
        marks = sorted(int(k) for k in (labels.get(cid) or {})) if labels.get(cid) else []
        offs = []
        for s in ss:
            if marks:
                offs.append(min(abs(s["start"] - m) for m in marks))
        rows.append({
            "cid": cid,
            "title": (parts.get(cid) or (0, "", ""))[1][:26],
            "cat": (parts.get(cid) or (0, "", ""))[2],
            "dur": dur,
            "n": len(ss),
            "cover": (sum(lens) / dur) if dur else 0.0,
            "med": med(lens),
            "mx": max(lens),
            "long": sum(1 for L in lens if L > LONG_SEG),
            "nsong": n_song,
            "perseg": (n_song / float(len(ss))) if n_song else 0.0,
            "off": med(offs) if offs else None,
        })

    if not rows:
        print("没有匹配的分P")
        return 1

    tot_seg = sum(r["n"] for r in rows)
    tot_long = sum(r["long"] for r in rows)
    print("分段库：%d 个分P / %d 段" % (len(rows), tot_seg))
    print("段长中位数（整体）：%.1f 分钟" % (med([r["med"] for r in rows]) / 60.0))
    print("超长段（>%d 分钟）：%d 段（%.0f%%）"
          % (LONG_SEG // 60, tot_long, tot_long * 100.0 / max(tot_seg, 1)))

    music = [r for r in rows if r["cat"] in MUSIC_CATS]
    if music:
        ms = sum(r["n"] for r in music)
        ml = sum(r["long"] for r in music)
        print("\n其中唱歌 / 电台类 %d 个分P：段数 %d，段长中位 %.1f 分钟，"
              "超长段 %d 段（%.0f%%）—— 这类才该接近「一首歌一段」"
              % (len(music), ms, med([r["med"] for r in music]) / 60.0,
                 ml, ml * 100.0 / max(ms, 1)))

    with_song = [r for r in rows if r["nsong"]]
    if with_song:
        print("\n有歌单的 %d 个分P（歌数 / 段数 越大 = 切得越粗）：" % len(with_song))
        for r in sorted(with_song, key=lambda x: -x["perseg"]):
            print("  %s  %-26s 歌 %2d 首 / %2d 段 = %.2f 首每段，段长中位 %.1f 分"
                  % (r["cid"], r["title"], r["nsong"], r["n"], r["perseg"],
                     r["med"] / 60.0))

    with_off = [r for r in rows if r["off"] is not None]
    if with_off:
        print("\n有人工标签的 %d 个分P（段起点离内容切换点的中位偏移，越小越准）："
              % len(with_off))
        for r in sorted(with_off, key=lambda x: x["off"]):
            print("  %s  %-26s %2d 段，偏移中位 %.0f 秒" % (r["cid"], r["title"],
                                                            r["n"], r["off"]))

    print("\n段最粗的 %d 个分P（按段长中位数）：" % args.top)
    for r in sorted(rows, key=lambda x: -x["med"])[:args.top]:
        print("  %s  %-26s %2d 段，中位 %.1f 分，最长 %.1f 分，覆盖 %.0f%%（时长 %.1f 小时）"
              % (r["cid"], r["title"], r["n"], r["med"] / 60.0, r["mx"] / 60.0,
                 r["cover"] * 100, r["dur"] / 3600.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
