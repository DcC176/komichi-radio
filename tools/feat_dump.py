# -*- coding: utf-8 -*-
"""离线特征实验台：把某个分P的逐秒音频特征落盘，之后调参不再重复下载/解码。

为什么需要它
    auto_segments.py 每调一次参数都要重新拉音频、解码、算 astats（一个 2 小时分P 约 1~2 分钟）。
    分段的判据/阈值/边界规则需要反复试，在真机上试代价太高。
    这里把「取特征」和「定规则」拆开：特征只算一次存成 JSON，之后所有实验都是读 JSON + 改规则，
    秒级出结果。

产物
    tools/.offline/<cid>.json
        { "cid":..., "bvid":..., "duration":..., "rows":[ {t,rms_full,bass_ratio,crest,zcr}, ... ] }
        rows 每秒一条，覆盖整个分P。

用法
    python tools/feat_dump.py --cid 42117366217 --bvid BV1jshn6vEik
    python tools/feat_dump.py --cid 42117366217 --bvid BV1jshn6vEik --wav tools/.audio/win_xxx.wav
        （已有本地 wav 时不重新下载）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "tools", ".audio")
OFFLINE = os.path.join(ROOT, "tools", ".offline")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_segments import find_ffmpeg, get_json, audio_url  # noqa: E402


def run_ff(ff, args, cwd=None, timeout=1200):
    return subprocess.run([ff, "-hide_banner", "-loglevel", "error"] + args,
                          capture_output=True, timeout=timeout, cwd=cwd)


def dump_features(ff, wavname, cid):
    """对一个本地 wav 求逐秒特征（每秒一组；asetnsamples=n=16000 @16kHz = 1 秒）"""
    def band(filt, key):
        out = "fd_%s_%s.txt" % (cid, key.split(".")[-1])
        run_ff(ff, ["-i", wavname, "-af",
                    ("%sasetnsamples=n=16000,astats=metadata=1:reset=1:length=1,"
                     "ametadata=print:key=%s:file=%s:direct=1" % (filt, key, out)),
                    "-f", "null", "-"], cwd=WORK)
        vals = []
        with open(os.path.join(WORK, out), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(re.escape(key) + r"=(-?[\d.]+|-?inf|nan)", line)
                if not m:
                    continue
                s = m.group(1)
                vals.append(None if s in ("nan", "inf", "-inf") else float(s))
        return vals

    K_RMS = "lavfi.astats.Overall.RMS_level"
    K_CREST = "lavfi.astats.1.Crest_factor"
    K_ZCR = "lavfi.astats.1.Zero_crossings_rate"
    K_FLAT = "lavfi.astats.1.Flat_factor"

    full = band("", K_RMS)
    bass = band("highpass=f=25,lowpass=f=100,", K_RMS)
    crest = band("", K_CREST)
    zcr = band("", K_ZCR)
    flat = band("", K_FLAT)

    n = min(len(full), len(bass), len(crest), len(zcr), len(flat))
    rows = []
    for i in range(n):
        fv = full[i]
        rows.append({
            "t": i,
            "rms_full": -80.0 if fv is None else fv,
            "bass_abs": -99.0 if bass[i] is None else bass[i],
            "bass_ratio": ((bass[i] - fv) if (bass[i] is not None and fv is not None
                                              and fv > -70) else -99.0),
            "crest": crest[i],
            "zcr": zcr[i],
            "flat": flat[i],
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", required=True)
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--wav", help="复用已有 wav，不从网上重下")
    ap.add_argument("--ffmpeg")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    if not ff:
        print("未找到 ffmpeg")
        return 1
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(OFFLINE, exist_ok=True)

    wavname = "fd_%s.wav" % args.cid
    dst = os.path.join(WORK, wavname)
    if args.wav and os.path.exists(args.wav):
        wavname = os.path.abspath(args.wav)
        print("复用本地 wav：%s" % wavname)
    elif not os.path.exists(dst):
        url = audio_url(args.bvid, args.cid)
        hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA
        print("下载音频…")
        r = run_ff(ff, ["-headers", hdr, "-i", url, "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", "-y", wavname], cwd=WORK)
        if not os.path.exists(dst) or os.path.getsize(dst) < 1000:
            print("下载失败：%s" % r.stderr.decode("utf-8", "replace")[-300:])
            return 1
        wavname = dst

    print("求特征…")
    rows = dump_features(ff, wavname, args.cid)
    out = {"cid": args.cid, "bvid": args.bvid, "duration": len(rows), "rows": rows}
    path = os.path.join(OFFLINE, "%s.json" % args.cid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("已写入 %s（%d 秒）" % (path, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
