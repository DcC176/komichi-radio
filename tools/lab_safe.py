# -*- coding: utf-8 -*-
"""实验：保守边界策略 —— 只在「真正的静音/长伴奏空档」处切，其余一律向外扩满。

设计目标（对应用户口径「不要切掉歌的头尾」）：
    · 段 = 一整块连续内容，边界只落在「持续静音」或「伴奏长时间缺失」处；
    · 宁可段长，也不在歌中间切；
    · 前奏/尾奏（响度低、伴奏弱但不是静音）通过外扩收回段内。

真值锚点：戒烟 557 起 ~1012 止（已由画面歌名标注核实：554 仍是「不开灯俱乐部」，
          560 已是「戒烟」→ 边界在 554~560 之间）。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import _med, silence_zones, smooth  # noqa: E402


def load(cid):
    with open(os.path.join(ROOT, "tools", ".offline", "%s.json" % cid),
              encoding="utf-8") as f:
        return json.load(f)["rows"]


def content_mask(rows, sil_db=-50.0):
    """内容判据：整体响度高于阈值即算有内容（宽松，宁可多留）。

    只用响度、不用伴奏比 —— 因为前奏/尾奏没有伴奏但确实属于歌，
    用伴奏比会把它们判成「无内容」从而切掉歌头歌尾。
    """
    m = [r["rms_full"] > sil_db for r in rows]
    return smooth([1.0 if x else 0.0 for x in m], 9)


def blocks(rows, m, min_gap=20):
    """把有内容的秒连成块，间隔小于 min_gap 的直接合并（避免把换气当断点）"""
    flags = [v > 0.5 for v in m]
    runs = []
    i = 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j + 1 < len(flags) and flags[j + 1]:
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
    return merged


def silence_gaps(rows, db=-52.0, run=5):
    """真正的静音空档：连续 run 秒低于 db"""
    zones = silence_zones(rows)
    return [(a, b) for a, b in zones if b - a >= run]


def main():
    rows = load("42117366217")
    tmax = rows[-1]["t"] + 1
    gaps = silence_gaps(rows)
    print("静音空档（≥5s，< -52dB）：")
    print("  " + " / ".join("%d-%d(%ds)" % (a, b, b - a) for a, b in gaps))
    print("\n（对照）旧代码 mask 合并块：578-923 是「戒烟」但头切了21s、尾切了89s\n")

    for sil_db, min_gap in ((-50.0, 20), (-52.0, 25), (-48.0, 15)):
        m = content_mask(rows, sil_db)
        bl = blocks(rows, m, min_gap)
        bl = [b for b in bl if b[1] - b[0] >= 45]
        print("=== 内容阈 %.0f dB / 合并间隔 %ds → %d 段 ===" % (sil_db, min_gap, len(bl)))
        for i, (a, b) in enumerate(bl):
            tags = []
            if a <= 557 < b:
                tags.append("含557")
            if a <= 1012 < b:
                tags.append("含1012")
            print("  %2d  %5d - %5d  (%4d s = %5.1f min)%s"
                  % (i + 1, a, b, b - a, (b - a) / 60,
                     ("   [" + ",".join(tags) + "]") if tags else ""))
        print()


if __name__ == "__main__":
    main()
