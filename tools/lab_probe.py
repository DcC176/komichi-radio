# -*- coding: utf-8 -*-
"""对比「真边界」与「歌内假边界」的局部特征，找出可区分的判据。

真值（人工读帧）：557（戒烟起）、1012（戒烟止）
候选中可疑的「歌内假边界」：899 / 955（都在戒烟内部）
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import _med  # noqa: E402


def load(cid):
    with open(os.path.join(ROOT, "tools", ".offline", "%s.json" % cid),
              encoding="utf-8") as f:
        return json.load(f)["rows"]


def probe(rows, t, name, win=20):
    """打印 t 前后各 win 秒的特征中位数，以及差异"""
    def seg(key, lo, hi):
        return _med([r[key] for r in rows if lo <= r["t"] < hi])
    print("\n--- %s @ t=%d ---" % (name, t))
    for key in ("rms_full", "bass_ratio", "bass_abs", "crest", "zcr", "flat"):
        b = seg(key, t - win, t)
        a = seg(key, t, t + win)
        if b is None or a is None:
            continue
        print("  %-11s 前 %7.2f   后 %7.2f   差 %+7.2f" % (key, b, a, a - b))
    # 前后 30 秒里有没有「长伴奏空档」（bass_ratio 掉到很低）
    for label, lo, hi in (("前 60s", t - 60, t), ("后 60s", t, t + 60)):
        vals = [r["bass_ratio"] for r in rows if lo <= r["t"] < hi and r["bass_ratio"] > -95]
        if vals:
            print("  %s bass_ratio: min %.1f / med %.1f / max %.1f"
                  % (label, min(vals), _med(vals), max(vals)))


def main():
    rows = load("42117366217")
    for t, name in ((557, "真边界：戒烟起"), (1012, "真边界：戒烟止"),
                    (899, "疑似假边界（歌内）"), (955, "疑似假边界（歌内）"),
                    (475, "候选：? "), (536, "候选：?"), (830, "候选：?"),
                    (990, "候选：?"), (1022, "候选：?")):
        probe(rows, t, name)


if __name__ == "__main__":
    main()
