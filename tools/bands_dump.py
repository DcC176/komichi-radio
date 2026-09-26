# -*- coding: utf-8 -*-
"""多频带实验：从本地全片 wav 提 6 个频带的逐秒能量，找「歌曲边界」信号。

动机
    已知真值锚点：557（戒烟起，画面歌名核实）、1012/1022（戒烟止）。
    之前用 25-100Hz 单频带找不到 557（因为前后两首歌伴奏连续）。
    这里换成**全频带结构**：歌曲切换时，混音的整体频谱分布会变。

产物
    tools/.offline/<cid>_bands.json  每频带每秒一条
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "tools", ".audio")
OFFLINE = os.path.join(ROOT, "tools", ".offline")
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import find_ffmpeg  # noqa: E402

# 6 个频带（Hz）：低音 / 低中 / 中 / 中高 / 高 / 超高
BANDS = [
    ("b1",  25,   100),
    ("b2",  100,  300),
    ("b3",  300,  800),
    ("b4",  800,  2000),
    ("b5",  2000, 6000),
    ("b6",  6000, 16000),
]


def run_ff(ff, args, cwd=None, timeout=3000):
    return subprocess.run([ff, "-hide_banner", "-loglevel", "error"] + args,
                          capture_output=True, timeout=timeout, cwd=cwd)


def extract(ff, wavname, cid):
    out = {}
    for name, lo, hi in BANDS:
        f = "fb_%s_%s.txt" % (cid, name)
        run_ff(ff, ["-i", wavname, "-af",
                    ("highpass=f=%d,lowpass=f=%d,asetnsamples=n=16000,"
                     "astats=metadata=1:reset=1:length=1,"
                     "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=%s:direct=1"
                     % (lo, hi, f)),
                    "-f", "null", "-"], cwd=WORK)
        vals = []
        with open(os.path.join(WORK, f), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"RMS_level=(-?[\d.]+|-?inf|nan)", line)
                if m:
                    s = m.group(1)
                    vals.append(-99.0 if s in ("nan", "inf", "-inf") else float(s))
        out[name] = vals
        print("  频带 %s (%d-%dHz)：%d 秒" % (name, lo, hi, len(vals)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", default="42117366217")
    ap.add_argument("--ffmpeg")
    args = ap.parse_args()
    ff = find_ffmpeg(args.ffmpeg)
    wav = os.path.join(WORK, "fd_%s.wav" % args.cid)
    if not os.path.exists(wav):
        print("缺 %s" % wav)
        return 1
    os.makedirs(OFFLINE, exist_ok=True)
    print("提多频带特征…")
    bands = extract(ff, wav, args.cid)
    n = min(len(v) for v in bands.values())
    path = os.path.join(OFFLINE, "%s_bands.json" % args.cid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cid": args.cid, "n": n,
                   "bands": {k: v[:n] for k, v in bands.items()}}, f,
                  ensure_ascii=False)
    print("已写入 %s（%d 秒）" % (path, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
