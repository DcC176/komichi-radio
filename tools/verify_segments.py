# -*- coding: utf-8 -*-
"""验证生成的 data/segments.js：① 已知真值的歌有没有被切完整 ② 覆盖了哪些分P。

用法：python tools/verify_segments.py
真值来自人工读帧（cid 42117366217：「戒烟」约 557–1012 秒）。

为什么要查覆盖：跑过的分P 才有片段表，没跑过的会整段播放且**不报错**。
这个脚本把「哪些 cid 有片段、哪些没有」直接打出来，避免以为已经全市覆盖。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 人工读帧的真值：cid -> [(歌名, 起点, 终点)]
TRUTH = {
    "42117366217": [("戒烟", 557, 1012)],
}

# 容差：边界精度受抽帧步长限制（默认 5 秒）。真值是人眼读帧给出的，
# 与算法输出差几个帧是正常的，不算失败 —— 只有「差出半首歌」才是真问题。
TOL = 20


def load_segments():
    path = os.path.join(ROOT, "data", "segments.js")
    s = open(path, encoding="utf-8").read()
    m = re.search(r"window\.SEGMENTS\s*=\s*(\{.*\})\s*;?\s*$", s, re.S)
    if not m:
        raise RuntimeError("无法从 segments.js 解析 window.SEGMENTS")
    return json.loads(m.group(1))


def all_parts():
    """从 programs.json 取全部 (bvid, cid, 时长, 标题, 分类)。"""
    path = os.path.join(ROOT, "data", "programs.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for p in data.get("programs", []):
        for part in p.get("parts") or []:
            out.append((p.get("bvid"), str(part.get("cid")),
                        int(part.get("duration") or 0),
                        p.get("title", ""), p.get("category", "")))
    return out


def report_coverage(segs):
    parts = all_parts()
    if not parts:
        print("\n（没有 data/programs.json，跳过覆盖统计）")
        return 0
    have = [p for p in parts if segs.get(p[1])]
    miss = [p for p in parts if not segs.get(p[1])]
    cov = sum(p[2] for p in have)
    tot = sum(p[2] for p in parts)
    print("\n覆盖情况：%d/%d 个分P（%.1f%% 时长，%.1f/%.1f 小时）"
          % (len(have), len(parts), cov * 100.0 / max(tot, 1),
             cov / 3600.0, tot / 3600.0))
    if miss:
        by_cat = {}
        for bvid, cid, dur, title, cat in miss:
            by_cat.setdefault(cat, []).append((bvid, cid, dur, title))
        print("未覆盖（这些分P 会整段播放，属正常但无跳过收益）：")
        for cat in sorted(by_cat, key=lambda c: -sum(x[2] for x in by_cat[c])):
            items = by_cat[cat]
            print("  %s：%d 个分P / %.1f 小时"
                  % (cat or "(无分类)", len(items),
                     sum(x[2] for x in items) / 3600.0))
    return len(miss)


def main():
    segs = load_segments()
    print("segments.js：%d 个 cid，%d 段"
          % (len(segs), sum(len(v) for v in segs.values())))
    bad = 0
    for cid, songs in TRUTH.items():
        rows = segs.get(cid) or []
        print("\ncid %s（%d 段）" % (cid, len(rows)))
        for name, a, b in songs:
            # 找「完整包含 [a,b]」的段，或「端点贴着真值（容差内）」的段
            whole = [r for r in rows
                     if r["start"] <= a + TOL and r["end"] >= b - TOL]
            if whole:
                w = whole[0]
                print("  ✅「%s」完整落在段 %s–%s 内（真值 %d–%d，容差 %ds）"
                      % (name, w["start"], w["end"], a, b, TOL))
                continue
            # 没有段覆盖它：再看是否有切点落在歌的**内部**（离两端都超过容差），
            # 那才是真的把歌切断了。
            cuts = sorted({r["start"] for r in rows if a + TOL < r["start"] < b - TOL}
                          | {r["end"] for r in rows if a + TOL < r["end"] < b - TOL})
            print("  ❌「%s」没有被任何一段覆盖（真值 %d–%d）" % (name, a, b))
            if cuts:
                print("     且存在落在歌内部的切点：%s" % cuts)
            bad += 1
    report_coverage(segs)
    print("\n%s" % ("全部通过" if bad == 0 else "有 %d 项未通过" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
