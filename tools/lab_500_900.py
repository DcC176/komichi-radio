# -*- coding: utf-8 -*-
"""看 500~900 秒（戒烟前半 + 中段）的所有特征，找可用的切分信号。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import _med  # noqa: E402


def main():
    with open(os.path.join(ROOT, "tools", ".offline", "42117366217.json"),
              encoding="utf-8") as f:
        rows = json.load(f)["rows"]

    print("500~900 秒，每 4 秒一行；同时打印 15 秒滑窗中位数")
    keys = ("rms_full", "bass_abs", "bass_ratio", "crest", "zcr")

    def rm(key, t, w=15):
        return _med([r[key] for r in rows if abs(r["t"] - t) <= w])

    print("\n  t    rms   bass_abs  bass_r  crest     zcr  || 滑窗中位 rms  bass_abs  bass_r  zcr")
    for r in rows:
        t = r["t"]
        if not (500 <= t <= 900) or t % 4:
            continue
        print("  %4d %7.1f %8.1f %7.1f %8.0f %5.2f  ||  %6.1f  %7.1f  %6.1f  %5.2f"
              % (t, r["rms_full"], r["bass_abs"], r["bass_ratio"], r["crest"] or 0,
                 r["zcr"] or 0,
                 rm("rms_full", t) or 0, rm("bass_abs", t) or 0,
                 rm("bass_ratio", t) or 0, rm("zcr", t) or 0))


if __name__ == "__main__":
    main()
