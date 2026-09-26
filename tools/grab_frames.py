# -*- coding: utf-8 -*-
"""在指定秒数抓取视频帧，用来看「歌曲名标注」在哪个时间点变化。

用途：验证自动分段边界是否准 —— 标注是 UP 主自己打的，是最可靠的真值来源。
      （注意：不能按 segments.js 的起点采样来自证，那是循环论证。）

用法
    python tools/grab_frames.py --bvid BV1jshn6vEik --cid 42117366217 \
        --at 540 550 555 557 560 565 570 580 600 650 690 700 720 900 950 1000 1010 1020
"""
import argparse
import os
import subprocess
import sys
import urllib.request
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", ".frames")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_segments import find_ffmpeg, get_json  # noqa: E402


def video_url(bvid, cid):
    """取一个较低清晰度的 MP4（抓帧不需要高清，快）"""
    d = get_json("https://api.bilibili.com/x/player/playurl"
                 "?bvid=%s&cid=%s&fnval=1&qn=32" % (bvid, cid),
                 "https://www.bilibili.com/video/%s" % bvid)
    if d.get("code") != 0:
        raise RuntimeError("playurl code=%s %s" % (d.get("code"), d.get("message")))
    durl = (d.get("data") or {}).get("durl") or []
    if not durl:
        raise RuntimeError("无 durl")
    return durl[0]["url"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--cid", required=True)
    ap.add_argument("--at", nargs="+", type=float, required=True)
    ap.add_argument("--crop", default="crop=iw*0.42:ih*0.16:0:ih*0.70",
                    help="裁剪区域，默认取左下角（歌名标注常在左下）")
    ap.add_argument("--ffmpeg")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    if not ff:
        print("未找到 ffmpeg")
        return 1
    os.makedirs(OUT, exist_ok=True)
    url = video_url(args.bvid, args.cid)
    print("视频地址已取到")
    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA

    for t in args.at:
        dst = os.path.join(OUT, "%s_%05d.png" % (args.cid, int(t)))
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error",
                            "-headers", hdr, "-ss", str(t), "-i", url,
                            "-frames:v", "1", "-vf", args.crop, "-y", dst],
                           capture_output=True, timeout=180)
        ok = os.path.exists(dst)
        print("  %6.1f s → %s %s" % (t, "OK" if ok else "失败",
                                     "" if ok else r.stderr.decode("utf-8", "replace")[-150:]))
    print("输出目录：%s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
