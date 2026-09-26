# -*- coding: utf-8 -*-
"""分段边界诊断：量化「歌曲头尾被切掉」的程度，并给出建议的外扩量。

背景
    tools/auto_segments.py 用双判据（整体响度 + 25-100Hz 相对能量）逐秒打
    「有内容」标签。问题在于歌曲的头尾：前奏常常只有钢琴/人声、没有贝斯与底鼓，
    25-100Hz 就掉下去了；尾奏渐弱同理。于是检测到的段边界会**落在歌内部**，
    用户听到的是「歌刚进来就跳走」「副歌完直接切」。

方法
    逐秒取四个 astats 特征（全部来自同一条 16kHz 单声道流，不额外解码）：
        rms      整体响度           —— 判断这段有没有声
        bass     25-100Hz 相对能量  —— 判断有没有伴奏（在唱）
        crest    峰值因子           —— 混音密度：伴奏进来会明显抬升
        zcr      过零率             —— 音色亮度：人声/钢琴 vs 全编制
        flat     频谱平坦度         —— 噪声性：环境底噪 vs 有调性内容
    再算「相邻 3 秒窗口的特征距离」，在段边界附近找**第一个真正的跳变点**——
    那才是歌的起止。边界两侧特征几乎不连续 → 说明切在歌中间。

输出
    · 每段：头/尾的连续性判定、建议外扩秒数、附近跳变点强度
    · 汇总：有多少段疑似切歌、平均要外扩多少秒
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "tools", ".audio")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# astats 在 16kHz 上配 asetnsamples=n=16000 时，**每个样本块正好 1 秒**
# （pts_time 依次是 0,1,2,…）——所以输出序列天然就是「逐秒」，不需要再重采样。
# 想改分辨率就改这里的 n：n=8000 → 0.5 秒/样本。
SAMPLE_SEC = 1.0
FEATS = [
    ("rms", "lavfi.astats.Overall.RMS_level", "", "median"),
    ("bass", "lavfi.astats.Overall.RMS_level", "highpass=f=25,lowpass=f=100,", "median"),
    ("crest", "lavfi.astats.1.Crest_factor", "", "median"),
    ("zcr", "lavfi.astats.1.Zero_crossings_rate", "", "median"),
    ("flat", "lavfi.astats.1.Flat_factor", "", "median"),
]


def find_ffmpeg(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("FFMPEG")
    if env and os.path.exists(env):
        return env
    w = shutil.which("ffmpeg")
    if w:
        return w
    for p in (os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
              r"C:\ffmpeg\bin\ffmpeg.exe"):
        if os.path.exists(p):
            return p
    for base in (os.path.expandvars(r"%LOCALAPPDATA%\Packages"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs")):
        if os.path.isdir(base):
            for dirpath, _d, files in os.walk(base):
                if "ffmpeg.exe" in files:
                    return os.path.join(dirpath, "ffmpeg.exe")
    return None


def get_json(url, referer=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer or "https://www.bilibili.com",
        "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def audio_url(bvid, cid):
    d = get_json("https://api.bilibili.com/x/player/playurl"
                 "?bvid=%s&cid=%s&fnval=16&fourk=1" % (bvid, cid),
                 "https://www.bilibili.com/video/%s" % bvid)
    a = ((d.get("data") or {}).get("dash") or {}).get("audio") or []
    if not a:
        raise RuntimeError("无音频流")
    a.sort(key=lambda x: x["bandwidth"])
    return a[0]["baseUrl"]


def run(ff, args, cwd=None, timeout=1200):
    return subprocess.run([ff, "-hide_banner", "-loglevel", "error"] + args,
                          capture_output=True, timeout=timeout, cwd=cwd)


def grab(ff, cid, wav, tag, key, filt):
    """取一个特征的逐样本序列。

    注意：file= 的值不能带盘符（`:` 会被当选项分隔符），因此统一在 WORK 下用相对名。
    """
    out = "bd_%s_%s.txt" % (cid, tag)
    path = os.path.join(WORK, out)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
    run(ff, ["-i", wav, "-af",
             ("%sasetnsamples=n=16000,astats=metadata=1:reset=1:length=1,"
              "ametadata=print:key=%s:file=%s:direct=1" % (filt, key, out)),
             "-f", "null", "-"], cwd=WORK)
    vals = []
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.search(re.escape(key) + r"=(-?[\d.n]+|inf|-inf)", line)
            if not m:
                continue
            s = m.group(1)
            if s in ("-inf", "nan", "inf"):
                vals.append(None)
            else:
                vals.append(float(s))
    return vals


def median(xs):
    ys = sorted(x for x in xs if x is not None)
    if not ys:
        return None
    n = len(ys)
    return ys[n // 2] if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2.0


def per_second(vals, seconds):
    """逐样本序列已经是逐秒的（见 SAMPLE_SEC 说明），这里只做长度对齐。

    如果以后把 asetnsamples 的 n 调小（分辨率提高），把此处的 per 改成
    16000 // n 再做中位数聚合即可。
    """
    per = 1
    if per == 1:
        return list(vals[:seconds]) + [None] * max(0, seconds - len(vals))
    out = []
    for s in range(seconds):
        chunk = vals[s * per:(s + 1) * per]
        out.append(median(chunk) if chunk else None)
    return out


def smooth(xs, w=5):
    h = w // 2
    out = []
    for i in range(len(xs)):
        win = [v for v in xs[max(0, i - h):min(len(xs), i + h + 1)] if v is not None]
        out.append(median(win) if win else None)
    return out


def load_segments():
    text = open(os.path.join(ROOT, "data", "segments.js"), encoding="utf-8").read()
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def norm_dist(a, b, scale):
    """单特征距离：按给定尺度归一化，缺值视为 0（不参与判定）"""
    if a is None or b is None:
        return None
    return min(1.0, abs(a - b) / scale)


def transition_profile(feats, seconds, win=3):
    """相邻窗口的特征距离曲线。返回 [(t, score)]，score ∈ [0,1]。

    歌曲起止处：伴奏进出、混音密度突变 → crest/zcr/bass 都会跳。
    说话/换气处：只有响度小波动 → crest/zcr 基本不动。
    所以用多特征联合，而不是只看响度（那才是当前算法切歌的根因）。
    """
    # 各特征的典型幅度，用来把距离归一到 0~1
    SCALE = {"rms": 8.0,           # dB
             "bass": 8.0,          # dB（相对值）
             "crest": 3.0,         # 峰值因子的量级差，取对数后
             "zcr": 0.05,          # 过零率 0~1
             "flat": 0.15}         # 平坦度
    LOG = {"crest"}                # 峰值因子跨几个数量级，先取 log10 再比

    def val(name, i):
        v = feats[name][i]
        if v is None:
            return None
        if name in LOG:
            return None if v <= 0 else (v ** 0.25)      # 压一下动态范围
        return v

    out = []
    for t in range(win, seconds - win):
        dists = []
        for name in ("rms", "bass", "crest", "zcr", "flat"):
            before = [val(name, i) for i in range(t - win, t)]
            after = [val(name, i) for i in range(t, t + win)]
            b, a = median(before), median(after)
            d = norm_dist(b, a, SCALE[name])
            if d is not None:
                dists.append(d)
        if dists:
            out.append((t, sum(dists) / len(dists)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", required=True)
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--dur", type=int, default=0, help="分析时长（默认到最后一个段尾 +120s）")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--extend", type=int, default=90,
                    help="向外搜索跳变点的最大秒数（默认 90）")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    if not ff:
        print("未找到 ffmpeg")
        return 1
    os.makedirs(WORK, exist_ok=True)

    segs = load_segments().get(args.cid, [])
    dur = args.dur or (max(s["end"] for s in segs) + 120 if segs else 3600)
    print("分P %s：%d 段，分析 %d 秒" % (args.cid, len(segs), dur))

    url = audio_url(args.bvid, args.cid)
    wav = "bd_%s.wav" % args.cid
    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA
    r = run(ff, ["-headers", hdr, "-ss", "0", "-t", str(dur),
                 "-i", url, "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", "-y", wav], cwd=WORK)
    if not os.path.exists(os.path.join(WORK, wav)) \
            or os.path.getsize(os.path.join(WORK, wav)) < 1000:
        print("下载失败：%s" % r.stderr.decode("utf-8", "replace")[-300:])
        return 1

    feats = {}
    for tag, key, filt, _agg in FEATS:
        per_sample = grab(ff, args.cid, wav, tag, key, filt)
        secs = per_second(per_sample, dur)
        if tag == "bass":
            # bass 取的是低频带 RMS，要减去全频 RMS 才是有意义的「相对能量」
            full = feats.get("rms")
            if full:
                secs = [(b - f) if (b is not None and f is not None) else None
                        for b, f in zip(secs, full)]
        feats[tag] = secs
        print("  特征 %-6s：%d 秒" % (tag, sum(1 for v in secs if v is not None)))

    for k in feats:
        feats[k] = smooth(feats[k], 5)

    prof = transition_profile(feats, dur)
    pmax = max((s for _t, s in prof), default=1.0) or 1.0
    print("跳变强度：峰值 %.3f，中位 %.3f" % (pmax, median([s for _t, s in prof]) or 0))

    rms = feats["rms"]
    bass = feats["bass"]

    def at(arr, t):
        return arr[t] if 0 <= t < len(arr) else None

    # 找出「强跳变点」，作为候选歌曲边界
    strong = [(t, s) for t, s in prof if s > pmax * 0.55]
    print("强跳变点 %d 个" % len(strong))

    def near_transition(t, direction):
        """在 t 的 direction 方向找最近的强跳变点，返回 (偏移, 强度)"""
        best = None
        for tt, s in strong:
            off = tt - t
            if direction > 0 and not (0 <= off <= args.extend):
                continue
            if direction < 0 and not (-args.extend <= off <= 0):
                continue
            if best is None or abs(off) < abs(best[0]):
                best = (off, s)
        return best

    print("\n%-4s %-7s %-7s | %-22s | %-22s | 结论" %
          ("#", "start", "end", "头（外扩量 / 跳变强度）", "尾（外扩量 / 跳变强度）"))
    clip_head = clip_tail = 0
    head_ext, tail_ext = [], []
    for i, s in enumerate(segs):
        a, b = s["start"], s["end"]
        # 头：段首前 1 秒还在有声（> -52 dB）→ 边界落在内容里
        hbusy = (at(rms, a - 1) or -80) > -52
        tbusy = (at(rms, b) or -80) > -52
        ht = near_transition(a, -1)
        tt = near_transition(b, +1)
        hs = "—" if not ht else "%+4ds / %.2f" % (ht[0], ht[1] / pmax)
        ts = "—" if not tt else "%+4ds / %.2f" % (tt[0], tt[1] / pmax)
        concl = []
        if hbusy:
            clip_head += 1
            concl.append("歌头被切")
            if ht:
                head_ext.append(abs(ht[0]))
        if tbusy:
            clip_tail += 1
            concl.append("歌尾被切")
            if tt:
                tail_ext.append(abs(tt[0]))
        if concl:
            print("%-4d %-7d %-7d | %-22s | %-22s | %s" %
                  (i, a, b, hs, ts, " + ".join(concl)))

    print("\n头被切 %d / %d 段；尾被切 %d / %d 段" %
          (clip_head, len(segs), clip_tail, len(segs)))
    if head_ext:
        print("头建议外扩：中位 %.0fs，均值 %.0fs，最大 %ds"
              % (median(head_ext), sum(head_ext) / len(head_ext), max(head_ext)))
    if tail_ext:
        print("尾建议外扩：中位 %.0fs，均值 %.0fs，最大 %ds"
              % (median(tail_ext), sum(tail_ext) / len(tail_ext), max(tail_ext)))
    print("\n（外扩量来自「边界到最近强跳变点」的距离；"
          "若这个距离很小，说明边界本来就贴着跳变点，不需要动）")

    try:
        os.remove(os.path.join(WORK, wav))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
