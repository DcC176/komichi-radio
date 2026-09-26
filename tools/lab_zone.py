# -*- coding: utf-8 -*-
"""细看 940~1050 这一段：mask 断裂点 957 与真值 1012 之间发生了什么。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import bass_steps  # noqa: E402


def main():
    with open(os.path.join(ROOT, "tools", ".offline", "42117366217.json"),
              encoding="utf-8") as f:
        rows = json.load(f)["rows"]
    ins, outs = bass_steps(rows)
    print("923~1180 之间的伴奏进出点：")
    print("  in :", [t for t in ins if 923 <= t <= 1180])
    print("  out:", [t for t in outs if 923 <= t <= 1180])
    print()
    print(" t     rms_full  bass_ratio  bass_abs")
    for r in rows:
        if 930 <= r["t"] <= 1060 and r["t"] % 2 == 0:
            mark = ""
            if r["t"] in ins:
                mark = "  <== 伴奏进入"
            if r["t"] in outs:
                mark = "  <== 伴奏退出"
            print(" %4d  %8.1f  %9.1f  %8.1f%s"
                  % (r["t"], r["rms_full"], r["bass_ratio"], r["bass_abs"], mark))


if __name__ == "__main__":
    main()
