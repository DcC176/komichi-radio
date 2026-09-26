# -*- coding: utf-8 -*-
"""实验：用「伴奏平台」而不是「mask 断裂点」来定边界。

观察（cid 42117366217）：
    · 923~1018  bass_abs 在 -45~-66 乱跳（尾奏/中间段落，伴奏时有时无）
    · 1020 起   bass_abs 稳定在 -29~-34（新歌的稳态伴奏）→ 40 dB 台阶
    · 957 那个 mask 断裂点附近完全没有伴奏结构变化 → 是假边界

假设：歌曲主体 = 「伴奏绝对能量高且稳定」的平台；边界应落在平台之间的过渡带，
      再向外扩到内容真正淡出处（持续静音），这样前奏/尾奏不会被切。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import _med, otsu, silence_zones  # noqa: E402


def load(cid):
    with open(os.path.join(ROOT, "tools", ".offline", "%s.json" % cid),
              encoding="utf-8") as f:
        return json.load(f)["rows"]


def roll_med(xs, w):
    """滑窗中位数（w 秒）"""
    out = []
    h = w // 2
    for i in range(len(xs)):
        out.append(_med(xs[max(0, i - h):min(len(xs), i + h + 1)]))
    return out


def roll_std(xs, w):
    import math
    out = []
    h = w // 2
    for i in range(len(xs)):
        seg = [v for v in xs[max(0, i - h):min(len(xs), i + h + 1)] if v is not None]
        if len(seg) < 3:
            out.append(None)
            continue
        m = sum(seg) / len(seg)
        out.append(math.sqrt(sum((v - m) ** 2 for v in seg) / len(seg)))
    return out


def platforms(rows, win=15, hi_frac=0.5, std_max=6.0):
    """找「伴奏平台」：bass_abs 高且稳定的区间。

    判据：
      · bass_abs 的滑窗中位数高于「高分段阈值」（用 Otsu 分高低两档）
      · 同期滑窗标准差小于 std_max（伴奏稳定 → 歌曲主体，不是尾奏抖动）
    """
    vals = [r["bass_abs"] for r in rows]
    m = roll_med(vals, win)
    s = roll_std(vals, win)
    good = [v for v in m if v is not None and v > -90]
    if len(good) < 20:
        return []
    thr = otsu(good)
    if hi_frac != 0.5:
        sd = sorted(good)
        thr = sd[int(len(sd) * hi_frac)]
    flags = [bool(m[i] is not None and s[i] is not None
                  and m[i] > thr and s[i] < std_max) for i in range(len(rows))]
    # 合并短断点
    zones = []
    i = 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j + 1 < len(flags) and flags[j + 1]:
                j += 1
            zones.append([rows[i]["t"], rows[j]["t"] + 1])
            i = j + 1
        else:
            i += 1
    return zones, thr


def merge_gap(zones, gap):
    out = []
    for z in zones:
        if out and z[0] - out[-1][1] < gap:
            out[-1][1] = z[1]
        else:
            out.append(list(z))
    return out


def main():
    rows = load("42117366217")
    for win, hi_frac, std_max, gap in ((15, 0.5, 6.0, 40), (15, 0.6, 8.0, 40),
                                       (25, 0.5, 8.0, 60), (15, 0.45, 10.0, 45)):
        res = platforms(rows, win, hi_frac, std_max)
        if not res:
            print("win=%d frac=%.2f std=%.1f → 无平台" % (win, hi_frac, std_max))
            continue
        zones, thr = res
        zones = merge_gap(zones, gap)
        zones = [z for z in zones if z[1] - z[0] >= 45]
        print("\n=== win=%d hi=%.2f std<%.1f gap=%d → 阈 %.1f，%d 个平台 ==="
              % (win, hi_frac, std_max, gap, thr, len(zones)))
        for i, (a, b) in enumerate(zones):
            hit = ""
            if a <= 557 < b:
                hit += " [含557]"
            if a <= 1012 < b:
                hit += " [含1012]"
            print("  %2d  %5d - %5d  (%4d s)%s" % (i + 1, a, b, b - a, hit))


if __name__ == "__main__":
    main()
