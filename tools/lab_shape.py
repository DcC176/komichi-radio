# -*- coding: utf-8 -*-
"""用「频谱形状变化」找歌曲边界。

原理
    把每秒的 6 频带能量归一化成「形状向量」（减掉整体响度的影响），
    相邻窗口的形状差异（1 - 余弦相似度）在**歌曲切换**时会跳高：
    不同歌的编曲、key、配器不同 → 频谱形状不同。
    而同一首歌内部主歌/副歌切换，形状变化要小得多。

评估
    真值锚点：557（戒烟起）、1012~1022（戒烟止）。
    指标：557 / 1012 附近是否有峰值；峰值总数是否接近歌数（本分P 约 19~25 首）。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import _med  # noqa: E402

BANDS = ["b1", "b2", "b3", "b4", "b5", "b6"]


def load_bands(cid):
    with open(os.path.join(ROOT, "tools", ".offline", "%s_bands.json" % cid),
              encoding="utf-8") as f:
        return json.load(f)


def shape_diff(bands, t, win=12):
    """t 处前后各 win 秒的平均频谱形状的余弦距离（0=相同，1=完全不同）"""
    n = bands["n"]
    a = [0.0] * 6
    b = [0.0] * 6
    ca = cb = 0
    for t2 in range(max(0, t - win), t):
        for k, name in enumerate(BANDS):
            v = bands["bands"][name][t2]
            if v > -90:
                a[k] += 10 ** (v / 20.0)
                ca += 1
    for t2 in range(t, min(n, t + win)):
        for k, name in enumerate(BANDS):
            v = bands["bands"][name][t2]
            if v > -90:
                b[k] += 10 ** (v / 20.0)
                cb += 1
    if ca == 0 or cb == 0:
        return None
    sa = sum(a) or 1e-9
    sb = sum(b) or 1e-9
    a = [x / sa for x in a]
    b = [x / sb for x in b]
    dot = sum(x * y for x, y in zip(a, b))
    return 1.0 - dot


def main():
    d = load_bands("42117366217")
    n = d["n"]
    prof = [shape_diff(d, t) for t in range(n)]
    # 光滑
    sm = [_med([v for v in prof[max(0, i - 4):min(n, i + 5)] if v is not None])
          for i in range(n)]

    print("=== 频谱形状差异在关键位置的取值 ===")
    for t in (300, 400, 475, 536, 557, 600, 692, 800, 900, 955, 1000, 1012, 1022, 1100):
        v = sm[t]
        bar = "#" * int((v or 0) * 80)
        print("  %5d  %.3f  %s" % (t, v or 0, bar))

    # 找局部峰值
    peaks = []
    for i in range(30, n - 30):
        v = sm[i]
        if v is None:
            continue
        nb = [sm[j] for j in range(i - 25, i + 26)
              if j != i and sm[j] is not None]
        if not nb:
            continue
        if v >= max(nb) and v > 0.06:
            peaks.append((i, v))
    # 去重（相邻 40 秒内只留最高）
    dedup = []
    for t, v in peaks:
        if dedup and t - dedup[-1][0] < 40:
            if v > dedup[-1][1]:
                dedup[-1] = (t, v)
        else:
            dedup.append((t, v))
    print("\n=== 频谱形状变化的峰值（%d 个）===" % len(dedup))
    print("  " + "  ".join("%d(%.2f)" % (t, v) for t, v in dedup))
    hit = [t for t, _ in dedup]
    print("\n  557 附近峰值：", [t for t in hit if abs(t - 557) <= 40])
    print("  1012 附近峰值：", [t for t in hit if abs(t - 1017) <= 40])


if __name__ == "__main__":
    main()
