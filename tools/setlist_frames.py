# -*- coding: utf-8 -*-
"""抽取各分P 的「歌单浮层」画面并拼成图，便于读取歌单。

主播把歌单以浮层形式显示在画面里（有时左下角小字、有时整屏大字），并且会随进度缓慢滚动，
所以单帧可能只露出部分。本脚本对每个分P 取多个时间点，裁出浮层区域后拼图，
再由人（或具备视觉能力的模型）读取歌单文字。

用法：
    python tools/setlist_frames.py --bvid BV16RuU6AEvc
    python tools/setlist_frames.py --category 唱歌
产物：
    tools/.setlist/<cid>_<时刻>.png   单帧裁切
    tools/.setlist/sheet_<n>.png      拼图（每张 4 格）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", ".setlist2")
DATA = os.path.join(ROOT, "data")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 采样比例：浮层通常在歌曲进行中可见，取多个位置提高命中率
RATIOS = [0.30, 0.45, 0.60, 0.75, 0.88]
# 浮层区域（相对 852x480 的 480p 画面）：左侧 + 下方
CROP = "crop=852:180:0:110"
SCALE = "scale=852:180:flags=lanczos"


def find_ffmpeg(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("FFMPEG")
    if env and os.path.exists(env):
        return env
    w = shutil.which("ffmpeg")
    if w:
        return w
    for base in (os.path.expandvars(r"%LOCALAPPDATA%\Packages"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs")):
        if os.path.isdir(base):
            for dirpath, _d, files in os.walk(base):
                if "ffmpeg.exe" in files:
                    return os.path.join(dirpath, "ffmpeg.exe")
    return None


def get_json(url, referer):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def video_url(bvid, cid):
    d = get_json("https://api.bilibili.com/x/player/playurl"
                 "?bvid=%s&cid=%s&fnval=16&fourk=1" % (bvid, cid),
                 "https://www.bilibili.com/video/%s" % bvid)
    vids = (d.get("data") or {}).get("dash", {}).get("video") or []
    if not vids:
        raise RuntimeError("无视频流")
    return sorted(vids, key=lambda v: -v["bandwidth"])[0]["baseUrl"]


def run(ff, args, cwd=None):
    return subprocess.run([ff, "-hide_banner", "-loglevel", "error"] + args,
                          capture_output=True, timeout=300, cwd=cwd)


def tile(ff, files, out, cols=2):
    """把多张图拼成网格。

    注意：`tile` 滤镜是把同一个视频流的帧铺开，不能堆叠不同图片；
    拼接不同图片必须用 hstack / vstack。
    """
    if not files:
        return
    if len(files) == 1:
        shutil.copy(os.path.join(OUT, files[0]), os.path.join(OUT, out))
        return

    inputs = []
    for f in files:
        inputs += ["-i", f]

    rows = (len(files) + cols - 1) // cols
    fc = []
    labels = []
    for r in range(rows):
        row = files[r * cols:(r + 1) * cols]
        if len(row) == 1:
            labels.append("[%d]" % (r * cols))
            continue
        idx = "".join("[%d]" % (r * cols + i) for i in range(len(row)))
        fc.append("%shstack[r%d]" % (idx, r))
        labels.append("[r%d]" % r)

    if len(labels) == 1:
        fc.append("%scopy[out]" % labels[0])
    else:
        # vstack 的输入数必须显式声明（默认只接受 2 个）
        fc.append("%svstack=inputs=%d[out]" % ("".join(labels), len(labels)))

    run(ff, inputs + ["-filter_complex", ";".join(fc),
                      "-map", "[out]", "-frames:v", "1", "-y", out], cwd=OUT)


def load_segments():
    """读取 data/segments.js（它是 JS 赋值语句，取其中的 JSON 部分）"""
    path = os.path.join(DATA, "segments.js")
    if not os.path.exists(path):
        return {}
    text = open(path, encoding="utf-8").read()
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid")
    ap.add_argument("--category")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--by-segments", action="store_true",
                    help="按 data/segments.js 里每个分段的起点采样（推荐：字幕/歌词随歌曲变化）")
    ap.add_argument("--offset", type=int, default=30,
                    help="相对分段起点的偏移秒数（默认 30）")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    if not ff:
        print("未找到 ffmpeg")
        return 1
    os.makedirs(OUT, exist_ok=True)

    with open(os.path.join(DATA, "programs.json"), encoding="utf-8") as f:
        programs = json.load(f)["programs"]

    if args.bvid:
        targets = [p for p in programs if p["bvid"] == args.bvid]
    elif args.category:
        targets = [p for p in programs if p["category"] == args.category]
    else:
        print("请指定 --bvid 或 --category")
        return 1

    segs = load_segments() if args.by_segments else {}
    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA
    made = []

    for p in targets:
        print("[%s] %s" % (p["category"], p["title"]), flush=True)
        for part in p["parts"]:
            cid, dur = part["cid"], part["duration"]
            try:
                url = video_url(p["bvid"], cid)
            except Exception as e:
                print("   ! 取流失败：%s" % e)
                continue

            if args.by_segments:
                starts = [s["start"] + args.offset for s in segs.get(str(cid), [])]
                if not starts:
                    print("   cid %s：无分段数据，跳过" % cid)
                    continue
            else:
                starts = [int(dur * r) for r in RATIOS]

            for k, t in enumerate(starts, 1):
                # 裁剪直接在取帧时完成，不产生中间文件：
                # 本机删除受保护（累计删除会触发保护并终止进程），所以绝不做「先写后删」
                crop = "%s_%05d.png" % (cid, t)
                run(ff, ["-headers", hdr, "-ss", str(t), "-i", url,
                         "-frames:v", "1", "-q:v", "2",
                         "-vf", "%s,%s" % (CROP, SCALE),
                         "-y", crop], cwd=OUT)
                if os.path.exists(os.path.join(OUT, crop)):
                    made.append(crop)
            print("   cid %s：采样 %d 帧" % (cid, len(starts)), flush=True)

    # 每 8 格拼一张（竖排条带，歌词/字幕更易读）
    per = 8
    for i in range(0, len(made), per):
        chunk = made[i:i + per]
        out = "sheet_%02d.png" % (i // per + 1)
        tile(ff, chunk, out, cols=1)
        print("拼图：%s（%s）" % (out, " / ".join(c.replace(".png", "") for c in chunk)))

    print("\n产物目录：%s" % OUT)
    print("读取 sheet_*.png 后，把标签写进 data/labels.js（分段内容）或 data/setlists.js（整场歌单）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
