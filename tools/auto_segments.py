# -*- coding: utf-8 -*-
"""自动标注：从音频响度曲线检测「有内容片段」，生成 data/segments.js

原理（两个条件同时满足才算「有内容」）
    ① 整体响度够高 —— 排除静音与低语
    ② 25-100Hz 相对能量够高 —— 说明有伴奏（贝斯/底鼓），即在唱，而不是纯聊天
    两条判据各自用 Otsu 自适应阈值，避免不同场次增益不同导致失效。
    25-100Hz 是关键：实测唱歌时该比值约 -3~-11 dB，纯说话时 -18~-35 dB，区分度很高。

    判据只决定「哪些秒算有内容」，边界还需要第二步修正（见「边界修正」）：
    逐秒判据在歌曲内部会来回抖动（副歌与主歌的伴奏密度本就不同），
    所以必须把边界**吸附到真正的段落跳变点**上，否则会出现「歌刚进来就跳走」。

用法
    python tools/auto_segments.py --bvid BV16RuU6AEvc     # 单个投稿（含全部分P）
    python tools/auto_segments.py --category 唱歌          # 某分类下全部投稿
    python tools/auto_segments.py --all                   # 全部 42 个（很慢）
选项
    --limit-sec N     每个分P 只分析前 N 秒（调试用）
    --min-seg N       片段最短秒数（默认 45）
    --min-gap N       间隔小于此值则合并（默认 50；歌内低谷约 30s，歌间间隔 80s+）
    --max-seg N       单段最长秒数，超过则在能量最低点切开（默认 720）
    --snap N          边界吸附的搜索半径秒数（默认 90；0 = 关闭吸附）
    --dry-run         只打印，不写 data/segments.js
    --ffmpeg PATH     指定 ffmpeg

说明
    · 音频只在本机下载并即时分析，按 10 分钟分块处理，不长期落盘；产物只有片段表。
    · 已分析过的分P 结果缓存在 tools/.cache/auto_seg.json，重跑会跳过。
    · 这是启发式方法，结果请抽听确认；个别不准的分P 可在页面「标注」里手工修正。
"""
import argparse
import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "tools", ".cache")
WORK = os.path.join(ROOT, "tools", ".audio")
FEAT = os.path.join(ROOT, "tools", ".feat")      # 逐秒音频特征（重跑时省去重新下载）
DATA = os.path.join(ROOT, "data")

# 特征格式版本：改了 chunk_features 里抽哪些量之后必须递增
FEAT_VERSION = "f1"

# 分段流程的版本标记。改动判据 / 边界算法后必须递增 —— 否则旧缓存会被命中，
# 重跑拿到的还是旧结果。v2：开始用「已唱」浮层峰值重建歌曲边界。
CACHE_VERSION = "s3"      # s3：长段按「说话型空档」再切（见 split_by_talk）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CHUNK = 600          # 每次处理的音频块长（秒）
CHUNK_GAP = 0.4      # 请求间隔（串行时用；并发时靠并发数控制压力）
# 同时下载/分析的音频块数。下载与 ffmpeg 解码都是「等 I/O」，串行等于把时间全花在等，
# 实测并发能显著缩短整个音频阶段（见 process_part 里的注释）。
CHUNK_WORKERS = 4

# 打包版是 --noconsole，而 ffmpeg 是控制台程序 —— 每次调用都会新建一个控制台，
# 识别时就会不停弹出黑窗。CREATE_NO_WINDOW 让它彻底不出现（POSIX 上是 0，无副作用）。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _hide_startupinfo():
    """Windows 上的双保险：CREATE_NO_WINDOW 让子进程根本没有控制台，
    STARTF_USESHOWWINDOW + SW_HIDE 兜住「万一还是创建了一个窗口」的情况。
    POSIX 上返回 None（subprocess 接受 None）。"""
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0          # SW_HIDE
    return si


HIDE_SI = _hide_startupinfo()


def find_ffmpeg(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("FFMPEG")
    if env and os.path.exists(env):
        return env
    w = shutil.which("ffmpeg")
    if w:
        return w
    for p in (
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser("~/.workbuddy-ai/binaries/ffmpeg/ffmpeg.exe"),
    ):
        if os.path.exists(p):
            return p
    # 兜底：在常见目录里搜一次
    for base in (os.path.expandvars(r"%LOCALAPPDATA%\Packages"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs")):
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            if "ffmpeg.exe" in files:
                return os.path.join(dirpath, "ffmpeg.exe")
    return None


# 网页端可以把日志接进自己的作业面板（tools/serve.py 会装一个 sink）；
# 不装时就是普通的 print —— 命令行的行为一个字都没变。
LOG_SINK = [None]


def log(msg):
    sink = LOG_SINK[0]
    if sink is None:
        print(msg, flush=True)
    else:
        sink(msg)


# 网页端把进度接进 /api/segments/status（tools/serve.py 装 sink），不装就什么都不做。
# 上报的是「真的分析到第几秒」，不是估算 —— 落地在下面 process_part 的分块循环里。
PROGRESS_SINK = [None]


def report_progress(**kw):
    sink = PROGRESS_SINK[0]
    if sink is not None:
        sink(kw)


def get_json(url, referer="https://www.bilibili.com"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def load_programs():
    with open(os.path.join(DATA, "programs.json"), encoding="utf-8") as f:
        return json.load(f)


def audio_url(bvid, cid):
    d = get_json("https://api.bilibili.com/x/player/playurl"
                 "?bvid=%s&cid=%s&fnval=16&fourk=1" % (bvid, cid),
                 "https://www.bilibili.com/video/%s" % bvid)
    if d.get("code") != 0:
        raise RuntimeError("playurl code=%s" % d.get("code"))
    audios = (d.get("data") or {}).get("dash", {}).get("audio") or []
    if not audios:
        raise RuntimeError("无音频流")
    audios.sort(key=lambda a: a["bandwidth"])
    return audios[0]["baseUrl"]


# ---------------------------------------------------------------- 特征

def run_ff(ff, args, timeout=600, cwd=None):
    return subprocess.run([ff, "-hide_banner", "-loglevel", "error"] + args,
                          capture_output=True, timeout=timeout, cwd=cwd,
                          creationflags=NO_WINDOW, startupinfo=HIDE_SI)


def chunk_features(ff, url, start, dur, wavname, cwd=None, feat_name=None):
    """取 [start, start+dur) 的音频并算出逐秒特征。结果按块落盘缓存。

    cwd 是临时目录：并发时每个块必须用自己的目录（wav 与 ffmpeg 的输出文件名固定，
    共用一个目录会互相覆盖）。缓存键只跟 cid + start 有关，不受它影响。

    wavname 是**固定的**（chunk.wav，见 process_part）：文件名里带 cid 的话，
    每分析一个分P 就多留一个几十 MB 的 wav，跑几十期就是好几个 G；
    而本机的删除保护会让 os.remove 失败，清不掉。固定名 + 每 slot 一个目录 = 覆盖写，
    占用恒定。特征缓存不能跟着固定 —— 它按 cid 区分，所以用 feat_name 单独传进来。

    特征（每秒一组，因为 asetnsamples=n=16000 配 16kHz 正好一秒一块）：
        rms_full    整体响度
        bass_ratio  25-100Hz 相对能量（有伴奏 → 在唱）
        crest       峰值因子（混音密度：伴奏进出会明显抬升）
        zcr         过零率（音色亮度：人声/钢琴 vs 全编制）
    后两个只在「边界修正」里用，用来找歌曲真正的起止点。

    缓存的意义：这一步是整个流程里最贵的一环（每块要下载 600 秒音频再跑 4 遍
    ffmpeg）。一台机器上一次完整重跑要几小时，而特征本身只跟音频有关、跟分段
    参数无关 —— 把它缓存下来，改判据 / 重跑其它分P 就只花几分钟。

    注意：ffmpeg 滤镜参数里 file= 的值不能带盘符（`:` 会被当成选项分隔符），
    所以统一用相对文件名并在工作目录下执行。
    """
    cwd = cwd or WORK
    # 特征缓存按 cid 命名（feat_name），不跟着 wav 的固定名走 —— 否则不同分P 会互相命中
    fc = os.path.join(FEAT, "%s_%d_%s.json"
                      % (feat_name or wavname[:-4], start, FEAT_VERSION))
    if os.path.exists(fc):
        try:
            with open(fc, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("dur") == dur and cached.get("rows"):
                return cached["rows"]
        except Exception:
            pass

    hdr = "Referer: https://www.bilibili.com\r\nUser-Agent: %s\r\n" % UA
    r = run_ff(ff, ["-headers", hdr, "-ss", str(start), "-t", str(dur),
                    "-i", url, "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", "-y", wavname], cwd=cwd)
    if not os.path.exists(os.path.join(cwd, wavname)) \
            or os.path.getsize(os.path.join(cwd, wavname)) < 1000:
        raise RuntimeError("下载失败：%s" % r.stderr.decode("utf-8", "replace")[-200:])

    def band(name, filt, key, parse=None):
        out = "b_%s.txt" % name
        run_ff(ff, ["-i", wavname, "-af",
                    ("%sasetnsamples=n=16000,astats=metadata=1:reset=1:length=1,"
                     "ametadata=print:key=%s:file=%s:direct=1"
                     % (filt, key, out)),
                    "-f", "null", "-"], cwd=cwd)
        vals = []
        with open(os.path.join(cwd, out), encoding="utf-8", errors="replace") as f:
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

    full = band("full", "", K_RMS)
    # 25-100Hz：贝斯与底鼓所在频段。唱歌时有伴奏，这一段能量相对全频明显抬高；
    # 纯说话时人声基频在 100Hz 以上，这一段会掉下去 → 用来判断「有没有伴奏」。
    bass = band("bass", "highpass=f=25,lowpass=f=100,", K_RMS)
    crest = band("crest", "", K_CREST)
    zcr = band("zcr", "", K_ZCR)

    n = min(len(full), len(bass), len(crest), len(zcr))
    rows = []
    for i in range(n):
        fv = full[i]
        rows.append({
            "t": start + i,
            "rms_full": -80.0 if fv is None else fv,
            "bass_ratio": ((bass[i] - fv) if (bass[i] is not None
                                              and fv is not None and fv > -70) else -99.0),
            "crest": crest[i],
            "zcr": zcr[i],
        })
    save_feat(fc, dur, rows)
    return rows


def save_feat(path, dur, rows):
    """最佳努力落盘：写失败（磁盘满 / 被杀）只丢缓存，绝不影响本次结果。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"dur": dur, "rows": rows}, f)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------- 检测

def smooth(x, w):
    half = w // 2
    return [sum(x[max(0, i - half):min(len(x), i + half + 1)])
            / (min(len(x), i + half + 1) - max(0, i - half)) for i in range(len(x))]


def otsu(vals, bins=64):
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-6:
        return hi
    step = (hi - lo) / bins
    hist = [0] * bins
    for v in vals:
        k = min(bins - 1, max(0, int((v - lo) / step)))
        hist[k] += 1
    total = len(vals)
    sum_all = sum(((i + 0.5) * step + lo) * hist[i] for i in range(bins))
    sum_b = 0.0
    w_b = 0
    best, best_t = -1.0, hi
    for i in range(bins):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += ((i + 0.5) * step + lo) * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, best_t = var, (i + 1) * step + lo
    return best_t


def split_long(segs, rows, max_seg):
    """超长段在内部「伴奏最弱处」切开。

    有些场次全程垫着背景音乐，两条判据都不会掉下去，会连成一大块（实测出现过 70 分钟的单段），
    那样就失去跳转意义了。这里给段长设一个上限。

    切点优先选在 max_seg 位置附近的**伴奏最弱点**（多半是换气/间奏），
    而不是「响度 + 伴奏」之和最小处 —— 后者容易落在人声很小的尾奏上，
    结果是把一首歌的尾巴切成独立的段。
    """
    out = []
    for a, b in segs:
        if b - a <= max_seg:
            out.append([a, b])
            continue
        k = int((b - a + max_seg - 1) // max_seg)      # 需要切成几块
        cuts = []
        for i in range(1, k):
            target = a + (b - a) * i / k
            best = None
            for r in rows:
                if abs(r["t"] - target) > 90:
                    continue
                # 以伴奏相对能量为主判据；rms 只在同等伴奏时做微调
                score = r["bass_ratio"] * 1.0 + r["rms_full"] * 0.15
                if best is None or score < best[1]:
                    best = (r["t"], score)
            if best is not None:
                cuts.append(best[0])
        cuts = sorted(set(cuts))
        prev = a
        for c in cuts:
            if c - prev >= 60:
                out.append([prev, c])
                prev = c
        out.append([prev, b])
    return out


# ------------------------------------------------- 长段里「几首连成一段」
# 一段动辄 10 分钟往上，多半是几首歌之间主播报幕/聊了两句，而那几句话
# 不够长（< min_gap = 50 秒），被合并进同一段。这对「跳过空白」没影响，
# 但章节列表会变成「约 3 首」这种糊涂账 —— 用户点进去还是得自己拖。
#
# 判据是「说话型空档」：人声响（rms 过阈值）但**没有伴奏**（25-100Hz 低于阈值）。
#   歌与歌之间  = 主播说话（人声响 + 无伴奏）
#   歌内部换气  = 整体安静（人声也不响）
# 两者在双判据里都是 mask=False，性质却相反，所以要分开处理。
#
# **只切长段**（> LONG_SEG）：全局放宽 min_gap 确实能多切出段，但实测短段
# （< 2 分钟）会从 20 个涨到 100 个 —— 把歌切碎比不切更糟。只动长段时，
# 短段数量一个都不变。
LONG_SEG = 480          # 超过 8 分钟的段才考虑再切
PIECE_MIN = 180         # 切出来的每块至少 3 分钟
TALK_MIN = 8            # 「说话型空档」至少持续这么久
TALK_RATIO = 0.5        # 空档里至少一半的秒数是「人声响但没伴奏」


def talk_gaps(rows, a, b, thr, bthr, min_len=TALK_MIN, ratio=TALK_RATIO):
    """在 [a,b) 内找「说话型空档」，返回 [(起点, 终点)]。"""
    out = []
    run = None

    def close(s, e):
        seg = [r for r in rows if s <= r["t"] < e]
        if not seg or e - s < min_len:
            return
        talk = sum(1 for r in seg
                   if r["rms_full"] >= thr and r["bass_ratio"] < bthr)
        if talk / float(len(seg)) >= ratio:
            out.append((s, e))

    for r in rows:
        if not (a <= r["t"] < b):
            continue
        if r["rms_full"] >= thr and r["bass_ratio"] < bthr:
            if run is None:
                run = r["t"]
        else:
            if run is not None:
                close(run, r["t"])
                run = None
    if run is not None:
        close(run, b)
    return out


def split_by_talk(segs, rows, thr, bthr, long_seg=LONG_SEG,
                  min_piece=PIECE_MIN):
    """长段内部按「说话型空档」再切 —— 只动长段，短段一个都不会多出来。

    切点取空档的**中间**：两侧各留一点，别切到歌尾/歌头上去。

    实测（63 个分P，特征缓存离线跑）：长段（>8 分钟）213 → 143 个，
    短段（<2 分钟）20 → 20 个不变；新增切点处「伴奏退出深度」中位 5.6 dB，
    而原有切点只有 0.7 dB —— 新切点确实落在歌与歌之间，不是歌中间。
    """
    out = []
    for a, b in segs:
        if b - a <= long_seg:
            out.append([a, b])
            continue
        prev = a
        for s, e in talk_gaps(rows, a, b, thr, bthr):
            c = (s + e) // 2
            if c - prev >= min_piece and b - c >= min_piece:
                out.append([prev, c])
                prev = c
        out.append([prev, b])
    return out


# ---------------------------------------------------------------- 边界修正

# 为什么需要「边界修正」这一步
#   逐秒双判据只能回答「这一秒有没有内容」，**不负责把边界放在歌的起止处**。
#   实测「戒烟」一段（cid 42117366217，人工读帧的真实起止约 557–1012 秒）：
#     · 判据在歌内部来回抖 —— 主歌伴奏弱、副歌伴奏强，同一首歌里 mask 会 5~10 秒翻转一次；
#     · 靠 min_gap 合并出来的边界因此落不到歌的起止点上：原算法给的是 578–923，
#       歌头被切 21 秒、歌尾被切近 90 秒，听感就是「歌刚进来就跳走」。
#
#   修正分两步，都只用已经取到的逐秒曲线，不额外解码：
#     ① 外扩：从段的头/尾向外走，只要还没进入「持续静音」就继续吞。
#        歌曲的前奏（只有钢琴/人声）与尾奏（渐弱）响度低，但**不是静音**，
#        这一步专门把它们收回来 —— 这是「头尾被切」的主因。
#     ② 吸附：在外扩后的边界附近，找「伴奏（25-100Hz）持续进入/退出」的位置，
#        把边界对齐到伴奏的进出点。伴奏进出才是歌曲真正的起止标志。
#
# 实测参数（本分P）：静音判据 -55 dB 连续 3 秒；伴奏进出要求前后各 8 秒的
# 相对能量差 ≥ 4 dB。

SILENCE_DB = -55.0        # 低于此响度视为「静音」
SILENCE_RUN = 3           # 连续这么多秒才算「持续静音」，避免喘气被当间隔
PAD_RADIUS = 120          # 外扩最多走这么多秒
BASS_JUMP = 4.0           # 伴奏进出的最小相对能量差（dB）
BASS_WIN = 8              # 判断伴奏进出时取前后各多少秒


def _med(xs):
    ys = sorted(v for v in xs if v is not None)
    if not ys:
        return None
    n = len(ys)
    return ys[n // 2] if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2.0


def silence_zones(rows):
    """返回「持续静音」的区间列表 [(a, b)]。

    严格一点：要求连续 SILENCE_RUN 秒都低于 SILENCE_DB。
    喘气、念白间的停顿只有 1~2 秒，不会被算成静音，所以外扩不会跨过它们。
    """
    zones = []
    run = None
    for r in rows:
        quiet = r["rms_full"] < SILENCE_DB
        if quiet:
            if run is None:
                run = r["t"]
        else:
            if run is not None:
                if r["t"] - run >= SILENCE_RUN:
                    zones.append((run, r["t"]))
                run = None
    if run is not None and rows[-1]["t"] + 1 - run >= SILENCE_RUN:
        zones.append((run, rows[-1]["t"] + 1))
    return zones


def bass_steps(rows):
    """找「伴奏持续进入 / 退出」的时刻。

    伴奏（贝斯 + 底鼓）是歌曲与前奏/尾奏/说话的分界：
      · 进入：前 8 秒相对能量低、后 8 秒高 → 这里是歌真正开始的地方；
      · 退出：反过来 → 歌结束的地方。
    返回 (进入点列表, 退出点列表)。
    """
    ins, outs = [], []
    n = len(rows)
    for i in range(BASS_WIN, n - BASS_WIN):
        before = _med([rows[j]["bass_ratio"] for j in range(i - BASS_WIN, i)])
        after = _med([rows[j]["bass_ratio"] for j in range(i, i + BASS_WIN)])
        if before is None or after is None:
            continue
        # -99 是「这一秒整体太轻，比值无意义」的哨兵值，不参与
        if before <= -95 or after <= -95:
            continue
        d = after - before
        if d >= BASS_JUMP:
            ins.append(rows[i]["t"])
        elif d <= -BASS_JUMP:
            outs.append(rows[i]["t"])
    return _dedup(ins), _dedup(outs)


def _dedup(ts, gap=15):
    """把同一段台阶上的连续点压成一个代表点（取最靠中间的那个）。"""
    if not ts:
        return []
    out = [ts[0]]
    for t in ts[1:]:
        if t - out[-1] < gap:
            out[-1] = (out[-1] + t) // 2
        else:
            out.append(t)
    return out


def pad_boundaries(segs, rows, radius=PAD_RADIUS, min_seg=45.0):
    """把每个段的头/尾向外扩展到「持续静音」为止。

    只吞内容、不吞静音：遇到持续静音就停，所以不会把两首歌之间的真正空白也收进来。
    这是修复「头尾被切」的主要手段 —— 前奏与尾奏响度低但并非静音，正是被原算法切掉的部分。
    """
    if radius <= 0:
        return segs, 0
    sil = silence_zones(rows)
    tmin = rows[0]["t"] if rows else 0
    tmax = (rows[-1]["t"] + 1) if rows else 0

    def blocked(lo, hi):
        """[lo, hi) 内是否有持续静音"""
        return any(not (ze <= lo or zs >= hi) for zs, ze in sil)

    out = []
    for a, b in segs:
        na, nb = a, b
        # 头：向外走，每一步都要求「这一段里没有静音」
        step = 1
        while na - step >= max(tmin, a - radius):
            if blocked(na - step, na):
                break
            na -= 1
        # 尾：同理
        step = 1
        while nb + step <= min(tmax, b + radius):
            if blocked(nb, nb + step):
                break
            nb += 1
        if nb - na < min_seg:
            na, nb = a, b
        out.append([na, nb])

    # 外扩后可能重叠：按中点切开
    out.sort(key=lambda x: x[0])
    fixed = []
    for a, b in out:
        if fixed and a < fixed[-1][1]:
            mid = (a + fixed[-1][1]) // 2
            if mid - fixed[-1][0] >= min_seg and b - mid >= min_seg:
                fixed[-1][1] = mid
                a = mid
            else:
                a = fixed[-1][1]
        fixed.append([a, b])

    changed = sum(1 for (a, b), (x, y) in zip(segs, fixed)
                  if abs(a - x) > 3 or abs(b - y) > 3)
    return fixed, changed


def snap_boundaries(segs, rows, radius=45, min_seg=45.0):
    """把段首/段尾吸附到最近的「伴奏持续进入 / 退出」点。

    伴奏进出是歌曲起止最可靠的音频标志（前奏/尾奏没有底鼓与贝斯，
    说话段也没有）。外扩之后做这一步，是为了把边界从「差不多」收成「贴着歌」。
    """
    if radius <= 0 or not segs:
        return segs, 0
    ins, outs = bass_steps(rows)
    if not ins and not outs:
        return segs, 0
    out = []
    for a, b in segs:
        na, nb = a, b
        # 段首吸附到最近的「伴奏进入」点
        for t in ins:
            if abs(t - a) <= radius and abs(t - a) < abs(na - a) + 1:
                na = t
        # 段尾吸附到最近的「伴奏退出」点
        for t in outs:
            if abs(t - b) <= radius and abs(t - b) < abs(nb - b) + 1:
                nb = t
        if nb - na < min_seg:
            na, nb = a, b
        out.append([na, nb])

    out.sort(key=lambda x: x[0])
    fixed = []
    for a, b in out:
        if fixed and a < fixed[-1][1]:
            mid = (a + fixed[-1][1]) // 2
            if mid - fixed[-1][0] >= min_seg and b - mid >= min_seg:
                fixed[-1][1] = mid
                a = mid
            else:
                a = fixed[-1][1]
        fixed.append([a, b])

    changed = sum(1 for (a, b), (x, y) in zip(segs, fixed)
                  if abs(a - x) > 3 or abs(b - y) > 3)
    return fixed, changed


def refine_by_bass(segs, rows, min_seg=45.0, max_seg=720.0):
    """在「全程垫着伴奏」的段落内部，用伴奏进出点进一步细分。

    为什么需要这一步：
        有些场次（尤其唱歌回）从开场到结束一直有背景音乐，25-100Hz 判据永远为真，
        双判据于是把整场连成几个大块 —— 实测一个 19 首歌的分P 只切出 4 段，
        每段跨四五首歌，跳转功能等于失效。
        而「伴奏（贝斯+底鼓）持续进入 / 退出」是歌曲真正的起止标志：
        同一分P 上它给出了 556 / 1003 / 1022，与人工读帧实测的 557 / 1012 吻合。

    做法
        对每个超长段，取落在区间内的伴奏进入点作为候选切点，
        使切出来的子段不短于 min_seg、不长于 max_seg；
        切完后内部若还有子段超过 max_seg，再交给 split_long 兜底。
    """
    if not segs:
        return segs, 0
    ins, _outs = bass_steps(rows)
    if not ins:
        return segs, 0

    out = []
    changed = 0
    for a, b in segs:
        if b - a <= max_seg:
            out.append([a, b])
            continue
        pts = sorted(t for t in ins if a + min_seg <= t <= b - min_seg)
        if not pts:
            out.append([a, b])
            continue
        # 贪心：从 a 出发，每次都切到「不超过 a+max_seg」的最靠后的伴奏进入点；
        # 若前面还有更近的点能切，也接受（保证段长尽量靠近一首歌）。
        prev = a
        for t in pts + [b]:
            # 只有当「再往下走会超过 max_seg」或这是最后一个候选时才切
            if t - prev > max_seg or t == b:
                # 回退到不超过 max_seg 的最后一个可用切点
                usable = [x for x in pts if prev + min_seg <= x <= prev + max_seg]
                if t == b and (b - prev) <= max_seg:
                    cut = b
                elif usable:
                    cut = usable[-1]
                else:
                    cut = min(t, b)
                if cut - prev >= min_seg:
                    out.append([prev, cut])
                    prev = cut
        if b - prev >= min_seg:
            out.append([prev, b])
        elif out:
            out[-1][1] = max(out[-1][1], b)
        changed += 1

    # 仍超过 max_seg 的，交回 split_long 兜底
    fixed = []
    for a, b in out:
        if b - a > max_seg:
            fixed.extend(split_long([[a, b]], rows, max_seg))
        else:
            fixed.append([a, b])

    # 合并被切得过碎（< min_seg）或次序错乱的
    merged = []
    for a, b in sorted(fixed, key=lambda x: x[0]):
        if merged and (b - a < min_seg or a < merged[-1][1]):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged, changed


def apply_sung_keys(segs, keys, rows, min_seg=45.0, max_seg=720.0):
    """用画面「已唱：…」浮层的峰值重建歌曲边界 —— 这是「避免切掉歌头歌尾」的正解。

    为什么音频做不到（实测，非推测）
        cid 42117366217（「戒烟」真值起点约 557 秒）。音频的伴奏进出判据在同一首歌里
        给出了 18 个点（556 608 627 694 752 830 873 899 955 990 / 583 615 669 865
        902 946 972 1003）——**伴奏曲线无法区分「歌结束」与「歌内间奏」**，
        两者都是伴奏短暂减弱。原算法于是输出 578–923（pad 后 488–957）+
        957–1166，把戒烟（557–1012）从 957 处劈成两半：
        这就是「头尾被切掉」的真实形态。

    而左上角「已唱：<歌名列表>」浮层只在**开始唱一首新歌**时把它登记进去，
    其像素变化与歌曲起点一一对应。实测信噪比：
        5 秒步长 1447 帧：p50~p95 恒为 0.0000，只有 39 个非零帧
        1 秒步长  125 帧：只有 1 个非零帧（t=566，即「戒烟」被登记）
        1 秒步长  130 帧：只有 1 个非零帧（t=998，即下一首被登记）
        同样方法用在画面中部的歌词区会给出 37~51 个假峰 —— 区域选择是成败关键。

    为什么要「重建」而不是「把峰值插进音频段里」
        音频给出的假边界（这里 957）只要还在，戒烟就必被切断。
        所以不能保留音频的分段，必须以峰值为骨架重建：
          · 相邻两个峰值之间 = 一首歌（或一段内容），直接成为一段；
          · 峰值本身多为「主播报幕/静音」处（实测 t=570 是 −38.9 dB、
            t=3490 是 −71.1 dB），切在那里不会伤到任何一首歌；
          · 间隔不足 min_seg 的峰值判为误检，合并（实测 5875/5895 相隔 20 秒，
            是同一首歌被登记两次）；
          · 间隔超过 max_seg 的区间（实测 1595→3035 长达 24 分钟）里
            必然有未登记的内容，用音频结果在内部兜底细分；
          · 首峰之前与末峰之后同样交给音频兜底。
    """
    if not keys:
        return segs, 0
    ks = sorted(set(int(k) for k in keys))
    ded = []
    for k in ks:
        if not ded or k - ded[-1] >= min_seg:
            ded.append(k)
    if len(ded) < 2:
        return segs, 0

    tmax = (rows[-1]["t"] + 1) if rows else ded[-1] + 1

    # 骨架：相邻峰值两两成段
    spine = [[ded[i], ded[i + 1]] for i in range(len(ded) - 1)]

    # 音频结果里落在 [a,b] 内部的切点（用于兜底细分）
    def audio_cuts(a, b):
        return sorted(x for x, _ in segs if a + min_seg <= x <= b - min_seg)

    out = []
    for a, b in spine:
        if b - a > max_seg:
            cuts = audio_cuts(a, b)
            prev = a
            for c in cuts:
                if c - prev >= min_seg and b - c >= min_seg:
                    out.append([prev, c])
                    prev = c
            out.append([prev, b])
        else:
            out.append([a, b])

    # 首峰之前：用音频结果兜底（按 ded[0] 裁剪，否则跨界段会被整体丢掉）
    for x, y in segs:
        if x < ded[0]:
            a, b = x, min(y, ded[0])
            if b - a >= min_seg:
                out.append([a, b])
    # 末峰之后：同理
    for x, y in segs:
        if y > ded[-1]:
            a, b = max(x, ded[-1]), y
            if b - a >= min_seg:
                out.append([a, b])

    # 合并重叠与过碎
    merged = []
    for a, b in sorted(out, key=lambda p: (p[0], p[1])):
        if b - a < min_seg:
            continue
        if merged and a < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged, len(spine)


def detect(rows, min_seg, min_gap, max_seg=720, snap=90, keys=None):
    """两个条件同时满足才算「有内容」：
        ① 整体够响（排除静音与低语）
        ② 25-100Hz 相对能量够高（说明有伴奏 → 在唱，而不是纯聊天）
    两条判据各自用 Otsu 自适应阈值，避免不同场次增益不同导致失效。

    之后有三层边界处理：
        pad + snap        → 先做音频层的边界修正，得到一份「兜底分段」
        apply_sung_keys   → 若拿到「已唱」浮层峰值，**以峰值为骨架重建分段**（最优）
                            （拿不到时沿用兜底分段，行为与旧版一致）
    顺序很重要：骨架重建放在最后，因为它要覆盖（而不是补丁）音频的假边界。
    """
    if len(rows) < 30:
        return [], None, None, 0
    f = [r["rms_full"] for r in rows]
    br = [r["bass_ratio"] for r in rows]
    sm = smooth(f, 11)
    smb = smooth(br, 11)

    vals = [v for v in sm if v > -70]
    if len(vals) < 20:
        return [], None, None, 0
    thr = otsu(vals)

    bvals = [v for v in smb if v > -90]
    bthr = otsu(bvals) if len(bvals) >= 20 else -90.0

    mask = [(sm[i] > thr and smb[i] > bthr) for i in range(len(rows))]

    runs = []
    i = 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j + 1 < len(mask) and mask[j + 1]:
                j += 1
            runs.append([rows[i]["t"], rows[j]["t"] + 1])
            i = j + 1
        else:
            i += 1

    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < min_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    kept = [r for r in merged if r[1] - r[0] >= min_seg]

    changed = 0
    if snap > 0:
        kept, n0 = refine_by_bass(kept, rows, min_seg=min_seg, max_seg=max_seg)
        changed += n0
    split = split_long(kept, rows, max_seg)

    if snap > 0:
        split, n1 = pad_boundaries(split, rows, radius=snap, min_seg=min_seg)
        split, n2 = snap_boundaries(split, rows, radius=max(30, snap // 2),
                                    min_seg=min_seg)
        changed = max(changed, n1, n2)

    # 长段里「几首歌连成一段」的，按说话型空档再切一次。
    # 放在浮层骨架**之前**：有「已唱」浮层时边界以峰值为准，这里多出来的切点
    # 只通过 audio_cuts 参与「超长骨架段内部兜底」，不会盖掉峰值骨架。
    if split:
        split = split_by_talk(split, rows, thr, bthr)

    # 有「已唱」浮层峰值时，以峰值为骨架重建（覆盖上面音频得到的边界）。
    # 这是「避免切掉歌头歌尾」的关键一步：音频的假边界会被整体替换掉。
    if keys:
        split, n3 = apply_sung_keys(split, keys, rows, min_seg=min_seg,
                                   max_seg=max_seg)
        changed = max(changed, n3)
    return split, thr, bthr, changed


# ---------------------------------------------------------------- 主流程

def clean_work(path):
    """尽力清理临时 wav；删不掉只记日志，绝不影响退出码。"""
    try:
        os.remove(path)
    except Exception:
        pass
    if os.path.exists(path):
        log("    （临时文件未能删除，可手动清理：%s）" % path)


def cache_key(cid, min_seg, min_gap, snap):
    """缓存键必须带上全部影响结果的参数，否则改了参数还会命中旧结果。

    `CACHE_VERSION` 是**流程版本标记**：这一版开始用「已唱」浮层峰值重建边界，
    与旧版的音频结果不同，必须让旧缓存全部失效（见 main() 里的 stale 清理）。
    """
    return "%s|%g|%g|%g|%s" % (cid, min_seg, min_gap, snap, CACHE_VERSION)


def process_part(ff, bvid, part, args, cache):
    cid = str(part["cid"])
    key = cache_key(cid, args.min_seg, args.min_gap, args.snap)
    if key in cache and not args.limit_sec:
        return cache[key], True

    total = int(part["duration"])
    if args.limit_sec:
        total = min(total, args.limit_sec)
    # 工作量口径：音频分析 1 份，「已唱」精修再 1 份（截断运行 / 无精修依赖时只有 1 份）。
    # analyzed/total 直接就是总进度，两个阶段之间只增不减，不会出现 100% → 50% 的回跳。
    phases = 2 if (args.sung and not args.limit_sec) else 1
    report_progress(cid=cid, analyzed=0, total=total * phases, audio_total=total,
                    phases=phases, refine_ratio=0.0, stage="下载音频")
    rows = []
    ok_sec = 0
    jobs = [(start, min(CHUNK, total - start)) for start in range(0, total, CHUNK)]
    workers = max(1, min(CHUNK_WORKERS, len(jobs)))

    # 固定数量的工作目录，用队列分配：wav 与 ffmpeg 的输出文件名是固定的，
    # 并发时不能共用一个目录；而用完就删会撞上本机的删除保护（批量删除会杀进程），
    # 所以改成「固定几个目录、覆盖写、不删除」，磁盘占用恒定。
    slots = queue.Queue()
    for i in range(workers):
        slots.put(i)

    def one_chunk(job):
        start, dur = job
        slot = slots.get()
        try:
            cwd = os.path.join(WORK, "w%d" % slot)
            os.makedirs(cwd, exist_ok=True)
            for attempt in range(3):                # CDN 偶发 5XX，重试
                try:
                    return chunk_features(ff, part["_url"], start, dur,
                                          "chunk.wav", cwd=cwd,
                                          # 特征缓存名沿用旧格式，已跑出来的那批还能命中
                                          feat_name="chunk_%s" % cid)
                except Exception as e:
                    if attempt == 2:
                        log("      ! %d-%ds 失败（已重试 3 次）：%s"
                            % (start, start + dur, str(e)[-120:]))
                    else:
                        time.sleep(2 + attempt * 3)
            return []
        finally:
            slots.put(slot)

    # 并发：每块都要「下载 600 秒音频 + 跑 4 遍 ffmpeg」，绝大部分时间是在等 I/O，
    # 串行等于把等待全部叠加。map 按提交顺序产出，rows 的顺序与原来完全一致。
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got_rows in ex.map(one_chunk, jobs):
            rows.extend(got_rows)
            ok_sec += len(got_rows)
            # 每块处理完就上报一次：块失败的那一块 ok_sec 不涨，进度也就停在原地
            report_progress(cid=cid, analyzed=min(ok_sec, total), total=total * phases,
                            audio_total=total, phases=phases, stage="分析音频")
    if workers == 1:
        time.sleep(CHUNK_GAP)             # 串行时保留原有的请求间隔
    # 顺手收尾：wav 是固定名、下一块会覆盖写，删不掉也无所谓（占用的就是那几个文件）。
    # 不调 clean_work：它删不掉时会打一行日志，而本机删除保护几乎必然让它失败。
    for i in range(workers):
        try:
            os.remove(os.path.join(WORK, "w%d" % i, "chunk.wav"))
        except OSError:
            pass

    # 「已唱」浮层峰值：抽取并差分左上角区域，得到歌边界。
    # 拿不到（无浮层 / 缺依赖 / 网络失败）时返回空，detect 自动退化为纯音频流程。
    # 注意：这一步要下载整支视频，与上面的音频并发一起跑会互相抢带宽 —— 实测两边
    # 都慢好几倍（音频 36s→139s、抽帧 9s→277s），所以必须串行，不要「优化」成并行。
    keys = []
    if args.sung and not args.limit_sec:
        report_progress(cid=cid, phases=phases, audio_total=total,
                        refine_ratio=0.0, stage="读取「已唱」浮层")
        try:
            from seg_refine import detect_keys
            keys = detect_keys(bvid, cid, duration=total, ffmpeg=args.ffmpeg,
                               progress=lambda ratio: report_progress(
                                   cid=cid, phases=phases, audio_total=total,
                                   refine_ratio=max(0.0, min(1.0, ratio)),
                                   stage="读取「已唱」浮层"))
        except ImportError as e:
            log("      ! 缺少依赖（%s），本次只用音频。"
                "如需边界精修：pip install numpy pillow" % e)
        except Exception as e:
            log("      ! 取「已唱」边界失败，回退音频结果：%s" % str(e)[-120:])
        report_progress(cid=cid, phases=phases, audio_total=total,
                        refine_ratio=1.0, stage="读取「已唱」浮层")

    if keys:
        SUNG_KEYS[cid] = [int(k) for k in keys]
        save_sungkeys(cid, keys)          # 下次命中缓存时也能把它写进 sung.js
    segs, thr, bthr, changed = detect(rows, args.min_seg, args.min_gap,
                                      args.max_seg, args.snap, keys=keys)
    result = [[int(a), int(b)] for a, b in segs]
    complete = ok_sec >= total * 0.8
    log("      cid %s：%d/%d 秒 → %d 段（响度阈 %.1f / 伴奏阈 %.1f dB，"
        "边界修正 %d 段，已唱边界 %d 个，合计 %.1f 分钟）%s"
        % (cid, ok_sec, total, len(result), thr or 0, bthr or 0, changed,
           len(keys), sum(b - a for a, b in result) / 60.0,
           "" if complete else "  ⚠ 覆盖不足，不写缓存"))
    report_progress(cid=cid, analyzed=total, total=total * phases,
                    audio_total=total, phases=phases, refine_ratio=1.0,
                    stage="计算边界")
    return result, complete


# ------------------------------------------------------------ 可复用入口
# 下面这几个函数同时给 CLI（main）和网页端（tools/serve.py）使用，
# 保证「网页里点一下」和「命令行跑一次」走的是同一套逻辑。

def ensure_dirs():
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)


# 本次跑出来的「已唱」浮层登记时刻：cid -> [秒, ...]。
# 它们是**自动标注**的依据 —— 主播每开始唱一首歌，左上角浮层就把歌名登记进去，
# 登记时刻与「第几首」一一对应。有了它，章节列表里写的是「第 7 首」而不是
# 按段长估出来的「约 2 首」；本场歌单（data/setlists.js）的项数对得上时，
# 还能直接把歌名填上去。
SUNG_KEYS = {}


def sungkey_path(cid):
    """登记时刻的单分P 缓存。

    为什么要单独存：分段结果命中缓存时整个分P 都会跳过（含抽帧），
    可「第几首」是从画面里读出来的、跟音频分析无关，跳过了就再也写不进 sung.js。
    取一次就落盘，之后命中缓存也能读回来。
    """
    return os.path.join(CACHE, "sungkeys", "%s.json" % cid)


def load_sungkeys(cid):
    try:
        with open(sungkey_path(cid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_sungkeys(cid, keys):
    try:
        os.makedirs(os.path.dirname(sungkey_path(cid)), exist_ok=True)
        with open(sungkey_path(cid), "w", encoding="utf-8") as f:
            json.dump([int(k) for k in keys], f)
    except OSError:
        pass


def write_sungkeys(dry_run=False):
    """把「已唱」登记时刻写进 data/sung.js（与 segments.js 同目录，跟着主播走）。

    只更新本次跑过的分P，其余沿用文件里已有的 —— 命中缓存跳过的分P 本次没重算，
    它们的登记时刻仍然有效，不该被抹掉。
    """
    if not SUNG_KEYS:
        return 0
    path = os.path.join(DATA, "sung.js")
    old = {}
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"window\.SUNGKEYS\s*=\s*(\{.*\})\s*;", f.read(), re.S)
        if m:
            old = json.loads(m.group(1))
    except Exception:
        old = {}
    merged = dict(old)
    merged.update(SUNG_KEYS)
    if dry_run:
        return len(merged)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("/* 画面「已唱」浮层的登记时刻（秒），由 tools/auto_segments.py 生成 */\n")
            f.write("/* 每个时刻 = 主播开始唱下一首歌的画面变化点，序号即「第几首」 */\n")
            f.write("window.SUNGKEYS = ")
            json.dump({k: sorted(set(int(x) for x in v))
                       for k, v in merged.items()}, f, ensure_ascii=False, indent=1)
            f.write(";\n")
        log("已写入 %s（%d 个分P 有登记时刻）" % (os.path.basename(path), len(merged)))
    except OSError as e:
        log("（登记时刻写入失败，不影响分段：%s）" % e)
    return len(merged)


def load_cache(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def default_args(**over):
    """CLI 与网页端共用的参数默认值（网页端不必走 argparse）。"""
    a = argparse.Namespace(limit_sec=0, min_seg=45.0, max_seg=720.0, min_gap=50.0,
                           snap=90.0, sung=True, dry_run=False, ffmpeg=None)
    for k, v in over.items():
        setattr(a, k, v)
    return a


def process_program(ff, p, args, cache, cache_path, logf=log):
    """处理一个投稿的全部分P（命中缓存的分P 跳过）。返回 (已算, 跳过)。"""
    logf("\n[%s] %s（%s）" % (p["category"], p["title"], p["date"]))
    done = skip = 0
    parts = len(p["parts"])
    for idx, part in enumerate(p["parts"]):
        cid = str(part["cid"])
        # 投稿级的进度（第几个分P、标题）在这里报，分P 内部的秒数在 process_part 里报；
        # 工作量口径与 process_part 一致：音频 1 份 + 精修 1 份（无精修时 1 份）。
        phases = 2 if (args.sung and not args.limit_sec) else 1
        report_progress(bvid=p["bvid"], title=p["title"], cid=cid,
                        part_index=idx + 1, parts=parts,
                        analyzed=0, total=int(part["duration"]) * phases,
                        audio_total=int(part["duration"]), phases=phases,
                        refine_ratio=0.0, stage="准备")
        key = cache_key(cid, args.min_seg, args.min_gap, args.snap)
        if key in cache and not args.limit_sec:
            logf("      cid %s：命中缓存，跳过" % cid)
            # 分段跳过了，但「第几首」的登记时刻是独立缓存的，取回来照写 sung.js
            cached_keys = load_sungkeys(cid)
            if cached_keys:
                SUNG_KEYS[cid] = cached_keys
            # 命中缓存也要把进度报满 —— 不然这一分P 的进度条会停在「准备」不动
            report_progress(bvid=p["bvid"], title=p["title"], cid=cid,
                            part_index=idx + 1, parts=parts,
                            analyzed=int(part["duration"]),
                            total=int(part["duration"]),
                            audio_total=int(part["duration"]), phases=1,
                            refine_ratio=1.0, stage="命中缓存，跳过")
            skip += 1
            continue
        try:
            part["_url"] = audio_url(p["bvid"], part["cid"])
        except Exception as e:
            logf("      ! 取音频地址失败：%s" % e)
            continue
        try:
            segs, complete = process_part(ff, p["bvid"], part, args, cache)
            # 只缓存「有结果且覆盖充分」的分P：空结果或覆盖不足可能来自下载失败，
            # 缓存了就不会再重试；调试用的截断结果同样不写缓存
            if not args.limit_sec and segs and complete:
                cache[key] = segs
                save_cache(cache_path, cache)
                done += 1
            elif not complete:
                logf("      （覆盖不足，未写入缓存，下次会重试）")
        except Exception as e:
            logf("      ! 处理失败：%s" % e)
    return done, skip


def load_existing_segments(path):
    """读现有 segments.js → {cid: [(start, end), ...]}，用于与本次结果合并。"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return {}
    m = re.search(r"window\.SEGMENTS\s*=\s*(\{.*\})\s*;", txt, re.S)
    if not m:
        return {}
    try:
        raw = json.loads(m.group(1))
    except Exception:
        return {}
    out = {}
    for cid, segs in raw.items():
        try:
            out[cid] = [(s["start"], s["end"]) for s in segs]
        except Exception:
            continue
    return out


def write_segments(cache, dry_run=False):
    """把缓存归并后写 data/segments.js（写前备份上一版）。返回分P 数。"""
    # 按 cid 归并：同一个 cid 可能同时有「当前版本」与「旧版本」两条记录，
    # 取当前版本的；没有当前版本的 cid 沿用旧版本（渐进升级，不丢未重跑的分P）。
    cur = {}
    old = {}
    for k, segs in cache.items():
        if not segs:
            continue
        parts = k.split("|")
        cid = parts[0]
        if len(parts) >= 5 and parts[4] == CACHE_VERSION:
            cur[cid] = segs
        else:
            old[cid] = segs
    out = dict(old)
    out.update(cur)
    log("\n共 %d 个分P 有片段（当前版本 %d 个，沿用旧版本 %d 个）"
        % (len(out), len(cur), len([c for c in out if c not in cur])))
    if dry_run:
        log("--dry-run：未写入文件")
        return len(out)

    path = os.path.join(DATA, "segments.js")
    # 与现有文件合并：打包版第一次跑时本地缓存是空的，只写缓存会把已有结果整份抹掉
    # （实测 46 → 1）。已有 cid 保留，本次算出来的覆盖它。
    merged = load_existing_segments(path)
    kept = len(merged)
    merged.update(out)
    if len(merged) != len(out):
        log("合并现有结果：原有 %d 个分P，本次新增/更新 %d 个，合计 %d 个"
            % (kept, len(merged) - kept, len(merged)))

    # 写前留一份上一版：分段结果重算成本很高（要重新下载音频），
    # 万一这次结果异常（例如某个分P 覆盖不足），还能整份回退。
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = f.read()
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(prev)
            log("已备份上一版到 %s.bak" % os.path.basename(path))
        except Exception as e:
            log("（备份失败，不影响写入：%s）" % e)
    with open(path, "w", encoding="utf-8") as f:
        f.write("/* 由 tools/auto_segments.py 自动生成，可用页面「标注」手工修正 */\n")
        f.write("/* 每一版都会把上一版备份到 segments.js.bak */\n")
        f.write("window.SEGMENTS = ")
        json.dump({k: [{"start": a, "end": b, "label": ""} for a, b in v]
                   for k, v in merged.items()}, f, ensure_ascii=False, indent=1)
        f.write(";\n")
    log("已写入 %s（%d 个分P / %d 段）"
        % (path, len(merged), sum(len(v) for v in merged.values())))
    return len(merged)


def process_bvid(bvid, logf=None, ff=None, programs=None, **over):
    """网页端入口：给单个投稿补齐分段并重写 segments.js，返回摘要 dict。

    全程在本进程内完成（不打子进程）—— 打包版没有可用的 python 解释器。
    programs 可以不传（默认读 data/programs.json）；网页端把「实时清单里的那一条」
    传进来，否则刚发布的新回放会因为 programs.json 还没更新而找不到。
    """
    logf = logf or log
    args = default_args(**over)
    ff = ff or find_ffmpeg(args.ffmpeg)
    if not ff:
        return {"ok": False, "error": "未找到 ffmpeg，无法分析音频"}
    ensure_dirs()
    cache_path = os.path.join(CACHE, "auto_seg.json")
    cache = load_cache(cache_path)
    programs = programs if programs is not None else load_programs()["programs"]
    targets = [p for p in programs if p["bvid"] == bvid]
    if not targets:
        return {"ok": False, "error": "节目单里没有 %s" % bvid}
    done = skip = 0
    for p in targets:
        d, s = process_program(ff, p, args, cache, cache_path, logf=logf)
        done += d
        skip += s
    total = write_segments(cache, args.dry_run)
    write_sungkeys(args.dry_run)
    return {"ok": True, "bvid": bvid, "title": targets[0]["title"],
            "processed": done, "skipped": skip,
            "parts": len(targets[0]["parts"]), "segments": total}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid")
    ap.add_argument("--category")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit-sec", type=int, default=0)
    ap.add_argument("--min-seg", type=float, default=45.0)
    ap.add_argument("--max-seg", type=float, default=720.0,
                    help="单段最长秒数，超过则在能量最低点切开（默认 720 = 12 分钟）")
    ap.add_argument("--min-gap", type=float, default=50.0)
    ap.add_argument("--snap", type=float, default=90.0,
                    help="边界吸附搜索半径秒数（默认 90；0 = 关闭边界修正）")
    ap.add_argument("--sung", dest="sung", action="store_true", default=True,
                    help="用画面「已唱」浮层峰值精修歌曲边界（默认开启，推荐）")
    ap.add_argument("--no-sung", dest="sung", action="store_false",
                    help="只用音频，不抽帧读「已唱」浮层")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ffmpeg")
    args = ap.parse_args()

    ff = find_ffmpeg(args.ffmpeg)
    if not ff:
        log("未找到 ffmpeg。请安装后重试，或用 --ffmpeg 指定路径。")
        return 1
    log("ffmpeg: %s" % ff)

    os.makedirs(CACHE, exist_ok=True)
    ensure_dirs()

    cache_path = os.path.join(CACHE, "auto_seg.json")
    cache = load_cache(cache_path)
    ver = CACHE_VERSION
    stale = [k for k in cache if not k.endswith("|" + ver)]
    if stale:
        # 不要删除旧版本结果：只跑了一个 bvid 时，剩下的 59 个分P 若被一并清掉，
        # segments.js 会从 851 段骤降到几段。旧结果留在文件里，写 segments.js 时
        # 按 cid「新版本优先、旧版本兜底」，就能渐进升级而不丢数据。
        log("缓存中有 %d 条旧版本（%s 之前）结果，本次未重跑的分P 将继续沿用它们"
            % (len(stale), ver))

    programs = load_programs()["programs"]
    if args.bvid:
        targets = [p for p in programs if p["bvid"] == args.bvid]
    elif args.category:
        targets = [p for p in programs if p["category"] == args.category]
    elif args.all:
        targets = programs
    else:
        log("请指定 --bvid / --category / --all 之一")
        return 1

    if not targets:
        log("没有匹配的投稿")
        return 1

    log("待处理 %d 个投稿，合计 %.1f 小时"
        % (len(targets), sum(p["duration"] for p in targets) / 3600.0))

    for p in targets:
        process_program(ff, p, args, cache, cache_path)

    write_segments(cache, args.dry_run)
    write_sungkeys(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
