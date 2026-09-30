# -*- coding: utf-8 -*-
"""用画面左上角「已唱：…」浮层的差分峰值精修歌曲边界。

为什么需要它（实测结论，非推测）
    cid 42117366217 上，人工读帧的真值：「戒烟」约在 557 秒开始。
    但音频判据在同一首歌内部给出了 18 个「伴奏进入/退出」点
    （556 608 627 694 752 830 873 899 955 990 / 583 615 669 865 902 946 972 1003），
    也就是说伴奏曲线**无法区分「一首歌结束」与「歌内间奏」**——
    两者都是伴奏短暂减弱。用它做切点，必然把歌从中间切开：
    原算法的输出是 488–957 + 957–1166，戒烟（557–1012）正好被 957 这一刀劈开。

    而左上角「已唱：<歌名列表>」浮层只在**唱完一首歌**时追加一个歌名，
    于是它的像素变化与歌曲边界一一对应。实测信噪比：

        5 秒步长、1447 帧：p50~p95 恒为 0.0000，只有 39 个非零帧
        1 秒步长、 125 帧：只有 1 个非零帧（t=566，diff=0.0892，即「戒烟」被登记）
        1 秒步长、 130 帧：只有 1 个非零帧（t=998，diff=0.1442，即戒烟唱完）

    对比中部偏右的歌词区：同样方法给出 37~51 个假峰。区域选择是成败关键。

峰值语义
    峰值出现在「歌名被写进列表」的时刻，也就是主播**开始唱这首歌**的时候
    （实测：t=300 的浮层是「已唱：不开灯俱乐部」，t=565 变成「已唱：戒烟、不开灯俱乐部」，
     而人工读帧的真值是戒烟约在 557 秒开始 —— 峰值正是它的起点）。
    因此峰值天然是**相邻两首歌的切分点**，放在这里不会切到任何一首歌的内部。
    这就是「避免把歌的头尾切掉」的直接实现。

使用
    # 精修单个 cid（会联网取视频流抽帧；已抽过则自动复用）
    python tools/seg_refine.py --bvid BV1jshn6vEik --cid 42117366217

    # 供 auto_segments.py 调用
    from seg_refine import detect_keys
    keys = detect_keys(bvid, cid, duration)   # -> [185, 570, 1000, ...] 秒
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from auto_segments import find_ffmpeg, get_json, NO_WINDOW, HIDE_SI  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 左上角「已唱：…」区域。相对坐标，避免分辨率变化失效。
# 实测 852x480：文字约在 x 0~210, y 5~22 → 相对 x 0~0.25, y 0.010~0.046
CROP = "crop=iw*0.26:ih*0.045:0:ih*0.008"

FRAME_DIR = os.path.join(ROOT, "tools", ".sungref")
CACHE_DIR = os.path.join(ROOT, "tools", ".cache")

# 判据阈值。实测真信号的背景恒为 0，峰值 0.06~0.96，所以 0.03 把两者分得很开；
# 取出后还会做「邻域独占」合并，压掉同一次追加被相邻两帧各记一次的情况。
PEAK_MIN = 0.03
# 同一首歌的重复登记间隔实测 ≤ 20 秒（如 5875/5895、3800/3805），
# 而两首歌的间隔以分钟计，所以 30 秒能可靠合并前者、不误伤后者。
PEAK_GAP = 30.0
# 开场噪声：浮层自身在开场阶段逐字出现/滚动，实测 0~80 秒连续 15 个高值帧。
# 这段不是歌曲边界，直接丢弃。
WARMUP = 90.0

# 「这块区域到底是不是歌单浮层」的合法性判据。
# 实测两类画面：
#   真信号（42117366217）：1446 帧中非零 24 帧 = 1.66%，孤立峰，峰间大量零值
#   噪声  （42117366199）： 682 帧中非零 44 帧 = 6.45%，且 3200~3415 连续每帧都变
#     —— 那是主播在末尾翻歌单/开聊天框，左上角整块在动，不是「追加一个歌名」。
# 阈值取 4%：离两类各有约 1.5 倍的余量。
NOISE_RATIO_MAX = 0.04
# 另一个结构性判据：合并后至少要有 2 个峰才有骨架可言（1 个峰切不出段）。
MIN_PEAKS = 2


def cache_key(cid, step):
    return "%s_%g" % (cid, step)


def video_url(bvid, cid):
    """取 MP4（fnval=1）地址。抽帧只需要 360P，省带宽。"""
    cache = os.path.join(CACHE_DIR, "vurl_%s_%s.txt" % (bvid, cid))
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3600:
        u = open(cache, encoding="utf-8").read().strip()
        if u:
            return u
    last = None
    for i in range(4):
        try:
            d = get_json("https://api.bilibili.com/x/player/playurl"
                         "?bvid=%s&cid=%s&fnval=1&qn=32" % (bvid, cid),
                         "https://www.bilibili.com/video/%s" % bvid)
            if d.get("code") != 0:
                raise RuntimeError("playurl code=%s" % d.get("code"))
            u = d["data"]["durl"][0]["url"]
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                f.write(u)
            return u
        except Exception as e:
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError("取视频地址失败：%s" % last)


def grab_frames(ff, url, cid, step, duration=None, progress=None):
    """一次 ffmpeg 抽帧（fps=1/step）。返回按序排列的文件名。

    逐帧 -ss 定位要 ~10 秒/帧，整支视频会到几小时；这里用 filter 的 fps，
    整支 2 小时视频约 25 秒。
    """
    d = os.path.join(FRAME_DIR, cid)
    os.makedirs(d, exist_ok=True)
    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
           "-headers", hdr,
           "-probesize", "5000000", "-analyzeduration", "5000000",
           "-i", url,
           "-vf", "%s,fps=1/%g,scale=iw*2:ih*2:flags=neighbor,"
                  "format=gray" % (CROP, step),
           "-y", os.path.join(d, "g_%06d.png")]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="ignore",
                            creationflags=NO_WINDOW, startupinfo=HIDE_SI)
    errbuf = []

    def drain_err():                      # stderr 必须有人读，否则管道写满 ffmpeg 会卡住
        for line in proc.stderr:
            errbuf.append(line)

    threading.Thread(target=drain_err, daemon=True).start()
    for line in proc.stdout:
        # -progress 每半秒吐一行 out_time=00:01:23.45 → 除以总时长就是抽帧进度。
        # 精修比音频分析还久（实测 85 秒 vs 36 秒），不报的话进度条会停在 100% 假死。
        if progress and duration and line.startswith("out_time="):
            try:
                h, m, s = line.split("=", 1)[1].strip().split(":")
                progress(min(1.0, (int(h) * 3600 + int(m) * 60 + float(s)) / float(duration)))
            except (ValueError, IndexError):
                pass
    proc.wait(timeout=3600)
    if proc.returncode != 0:
        sys.stderr.write("".join(errbuf)[:400] + "\n")
    return sorted(f for f in os.listdir(d)
                  if f.startswith("g_") and f.endswith(".png"))


def diff_series(dirpath, files, step, start=0.0):
    """返回 [(t, 变化像素占比)]。

    用「变化像素占比」而不是平均差：文字追加只改少数像素，平均差会被大片背景稀释。
    """
    rows = []
    prev = None
    for i, fn in enumerate(files):
        img = np.asarray(Image.open(os.path.join(dirpath, fn)), dtype=np.float32)
        t = start + (i + 1) * step
        if prev is not None and prev.shape == img.shape:
            rows.append((t, float(np.mean(np.abs(img - prev) > 60))))
        prev = img
    return rows


def pick_peaks(rows, min_v=PEAK_MIN, gap=PEAK_GAP, warmup=WARMUP):
    """从逐帧差分里挑出「已唱列表被追加」的时刻。

    返回 (peaks, usable)：
      peaks  —— [(t, v)]，已合并同一次追加的多帧
      usable —— False 表示这块区域**不是歌单浮层**（信号像噪声），调用方应放弃精修
    """
    cand = [(t, v) for t, v in rows if v >= min_v and t >= warmup]
    if not cand:
        return [], False
    # 合法性判据 ①：非零帧占比过高 → 整块区域在持续变化，不是孤立追加
    ratio = float(len(cand)) / max(1, len(rows))
    if ratio > NOISE_RATIO_MAX:
        return [], False

    out = []
    for t, v in cand:
        if out and t - out[-1][0] <= gap:
            # 同一次追加被相邻帧各记一次：保留更强的那个
            if v > out[-1][1]:
                out[-1] = (t, v)
        else:
            out.append((t, v))
    # 合法性判据 ②：峰太少则切不出骨架
    if len(out) < MIN_PEAKS:
        return out, False
    return out, True


def frame_files(cid):
    """返回可复用的帧目录与文件名列表。

    优先用本模块自己的目录 .sungref；若为空，回退到早期的 .sunglist
    （早期那批帧是同参数抽的，可直接复用，避免重复下载整支视频）。
    """
    for base in (FRAME_DIR, os.path.join(ROOT, "tools", ".sunglist")):
        d = os.path.join(base, cid)
        if os.path.isdir(d):
            fs = sorted(f for f in os.listdir(d)
                        if f.startswith("g_") and f.endswith(".png"))
            if fs:
                return d, fs
    return os.path.join(FRAME_DIR, cid), []


def analyze(bvid, cid, step=5.0, reuse=False, ffmpeg=None, duration=None, progress=None):
    """主入口：返回 {"keys": [...], "peaks": [[t, v], ...], "frames": n, "reused": bool}

    keys 是歌曲边界（该首歌被登记的瞬间），可直接作为分段切点。
    """
    ff = find_ffmpeg(ffmpeg)
    d, files = frame_files(cid) if reuse else (os.path.join(FRAME_DIR, cid), [])
    reused = bool(files)
    if not files:
        url = video_url(bvid, cid)
        files = grab_frames(ff, url, cid, step, duration=duration, progress=progress)
        d = os.path.join(FRAME_DIR, cid)
    if not files:
        return {"keys": [], "peaks": [], "frames": 0, "reused": False}

    rows = diff_series(d, files, step)
    peaks, usable = pick_peaks(rows)
    if not usable:
        # 这块区域不是歌单浮层（信号像噪声）。返回空 keys，让 auto_segments
        # 退回纯音频结果 —— 宁可边界粗一点，也不要用噪声切出一堆碎段。
        return {"keys": [], "peaks": [], "frames": len(files), "reused": reused,
                "reason": "信号不符合歌单浮层特征（非零帧占比 %.2f%%，峰数 %d）"
                          % (100.0 * len([1 for _, v in rows if v >= PEAK_MIN])
                             / max(1, len(rows)), len(peaks))}
    # 帧 i 的时间基（fps 滤镜给的第 i 帧 ≈ i*step）：
    # 5 秒步长实测偏差 ≤ 5 秒，作为「跳转到某首歌」的落点完全够用；不在这里补偿。
    keys = [int(round(t)) for t, _ in peaks]
    return {"keys": keys, "peaks": [[int(round(t)), round(v, 4)] for t, v in peaks],
            "frames": len(files), "reused": reused}


def detect_keys(bvid, cid, duration=None, step=5.0, ffmpeg=None, progress=None):
    """供 auto_segments.py 调用的薄封装。

    失败或信号不可用时返回 []（调用方回退到纯音频结果），不抛异常 ——
    抽帧依赖网络与视频源，不该因为一次取流失败就让整批分段任务中断。
    """
    try:
        r = analyze(bvid, cid, step=step, reuse=True, ffmpeg=ffmpeg,
                    duration=duration, progress=progress)
        if r.get("reason"):
            sys.stderr.write("  [seg_refine] cid %s 不使用「已唱」边界：%s\n"
                             % (cid, r["reason"]))
        return r["keys"]
    except Exception as e:
        sys.stderr.write("  [seg_refine] cid %s 取边界失败，回退音频结果：%s\n"
                         % (cid, e))
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--cid", required=True)
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--reuse", action="store_true", help="复用已抽的帧，不重新下载")
    ap.add_argument("--ffmpeg")
    args = ap.parse_args()

    t0 = time.time()
    r = analyze(args.bvid, args.cid, step=args.step, reuse=args.reuse,
                ffmpeg=args.ffmpeg)
    print("帧数 %d（%s）  耗时 %.1fs"
          % (r["frames"], "复用" if r["reused"] else "新抽", time.time() - t0))
    if r.get("reason"):
        print("不采用：%s" % r["reason"])
        return 1
    print("检出边界 %d 个：" % len(r["keys"]))
    for t, v in r["peaks"]:
        print("  t=%6d s   diff=%.4f" % (t, v))
    return 0 if r["keys"] else 1


if __name__ == "__main__":
    sys.exit(main())
