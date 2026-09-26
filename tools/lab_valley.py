# -*- coding: utf-8 -*-
"""凹谷检测：找「歌曲之间的能量收尾」。

关键发现（cid 42117366217）：
    t=557  rms -33.0 → -64.7（掉 31.7 dB），bass_abs -50.8 → -77.6
    这是「戒烟」开始前，前一首的收尾凹谷。
    而旧代码的 silence_zones 要求「连续 3 秒低于 -55dB」，1 秒的凹谷全被漏掉。

判据
    某秒的 rms 比「前后各 6 秒的中位数」低 DV 以上 → 算一个凹谷点。
    这些凹谷点就是**歌与歌之间**的位置（一首歌结束/下一首开始的过渡）。

评估
    真值锚点 557、1012~1022 是否命中；凹谷总数是否接近歌数。
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


def valleys(rows, drop=10.0, win=6):
    """返回凹谷点 [(t, depth)]，depth = 前后中位数 - 该秒 rms"""
    out = []
    n = len(rows)
    for i in range(win, n - win):
        before = _med([rows[j]["rms_full"] for j in range(i - win, i)])
        after = _med([rows[j]["rms_full"] for j in range(i + 1, i + 1 + win)])
        here = rows[i]["rms_full"]
        if before is None or after is None:
            continue
        # 要求它是局部最低（比前后都低）
        if here >= before or here >= after:
            continue
        ref = min(before, after)
        d = ref - here
        if d >= drop:
            out.append((rows[i]["t"], d))
    return out


def main():
    rows = load("42117366217")
    for drop in (8, 10, 12, 15):
        v = valleys(rows, drop)
        hit557 = [t for t, _ in v if abs(t - 557) <= 6]
        hit1012 = [t for t, _ in v if abs(t - 1017) <= 10]
        print("\n=== 落差≥%.0f dB → %d 个凹谷 ===" % (drop, len(v)))
        print("  557 命中：%s   1012/1022 命中：%s" % (hit557, hit1012))
        print("  凹谷点：" + " ".join("%d(%.0f)" % (t, d) for t, d in v[:70]))
    # 打印 540-580 的深度分布
    print("\n=== 540~580 各秒的落差（相对前后 6 秒中位数）===")
    n = len(rows)
    for i in range(540, 581):
        before = _med([rows[j]["rms_full"] for j in range(i - 6, i)])
        after = _med([rows[j]["rms_full"] for j in range(i + 1, i + 7)])
        ref = min(x for x in (before, after) if x is not None)
        d = ref - rows[i]["rms_full"]
        mark = "  <== 凹谷" if d >= 8 else ""
        print("  %4d  rms %7.1f  落差 %5.1f%s" % (rows[i]["t"], rows[i]["rms_full"], d, mark))


if __name__ == "__main__":
    main()
