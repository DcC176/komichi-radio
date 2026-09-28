# -*- coding: utf-8 -*-
"""二十四时小路电台 · 本地服务（静态文件 + B 站代理）

为什么需要它
    浏览器不能直接播放 B 站的视频流：CDN 校验 Referer（缺了返回 403），
    而网页无法伪造 Referer；B 站接口也不返回 CORS 头，跨域读不到。
    所以由本机这个进程代取接口、代转媒体流，页面就能用原生 <video> 播放并自由切画质。

接口
    GET /api/playurl?bvid=&cid=&qn=   取播放地址与可选清晰度
    GET /api/stream?u=<base64url>     转发媒体流（支持 Range，可拖动进度）
    GET /api/status                   当前登录态与可选清晰度
    GET /api/programs                 实时回放清单（页面加载时抓最新；?refresh=1 强制重抓）
    GET /api/status-board             小路状态：开播情况（右下角弹窗用，每次都取最新）

清晰度
    未登录：最高 480P（接口列出 1080P 档位但只实际下发 360P/480P）。登录后可解锁更高（含 1080P）。
    登录方式：把浏览器里的 SESSDATA 写进 tools/sessdata.txt（一行，只有本机进程会读，
    且只发给 B 站自己的接口，不会外传）。获取方法见 README。

用法
    python tools/serve.py            # 默认 http://127.0.0.1:8765/
    python tools/serve.py --port 9000
"""
import argparse
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

FROZEN = bool(getattr(sys, "frozen", False))    # True = 由 PyInstaller 打成的 EXE

WEB_ITEMS = ("index.html", "favicon.ico", "assets", "data")   # 打进 EXE 的网页文件


def _read_segments_file(path):
    """读 segments.js → {cid: [{"start":..,"end":..}, ...]}；没有/解析失败返回 {}。"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return {}
    m = re.search(r"window\.SEGMENTS\s*=\s*(\{.*\})\s*;", txt, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def _merge_segments_file(path, old):
    """把旧文件里有、新文件里没有的分P 合回去（内置快照覆盖后仍保留本机成果）。"""
    new = _read_segments_file(path)
    added = [cid for cid in old if cid not in new]
    if not added:
        return 0
    for cid in added:
        new[cid] = old[cid]
    with open(path, "w", encoding="utf-8") as f:
        f.write("/* 由 tools/auto_segments.py 自动生成，可用页面「标注」手工修正 */\n")
        f.write("/* 每一版都会把上一版备份到 segments.js.bak */\n")
        f.write("window.SEGMENTS = ")
        json.dump(new, f, ensure_ascii=False, indent=1)
        f.write(";\n")
    return len(added)


def setup_bundled_ffmpeg():
    """打包版自带 FFmpeg：把它指给 auto_segments（find_ffmpeg 优先认 FFMPEG 环境变量）。

    只有一个 EXE 时，用户机器上不会有 ffmpeg，兜底目录遍历也基本搜不到 ——
    所以直接把随包的那份指过去；用户自己设了 FFMPEG 就尊重用户的。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return ""
    p = os.path.join(meipass, "ffmpeg", "ffmpeg.exe")
    if os.path.exists(p):
        if not os.environ.get("FFMPEG"):
            os.environ["FFMPEG"] = p
        return p
    return ""


def unpack_web(appdir):
    """把 EXE 内置的网页文件释放到 appdir\\www，返回该目录；失败返回 None。

    只在「EXE 同目录没有 index.html」时调用 —— 也就是用户只拿到一个 EXE 的场景。
    靠 www_version.txt 判重：版本没变就不重复覆盖，省掉每次启动的拷贝开销。
    """
    src = getattr(sys, "_MEIPASS", None)
    if not src:
        return None
    dst = os.path.join(appdir, "www")
    try:
        with open(os.path.join(src, "www_version.txt"), encoding="utf-8") as f:
            want = f.read().strip()
        try:
            with open(os.path.join(dst, "www_version.txt"), encoding="utf-8") as f:
                have = f.read().strip()
        except OSError:
            have = ""
        # 使用说明始终释放到用户目录根（和凭据、日志同级），保证随时找得到
        rm = os.path.join(src, "readme.txt")
        if os.path.exists(rm):
            shutil.copy2(rm, os.path.join(appdir, "使用说明.txt"))
        # FFmpeg 的许可以及「怎么关掉它」写在单独文件里，随程序一起放出来
        lic = os.path.join(src, "ffmpeg.LICENSE.txt")
        if os.path.exists(lic):
            shutil.copy2(lic, os.path.join(appdir, "FFmpeg许可.txt"))
        if want and want == have and os.path.exists(os.path.join(dst, "index.html")):
            return dst
        os.makedirs(dst, exist_ok=True)
        # 重新释放前先留住用户机上算出来的分段：内置的那份只是发布时的快照，
        # 覆盖会把「新回放自动分段」的成果抹回旧快照（实测过）。
        old_seg = _read_segments_file(os.path.join(dst, "data", "segments.js"))
        for name in WEB_ITEMS:
            s = os.path.join(src, name)
            d = os.path.join(dst, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)   # 覆盖式，不先删目录
            elif os.path.exists(s):
                shutil.copy2(s, d)
        if old_seg:
            _merge_segments_file(os.path.join(dst, "data", "segments.js"), old_seg)
        with open(os.path.join(dst, "www_version.txt"), "w", encoding="utf-8") as f:
            f.write(want)
        return dst
    except OSError as e:
        # 这里静默失败会让「服务照样能用、但文件没落盘」变得无从排查，写进日志留痕。
        try:
            with open(os.path.join(appdir, "log.txt"), "a", encoding="utf-8") as f:
                f.write("释放网页文件失败：%s\n" % e)
        except OSError:
            pass
        return None


if FROZEN:
    # 凭据和日志写用户目录，EXE 放在只读位置（Program Files、只读盘）也能跑。
    APPDIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                          "KomichiRadio")
    try:
        os.makedirs(APPDIR, exist_ok=True)
    except OSError:
        APPDIR = os.path.dirname(os.path.abspath(sys.executable))
    # 网页文件有两种来源，优先外置：
    #   1) EXE 同目录有 index.html —— 外置模式，改前端不用重新打包（开发/定制用）
    #   2) 没有 —— 从 EXE 内置资源释放到 APPDIR\www，分发时只需要给一个 EXE
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
    if not os.path.exists(os.path.join(ROOT, "index.html")):
        ROOT = unpack_web(APPDIR) or os.path.join(getattr(sys, "_MEIPASS", ROOT))
    # 随包自带的 FFmpeg（自动分段要用），在别人机器上也能直接跑
    setup_bundled_ffmpeg()
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    APPDIR = os.path.join(ROOT, "tools")

SESS_FILE = os.path.join(APPDIR, "sessdata.txt")

if FROZEN:
    # --noconsole 打包时没有控制台，PyInstaller 会把 sys.stdout/stderr 换成「丢弃式」
    # 对象（不一定是 None），输出全进黑洞 —— 出问题就完全无从排查。
    # 所以这里无条件改成写日志文件（追加 + 时间戳，便于事后回看）。
    try:
        _logf = open(os.path.join(APPDIR, "log.txt"), "a",
                     encoding="utf-8", buffering=1)
        sys.stdout = _logf
        sys.stderr = _logf
        print("\n===== %s =====" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        # 兜底：这里失败会让后续所有 print 打到「丢弃式」对象上甚至直接崩，
        # 而静默模式崩了用户什么都看不到，所以宁可吞掉。
        pass

    # 首次运行：沿用项目目录里已有的登录凭据，省得重新扫码
    _legacy = os.path.join(ROOT, "tools", "sessdata.txt")
    if not os.path.exists(SESS_FILE) and os.path.exists(_legacy):
        try:
            import shutil
            shutil.copyfile(_legacy, SESS_FILE)
        except OSError:
            SESS_FILE = _legacy

MID = "1512246445"          # 四时小路Komichi
ROOM_ID = "1700301235"      # 直播间真实房间号（用 get_status_info_by_uids 查到）

APP_TAG = "komichi-radio"   # /api/ping 的应答标识：启动时用它认出「已经有一个实例在跑」

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com"

QN_DESC = {
    127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
    112: "1080P 高码率", 80: "1080P 高清", 74: "720P60", 64: "720P",
    32: "480P", 16: "360P",
}


def sessdata():
    """每次请求都重新读，改文件后不用重启"""
    if not os.path.exists(SESS_FILE):
        return ""
    try:
        with open(SESS_FILE, encoding="utf-8") as f:
            v = f.read().strip()
        # 允许整行是 "SESSDATA=xxx" 或只写值
        if "=" in v:
            m = re.search(r"SESSDATA\s*=\s*([^;\s]+)", v)
            if m:
                return m.group(1)
        return v
    except Exception:
        return ""


# urllib 没有 http_proxy 环境变量时会回落到注册表的 IE 代理，所以显式准备一个直连通道。
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(req, timeout):
    """发请求：先直连，直连失败再退回默认 opener（走系统 / 环境代理）。

    顺序不能反：真正的故障场景正是「代理配置还在，但代理进程没运行」——
    2026-09-27 实测本机注册表代理 127.0.0.1:7897 没监听时，图片与播放接口
    连续 4.5 小时全部 502。只信任 urllib 的代理回落是不够的。
    HTTPError 是上游真的回了响应（如 412 风控），不是通道问题，不重试。
    """
    try:
        return DIRECT_OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout)


def bili_get(url, referer=REFERER, timeout=25):
    hdrs = {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "identity",
        "Origin": "https://www.bilibili.com",
    }
    s = sessdata()
    if s:
        hdrs["Cookie"] = "SESSDATA=%s" % s
    req = urllib.request.Request(url, headers=hdrs)
    with urlopen(req, timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def api_playurl(query):
    bvid = query.get("bvid", [""])[0]
    cid = query.get("cid", [""])[0]
    qn = query.get("qn", ["0"])[0]
    if not bvid or not cid:
        return 400, {"error": "缺少 bvid / cid"}
    url = ("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=%s"
           "&fnval=1&fnver=0&fourk=0&platform=pc&high_quality=1"
           % (urllib.parse.quote(bvid), urllib.parse.quote(cid), urllib.parse.quote(qn)))
    try:
        d = bili_get(url, "https://www.bilibili.com/video/%s" % bvid)
    except Exception as e:
        return 502, {"error": "取播放地址失败：%s" % e}

    if d.get("code") != 0:
        return 502, {"error": "B 站返回 code=%s %s" % (d.get("code"), d.get("message"))}

    data = d.get("data") or {}
    durl = (data.get("durl") or [{}])[0]
    media = durl.get("url") or ""
    accept = data.get("accept_quality") or []
    cur = data.get("quality")
    logged = bool(sessdata())

    return 200, {
        "logged": logged,
        "quality": cur,
        "qualityDesc": QN_DESC.get(cur, str(cur)),
        "accept": [{"qn": q, "desc": QN_DESC.get(q, str(q))} for q in accept],
        "media": media,
        "size": durl.get("size"),
        "length": data.get("timelength"),
        "isDurl": bool(durl.get("url")),
    }


def api_status():
    s = sessdata()
    info = {"logged": bool(s)}
    if s:
        try:
            d = bili_get("https://api.bilibili.com/x/web-interface/nav")
            dd = d.get("data") or {}
            info["logged"] = bool(dd.get("isLogin"))
            info["uname"] = dd.get("uname") or ""
            info["vip"] = bool((dd.get("vipStatus") or 0) == 1)
        except Exception as e:
            info["error"] = str(e)
    info["jct"] = bool(load_credentials()[1])
    return 200, info


IMG_CACHE = {}          # 缩略图内存缓存：url → (content_type, bytes)


def api_img(raw):
    """代取缩略图。

    浏览器直连 i2.hdslb.com 在部分网络下会失败，而本机服务能取到 —— 所以让浏览器只跟 127.0.0.1 通信，图片由这里代取并缓存。
    """
    if not raw:
        return 400, None, None
    pad = "=" * (-len(raw) % 4)
    try:
        target = base64.urlsafe_b64decode(raw + pad).decode("utf-8")
    except Exception as e:
        return 400, None, None
    if not target.startswith("http"):
        return 400, None, None

    if target in IMG_CACHE:
        ct, body = IMG_CACHE[target]
        return 200, ct, body

    try:
        req = urllib.request.Request(target, headers={
            "User-Agent": UA, "Referer": REFERER, "Accept": "image/*,*/*",
        })
        with urlopen(req, 20) as r:
            ct = r.headers.get("Content-Type") or "image/jpeg"
            body = r.read()
    except Exception:
        return 502, None, None

    if len(body) < 200 * 1024:      # 只缓存小图，别吃内存
        IMG_CACHE[target] = (ct, body)
    return 200, ct, body


def b64url_encode(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


DASH_CACHE = {}         # (bvid, cid) → (时间戳, dash 数据)
DASH_TTL = 5400         # 90 分钟。播放地址约 2 小时过期，留足余量；同时避免频繁打 B 站接口触发风控


def dash_data(bvid, cid):
    key = (bvid, cid)
    hit = DASH_CACHE.get(key)
    if hit and time.time() - hit[0] < DASH_TTL:
        return hit[1], None
    url = ("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&fnval=16&fourk=1&qn=0"
           % (urllib.parse.quote(bvid), urllib.parse.quote(cid)))
    try:
        d = bili_get(url, "https://www.bilibili.com/video/%s" % bvid)
    except Exception as e:
        # 风控（412）或网络抖动时，宁可先用过期的缓存，也不要直接失败
        if hit:
            return hit[1], "接口暂时不可用，使用缓存数据"
        return None, "取 DASH 失败：%s" % e
    if d.get("code") != 0:
        if hit:
            return hit[1], "B 站返回 code=%s，使用缓存数据" % d.get("code")
        return None, "B 站返回 code=%s" % d.get("code")
    data = d.get("data") or {}
    if not (data.get("dash") or {}).get("video"):
        return None, "该视频没有 DASH 流"
    DASH_CACHE[key] = (time.time(), data)
    return data, None


def pick_tracks(data, qn):
    """选轨：视频优先 H.264（兼容性最好），清晰度不超过请求档；音频取最高码率。"""
    dash = data.get("dash") or {}
    vids = dash.get("video") or []
    auds = dash.get("audio") or []
    cands = [v for v in vids if v.get("codecid") == 7] or vids
    ok = [v for v in cands if v["id"] <= qn]
    v = max(ok or cands, key=lambda x: (x["id"], x["bandwidth"]))
    a = max(auds, key=lambda x: x["bandwidth"]) if auds else None
    return v, a


def parse_int(s, default):
    """把查询参数转成 int。

    前端在还没拿到清晰度列表时会发 qn=undefined，直接 int() 会抛 ValueError
    并把接口打成 500，所以统一走这里兜底。
    """
    try:
        v = int(str(s).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def api_dashinfo(query):
    """返回该分P 的 DASH 清晰度阶梯，供页面填充下拉框。"""
    bvid = query.get("bvid", [""])[0]
    cid = query.get("cid", [""])[0]
    if not bvid or not cid:
        return 400, {"error": "缺少 bvid / cid"}
    data, warn = dash_data(bvid, cid)
    if data is None:
        return 502, {"error": warn}
    dash = data.get("dash") or {}
    ids = sorted({v["id"] for v in dash.get("video") or []}, reverse=True)
    out = {"accept": [{"qn": i, "desc": QN_DESC.get(i, str(i))} for i in ids],
           "logged": bool(sessdata())}
    if warn:
        out["warn"] = warn
    return 200, out


def api_dash(host, query):
    """生成 DASH 的 MPD，供页面用 dash.js 播放。

    为什么要走 DASH：**MP4（durl）通道封顶 720P** —— 实测即便大会员登录也只给 720P/360P；
    1080P（qn=80/112）只存在于 DASH 通道。B 站的 DASH 轨道是「单文件 + SegmentBase 索引」，
    所以可以直接生成 isoff-on-demand 风格的 MPD，由 dash.js 按字节范围取。
    """
    bvid = query.get("bvid", [""])[0]
    cid = query.get("cid", [""])[0]
    qn = parse_int(query.get("qn", ["80"])[0], 80)
    if not bvid or not cid:
        return 400, {"error": "缺少 bvid / cid"}

    data, warn = dash_data(bvid, cid)
    if data is None:
        return 502, {"error": warn}

    dash = data.get("dash") or {}
    v, a = pick_tracks(data, qn)
    # 协议相对 URL：本地是 http、线上反代是 https，写死 http:// 在 HTTPS 下
    # 会被浏览器按混合内容拦掉。
    base = host if host.startswith(("//", "http://", "https://")) else "//" + host
    dur_s = float(dash.get("duration") or (data.get("timelength") or 0) // 1000 or 0)

    def rep(track, kind):
        if not track:
            return ""
        sb = track.get("segment_base") or track.get("SegmentBase") or {}
        init = sb.get("initialization") or sb.get("Initialization") or ""
        idx = sb.get("index_range") or sb.get("indexRange") or ""
        proxied = base + "/api/stream?u=" + b64url_encode(track["baseUrl"])
        w = ""
        if kind == "video":
            w = ' width="%s" height="%s" frameRate="%s"' % (
                track["width"], track["height"], str(track.get("frameRate") or "25"))
        return ('<Representation id="%s" bandwidth="%s" codecs="%s"%s>'
                '<BaseURL>%s</BaseURL>'
                '<SegmentBase indexRange="%s"><Initialization range="%s"/></SegmentBase>'
                '</Representation>' % (track["id"], track["bandwidth"],
                                       track.get("codecs", "avc1.640032"), w, proxied, idx, init))

    mpd = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
           'mediaPresentationDuration="PT%.3fS" minBufferTime="PT1.5S" '
           'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">'
           '<Period>'
           '<AdaptationSet contentType="video" mimeType="video/mp4" '
           'segmentAlignment="true" startWithSAP="1">%s</AdaptationSet>'
           '<AdaptationSet contentType="audio" mimeType="audio/mp4" '
           'segmentAlignment="true" startWithSAP="1">%s</AdaptationSet>'
           '</Period></MPD>') % (dur_s, rep(v, "video"), rep(a, "audio"))

    return 200, mpd, v["id"], QN_DESC.get(v["id"], str(v["id"]))


def api_stream(handler, query):
    raw = query.get("u", [""])[0]
    if not raw:
        return 400, {"error": "缺少 u"}
    pad = "=" * (-len(raw) % 4)
    try:
        target = base64.urlsafe_b64decode(raw + pad).decode("utf-8")
    except Exception as e:
        return 400, {"error": "u 解码失败：%s" % e}
    if not target.startswith("http"):
        return 400, {"error": "非法地址"}

    hdrs = {"User-Agent": UA, "Referer": REFERER, "Accept": "*/*"}
    rng = handler.headers.get("Range")
    if rng:
        hdrs["Range"] = rng
    s = sessdata()
    if s:
        hdrs["Cookie"] = "SESSDATA=%s" % s

    try:
        req = urllib.request.Request(target, headers=hdrs)
        resp = urlopen(req, 40)
    except urllib.error.HTTPError as e:
        # 地址过期（签名带时效）时把状态原样透出，前端会重新取地址
        handler.send_response(e.code)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return None, None
    except Exception as e:
        return 502, {"error": "转发失败：%s" % e}

    try:
        handler.send_response(resp.status)
        ctype = resp.headers.get("Content-Type") or ""
        if (not ctype) or ctype.startswith("application/octet-stream"):
            ctype = "video/mp4"          # 上游给的是通用类型，<video> 需要具体的
        handler.send_header("Content-Type", ctype)
        for k in ("Content-Length", "Content-Range", "Accept-Ranges"):
            v = resp.headers.get(k)
            if v:
                handler.send_header(k, v)
        handler.end_headers()
        while True:
            # 256 KB 一块。64 KB 时代理吞吐成为瓶颈 → 视频加载慢的主因。
            chunk = resp.read(262144)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # 拖进度条 / 关页面时客户端会直接掐断，属正常现象，不该刷一屏 traceback
        # （连响应头都还没发出去就断开的情况也在这里 —— 关页面时很常见）
        pass
    return None, None


def save_sessdata(value):
    save_credential("SESSDATA", value.strip())


# ---------------------------------------------------------------- 直播弹幕

def load_credentials():
    """读凭据文件 → (sessdata, bili_jct)。

    兼容两种格式：老版「整行只有 SESSDATA 值」；现版「每行一个 key=value」。
    发弹幕必须同时有 SESSDATA（身份）和 bili_jct（CSRF），缺一个 B 站都回 -111。
    """
    try:
        with open(SESS_FILE, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "", ""
    sess = jct = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"(SESSDATA|bili_jct)\s*=\s*(\S+)\s*$", line)
        if m:
            if m.group(1) == "SESSDATA":
                sess = m.group(2)
            else:
                jct = m.group(2)
        elif not sess:
            sess = line
    return sess, jct


def save_credential(key, value):
    """写凭据文件并保持另一项不变。文件不出本机目录，行为与 SESSDATA 相同。"""
    if not value or len(value) > 512 or (set(value) & set(" \t\r\n;\"'\\")):
        raise ValueError("凭据格式不合法")
    sess, jct = load_credentials()
    if key == "SESSDATA":
        sess = value
    else:
        jct = value
    os.makedirs(os.path.dirname(SESS_FILE), exist_ok=True)
    with open(SESS_FILE, "w", encoding="utf-8") as f:
        if sess:
            f.write("SESSDATA=%s\n" % sess)
        if jct:
            f.write("bili_jct=%s\n" % jct)


DANMAKU_MAX_LEN = 30        # B 站直播弹幕长度上限，超长 B 站自己也会拒
WHEEL_INTERVAL_MIN = 1.0    # 独轮车最小间隔（秒）：再快就是纯刷屏，只会加速触发风控
WHEEL_COUNT_MAX = 200       # 独轮车单轮条数上限


def _danmaku_ready():
    sess, jct = load_credentials()
    return bool(sess and jct)


def _send_danmaku(msg):
    """发一条弹幕，返回 (B 站 code, message)。code=0 才是成功。"""
    _, jct = load_credentials()
    body = urllib.parse.urlencode({
        "bubble": "0", "msg": msg, "color": "16777215", "mode": "1",
        "fontsize": "25", "roomid": ROOM_ID, "rnd": str(int(time.time())),
        "csrf": jct, "csrf_token": jct,
    }).encode("utf-8")
    sess = load_credentials()[0]
    req = urllib.request.Request("https://api.live.bilibili.com/msg/send", data=body, headers={
        "User-Agent": UA,
        "Referer": "https://live.bilibili.com/%s" % ROOM_ID,
        "Origin": "https://live.bilibili.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": "SESSDATA=%s; bili_jct=%s" % (sess, jct),
    })
    try:
        with urlopen(req, 15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return -1, "网络错误：%s" % e
    return int(d.get("code") or 0), d.get("message") or ""


def api_live_send(obj):
    msg = str(obj.get("msg") or "").strip()
    if not msg:
        return 400, {"error": "弹幕内容不能为空"}
    if len(msg) > DANMAKU_MAX_LEN:
        return 400, {"error": "弹幕最长 %d 个字（当前 %d 个）" % (DANMAKU_MAX_LEN, len(msg))}
    if not _danmaku_ready():
        return 403, {"error": "发弹幕需要登录，并粘贴 bili_jct（直播间页右侧有入口）"}
    code, message = _send_danmaku(msg)
    return (200 if code == 0 else 502), {"code": code, "message": message}


# 独轮车：循环发送同一条弹幕。逻辑放在服务端线程 —— 页面关了也能按计划继续，
# 且频率、上限、报错即停都由服务端强制，前端只是遥控器。
WHEEL_LOCK = threading.Lock()
WHEEL = {
    "running": False, "msg": "", "interval": 3.0, "total": 0, "sent": 0,
    "last_code": None, "last_message": "", "reason": "", "stop": None,
}


def _wheel_worker():
    st = WHEEL
    fails = 0
    try:
        while st["sent"] < st["total"] and not st["stop"].is_set():
            code, message = _send_danmaku(st["msg"])
            st["last_code"], st["last_message"] = code, message
            if code == 0:
                st["sent"] += 1
                fails = 0
            else:
                fails += 1
                if fails >= 3:
                    st["reason"] = "连续 %d 次发送失败（%s），已自动停止" % (fails, message)
                    break
            if st["sent"] >= st["total"]:
                st["reason"] = "已发完 %d 条" % st["total"]
                break
            st["stop"].wait(st["interval"])
    except Exception as e:
        st["reason"] = "独轮车线程异常：%s" % e
    finally:
        st["running"] = False
        print("独轮车结束：sent=%d/%d  %s" % (st["sent"], st["total"], st["reason"]))


def api_live_wheel_start(obj):
    msg = str(obj.get("msg") or "").strip()
    if not msg:
        return 400, {"error": "弹幕内容不能为空"}
    if len(msg) > DANMAKU_MAX_LEN:
        return 400, {"error": "弹幕最长 %d 个字（当前 %d 个）" % (DANMAKU_MAX_LEN, len(msg))}
    try:
        interval = float(obj.get("interval") or 3)
        count = int(obj.get("count") or 20)
    except (TypeError, ValueError):
        return 400, {"error": "间隔 / 条数必须是数字"}
    interval = max(WHEEL_INTERVAL_MIN, min(interval, 60.0))
    count = max(1, min(count, WHEEL_COUNT_MAX))
    if not _danmaku_ready():
        return 403, {"error": "独轮车需要登录 + bili_jct（直播间页右侧有入口）"}
    with WHEEL_LOCK:
        if WHEEL["running"]:
            return 409, {"error": "独轮车已在运行，先停止再重新开始"}
        WHEEL.update(running=True, msg=msg, interval=interval, total=count, sent=0,
                     last_code=None, last_message="", reason="", stop=threading.Event())
        threading.Thread(target=_wheel_worker, daemon=True).start()
    return 200, {"ok": True, "interval": interval, "count": count}


def api_live_wheel_stop():
    with WHEEL_LOCK:
        if WHEEL["running"] and WHEEL["stop"]:
            WHEEL["stop"].set()
            WHEEL["reason"] = "手动停止"
    return 200, {"ok": True}


def api_live_wheel_status():
    st = {k: WHEEL[k] for k in ("running", "msg", "interval", "total", "sent",
                                "last_code", "last_message", "reason")}
    st["room_id"] = ROOM_ID
    st["danmaku_ready"] = _danmaku_ready()
    return 200, st


DEFAULT_LIVE_QN = 250          # 超清。原画(10000)带宽太大，直播默认不抢它
LIVE_URL_CACHE = {"at": 0.0, "qn": 0, "url": "", "qualities": [], "current_qn": 0}
LIVE_URL_TTL = 60              # 流地址带时效，缓存一分钟，别频繁打接口


def live_play_url(qn):
    """取直播 FLV 地址（room/v1/Room/playUrl，不需要签名）。

    CDN 校验 Referer：不带 Referer 直连返回 403，所以浏览器只能在服务端中转后播放。
    """
    now = time.time()
    c = LIVE_URL_CACHE
    if c["url"] and c["qn"] == qn and now - c["at"] < LIVE_URL_TTL:
        return c
    url = ("https://api.live.bilibili.com/room/v1/Room/playUrl?cid=%s&qn=%s"
           "&platform=web&ptype=8"
           % (urllib.parse.quote(ROOM_ID), urllib.parse.quote(str(qn))))
    d = bili_get(url, "https://live.bilibili.com/%s" % ROOM_ID)
    if d.get("code") != 0:
        raise RuntimeError("B 站返回 code=%s" % d.get("code"))
    data = d.get("data") or {}
    media = ((data.get("durl") or [{}])[0]).get("url") or ""
    if not media:
        raise RuntimeError("没有取到直播流地址（可能刚下播）")
    c.update(at=now, qn=qn, url=media, current_qn=data.get("current_qn") or qn,
             qualities=[{"qn": q["qn"], "desc": q["desc"]}
                        for q in (data.get("quality_description") or [])])
    return c


def api_live_playinfo():
    """直播间画面信息：是否开播 + 可选清晰度。前端据此决定要不要接管播放器。"""
    out = {"room_id": ROOM_ID}
    try:
        live = fetch_live()
    except Exception as e:
        out.update(living=False, error="开播状态获取失败：%s" % e)
        return 200, out
    out.update(living=bool(live.get("living")), title=live.get("title") or "",
               online=live.get("online") or 0, uname=live.get("uname") or "")
    if out["living"]:
        try:
            info = live_play_url(DEFAULT_LIVE_QN)
            out["qualities"] = info["qualities"]
            out["current_qn"] = info["current_qn"]
        except Exception as e:
            out["error"] = str(e)
    return 200, out


def api_live_stream(handler, query):
    """转发直播流。直播是持续流，不支持 Range；断开由客户端决定（关页面 / 切走）。"""
    qn = parse_int(query.get("qn", ["0"])[0], DEFAULT_LIVE_QN)
    try:
        media = live_play_url(qn)["url"]
    except Exception as e:
        return 502, {"error": "取直播流失败：%s" % e}
    req = urllib.request.Request(media, headers={
        "User-Agent": UA, "Referer": "https://live.bilibili.com/%s" % ROOM_ID,
        "Accept": "*/*",
    })
    try:
        resp = urlopen(req, 30)
    except Exception as e:
        return 502, {"error": "连接直播流失败：%s" % e}
    handler.send_response(200)
    handler.send_header("Content-Type", "video/x-flv")
    handler.end_headers()
    try:
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass                       # 观众关页面 / 切走，属正常断开
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return None, None


def api_live_credential(obj):
    value = str(obj.get("jct") or "").strip()
    if not value:
        return 400, {"error": "bili_jct 不能为空"}
    try:
        save_credential("bili_jct", value)
    except ValueError as e:
        return 400, {"error": str(e)}
    return 200, {"ok": True, "danmaku_ready": _danmaku_ready()}


# ---- 实时弹幕（WebSocket 网关）--------------------------------------------
# wbi 签名：getDanmuInfo 无签名会被风控拦成 -352。实现从 cloud/server/app.py 移植。
_WBI_KEYS = None
WBI_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
             27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
             37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
             22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]


def wbi_keys():
    """img_key + sub_key 按 MIXIN 表重排得到 32 位混淆密钥，缓存住。"""
    global _WBI_KEYS
    if _WBI_KEYS is None:
        try:
            nav = bili_get("https://api.bilibili.com/x/web-interface/nav")
            wi = (nav.get("data") or {}).get("wbi_img") or {}
            raw = ((wi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
                   + (wi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0])
            _WBI_KEYS = "".join(raw[i] for i in WBI_MIXIN if i < len(raw))[:32] or ""
        except Exception:
            _WBI_KEYS = ""
    return _WBI_KEYS


def wbi_signed(url, params):
    keys = wbi_keys()
    if not keys:
        return url
    p = dict(params)
    p["wts"] = str(int(time.time()))
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(p[k]), safe=""))
                 for k in sorted(p))
    w_rid = hashlib.md5((q + keys).encode("utf-8")).hexdigest()
    return "%s?%s&w_rid=%s" % (url, q, w_rid)


def api_live_danmu_info():
    """弹幕 WebSocket 网关信息：前端直连 wss 收实时弹幕。"""
    sess = load_credentials()[0]
    url = wbi_signed("https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
                     {"id": ROOM_ID, "type": "0"})
    try:
        d = bili_get(url, "https://live.bilibili.com/%s" % ROOM_ID)
    except Exception as e:
        return 502, {"error": "弹幕网关获取失败：%s" % e}
    if d.get("code") != 0:
        return 502, {"error": "弹幕网关 code=%s %s" % (d.get("code"), d.get("message"))}
    data = d.get("data") or {}
    hosts = [{"host": h.get("host"), "wss_port": h.get("wss_port")}
             for h in (data.get("host_list") or []) if h.get("host")]
    return 200, {"room_id": ROOM_ID, "token": data.get("token") or "",
                 "hosts": hosts, "logged": bool(sess)}


# ---- 实时弹幕：服务端收、SSE 推给页面 -------------------------------------
# 为什么不让浏览器直连弹幕网关：浏览器握手带不上 bilibili 域的 Cookie，
# 实测认证后立刻被服务端断开（close 1006）；本机服务端带 Cookie + 官方 Origin
# 连接则正常。所以由服务端维持 WebSocket、解析后经 SSE 推给页面。
CHAT_SUBS = []                 # 订阅者队列（每个打开直播间页的浏览器一个）
CHAT_LOCK = threading.Lock()
CHAT_WORKER = {"thread": None, "running": False, "popularity": 0, "error": ""}
CHAT_BACKLOG = []              # 最近若干条，页面刚打开时先补上
CHAT_BACKLOG_MAX = 30
_BUVID = [None]
_UID = [None]


def self_uid():
    """弹幕认证里的 uid。有登录态时用真实 uid —— 用 0 会被网关直接关闭连接。"""
    if _UID[0] is None:
        try:
            d = bili_get("https://api.bilibili.com/x/web-interface/nav")
            _UID[0] = int(((d.get("data") or {}).get("mid") or 0))
        except Exception:
            _UID[0] = 0
    return _UID[0]


def buvid3():
    """弹幕网关握手要带 buvid3（设备指纹），取一次缓存住。"""
    if _BUVID[0] is None:
        try:
            d = bili_get("https://api.bilibili.com/x/frontend/finger/spi")
            _BUVID[0] = ((d.get("data") or {}).get("b_3") or "")
        except Exception:
            _BUVID[0] = ""
    return _BUVID[0]


def _ws_handshake(host, port, path, headers):
    raw = socket.create_connection((host, port), timeout=20)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n" % (path, host, key))
    for k, v in headers.items():
        req += "%s: %s\r\n" % (k, v)
    sock.sendall((req + "\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("握手期间连接被关闭")
        buf += chunk
    first = buf.split(b"\r\n", 1)[0].decode("latin-1")
    if "101" not in first:
        raise RuntimeError("握手失败：%s" % first)
    return sock


def _ws_send(sock, payload, opcode=2):
    """客户端发帧必须加掩码。"""
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
    elif n < 65536:
        head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv(sock, timeout=5):
    sock.settimeout(timeout)
    hdr = sock.recv(2)
    if len(hdr) < 2:
        return None, b""
    opcode = hdr[0] & 0x0F
    n = hdr[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", sock.recv(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", sock.recv(8))[0]
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return opcode, data


def _bili_pack(body, op, ver=1):
    """B 站包头 16 字节：包长(4) + 头长(2) + 版本(2) + 操作码(4) + 序号(4)。
    操作码是 4 字节 —— 按 2 字节写会被网关当成非法包直接断开。"""
    return struct.pack(">IHHII", 16 + len(body), 16, ver, op, 1) + body


def _chat_broadcast(item):
    payload = json.dumps(item, ensure_ascii=False)
    with CHAT_LOCK:
        CHAT_BACKLOG.append(payload)
        del CHAT_BACKLOG[:-CHAT_BACKLOG_MAX]
        subs = list(CHAT_SUBS)
    for q in subs:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def _chat_parse(data):
    off = 0
    while off + 16 <= len(data):
        ln = struct.unpack(">I", data[off:off + 4])[0]
        if ln < 16 or off + ln > len(data):
            return
        ver = struct.unpack(">H", data[off + 6:off + 8])[0]
        op = struct.unpack(">I", data[off + 8:off + 12])[0]
        body = data[off + 16:off + ln]
        if op == 3 and len(body) >= 4:
            CHAT_WORKER["popularity"] = struct.unpack(">I", body[:4])[0]
            _chat_broadcast({"type": "popularity", "value": CHAT_WORKER["popularity"]})
        elif op == 5:
            payload = body
            if ver == 2:
                try:
                    payload = zlib.decompress(body)
                except Exception:
                    payload = b""
            elif ver == 3:
                payload = b""            # brotli：本服务只请求 ver2，出现即忽略
            if payload:
                _chat_parse_nested(payload)
        off += ln


def _chat_parse_nested(payload):
    off = 0
    while off + 16 <= len(payload):
        ln = struct.unpack(">I", payload[off:off + 4])[0]
        if ln < 16 or off + ln > len(payload):
            return
        ver = struct.unpack(">H", payload[off + 6:off + 8])[0]
        op = struct.unpack(">I", payload[off + 8:off + 12])[0]
        if op == 5 and ver == 0:
            try:
                j = json.loads(payload[off + 16:off + ln].decode("utf-8", "replace"))
                if str(j.get("cmd") or "").startswith("DANMU_MSG") and j.get("info"):
                    info = j["info"]
                    who = info[2] or []
                    _chat_broadcast({"type": "danmaku", "uid": who[0] or 0,
                                     "uname": who[1] or "", "text": info[1] or ""})
            except Exception:
                pass
        off += ln


def _chat_worker():
    CHAT_WORKER.update(running=True, error="")
    try:
        while True:
            with CHAT_LOCK:
                if not CHAT_SUBS:
                    break
            sock = None
            try:
                info = api_live_danmu_info()[1]
                if not info.get("hosts") or not info.get("token"):
                    raise RuntimeError("没取到弹幕网关信息")
                host = info["hosts"][0]["host"]
                port = info["hosts"][0]["wss_port"] or 443
                sess = load_credentials()[0]
                ck = "buvid3=%s; b_nut=%d" % (buvid3(), int(time.time()))
                if sess:
                    ck = "SESSDATA=%s; %s" % (sess, ck)
                sock = _ws_handshake(host, port, "/sub", {
                    "Origin": "https://live.bilibili.com", "User-Agent": UA, "Cookie": ck})
                _ws_send(sock, _bili_pack(json.dumps({
                    "uid": self_uid(), "roomid": int(ROOM_ID), "proto_ver": 2,
                    "buvid": buvid3(), "platform": "web", "clientver": "1.14.3",
                    "type": 2, "key": info["token"]}).encode(), 7))
                last_hb = time.time()
                while True:
                    with CHAT_LOCK:
                        if not CHAT_SUBS:
                            break
                    if time.time() - last_hb > 25:
                        _ws_send(sock, _bili_pack(b"", 2))
                        last_hb = time.time()
                    try:
                        opcode, data = _ws_recv(sock, 5)
                    except socket.timeout:
                        continue
                    if opcode is None:
                        raise RuntimeError("网关关闭了连接")
                    if opcode == 2:
                        _chat_parse(data)
                    elif opcode == 9:
                        _ws_send(sock, data, opcode=10)
                    elif opcode == 8:
                        raise RuntimeError("网关要求关闭")
            except Exception as e:
                CHAT_WORKER["error"] = "%s: %s" % (type(e).__name__, e)
                _chat_broadcast({"type": "state", "text": "弹幕连接中断，正在重试…"})
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            time.sleep(3)                 # 重连间隔
    finally:
        CHAT_WORKER["running"] = False


def chat_subscribe():
    q = queue.Queue(maxsize=200)
    with CHAT_LOCK:
        CHAT_SUBS.append(q)
        backlog = list(CHAT_BACKLOG)
        need_worker = not CHAT_WORKER["running"]
    if need_worker:
        t = threading.Thread(target=_chat_worker, daemon=True)
        CHAT_WORKER["thread"] = t
        t.start()
    return q, backlog


def chat_unsubscribe(q):
    with CHAT_LOCK:
        if q in CHAT_SUBS:
            CHAT_SUBS.remove(q)


def api_login_qrcode():
    """申请扫码登录二维码。二维码内容就是 B 站的登录 URL，用 B 站 App 扫。"""
    try:
        d = bili_get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
    except Exception as e:
        return 502, {"error": "申请二维码失败：%s" % e}
    if d.get("code") != 0:
        return 502, {"error": "B 站返回 %s" % d.get("message")}
    dd = d.get("data") or {}
    return 200, {"url": dd.get("url"), "key": dd.get("qrcode_key")}


def api_login_poll(key):
    """轮询扫码状态；成功时从 Set-Cookie 里取出 SESSDATA 存到本机文件。

    code：86101 未扫描 / 86090 已扫描待确认 / 86038 已失效 / 0 成功
    """
    if not key:
        return 400, {"error": "缺少 key"}
    url = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
           "?qrcode_key=%s&source=main-fe-header" % urllib.parse.quote(key))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "identity",
    })
    try:
        with urlopen(req, 20) as r:
            raw = r.read()
            cookies = r.headers.get_all("Set-Cookie") or []
    except Exception as e:
        return 502, {"error": "轮询失败：%s" % e}

    d = json.loads(raw.decode("utf-8", "replace"))
    dd = d.get("data") or {}
    code = dd.get("code")
    out = {"code": code, "message": dd.get("message") or ""}
    if code == 0:
        # Set-Cookie 里同时带着 SESSDATA（身份）和 bili_jct（发弹幕的 CSRF），
        # 两个都存下来 —— 否则用户还得自己去浏览器里复制 bili_jct。
        sess = jct = ""
        for c in cookies:
            m = re.search(r"SESSDATA=([^;]+)", c)
            if m:
                sess = m.group(1)
            m = re.search(r"bili_jct=([^;]+)", c)
            if m:
                jct = m.group(1)
        if sess:
            save_credential("SESSDATA", sess)
        if jct:
            save_credential("bili_jct", jct)
        out["logged"] = bool(sess)
        out["jct"] = bool(jct)
        if not sess:
            out["message"] = "登录成功但未取到 SESSDATA"
        elif not jct:
            out["message"] = "登录成功，但未取到 bili_jct；重新扫码一次通常就有了"
    return 200, out


def api_logout():
    if os.path.exists(SESS_FILE):
        try:
            os.remove(SESS_FILE)
        except Exception:
            # 本机删除受限，退化为清空内容
            try:
                open(SESS_FILE, "w", encoding="utf-8").write("")
            except Exception:
                pass
    return 200, {"logged": False}


# ---------------------------------------------------------------- 实时清单

# 为什么要有这个接口
#   页面内嵌的 data/programs.js 是 collect.py 离线生成的快照，UP 主发新投稿后
#   不重新采集就不会变。用户要求「打开网页时拿到最新数据」，所以这里提供一个
#   实时抓取的接口，页面加载时优先用它；离线或接口失败时前端退回本地快照。
#
# 抓取策略
#   · 系列列表：x/series/archives 分页拉全（每页 30 条，本系列 2 页够用）
#   · 详情：x/web-interface/view，**带线程池并发**，否则 40 多个视频要串行等十几秒
#   · 结果缓存 PROGRAMS_TTL 秒，只用于挡住「同一批并发请求重复打接口」，
#     不作为「页面看到旧数据」的来源 —— 前端每次加载都会带 ?refresh=1 绕过它。

SERIES_ID = "5157110"
PROGRAMS_CACHE = {"at": 0.0, "data": None}
PROGRAMS_TTL = 240           # 仅用于并发去重与失败回退，页面加载带 refresh=1 时不生效
PROGRAMS_LOCK = threading.Lock()
VIEW_WORKERS = 8             # 详情接口并发数；B 站风控对并发较敏感，8 是实测安全值
VIEW_GAP = 0.05              # 每个并发请求前的抖动间隔


def _collect_mod():
    """复用 collect.py 的分类 / 标题清洗 / 缩略图逻辑，避免两处实现不一致

    打包后 collect.py 在 PyInstaller 的解包目录（sys._MEIPASS）里，
    不在 EXE 同目录 —— 这里要跟着走，否则实时清单接口会整体失败。
    """
    import importlib.util
    path = os.path.join(getattr(sys, "_MEIPASS", ROOT), "tools", "collect.py")
    spec = importlib.util.spec_from_file_location("collect_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_series_archives():
    """系列内全部投稿（分页拉全），返回 bilibili 原始 archives 列表"""
    items = []
    pn = 1
    while True:
        url = ("https://api.bilibili.com/x/series/archives?mid=%s&series_id=%s"
               "&only_normal=true&sort=desc&pn=%d&ps=30" % (MID, SERIES_ID, pn))
        d = bili_get(url, "https://space.bilibili.com/%s/lists/%s?type=series"
                          % (MID, SERIES_ID))
        if d.get("code") != 0:
            raise RuntimeError("系列接口 code=%s %s" % (d.get("code"), d.get("message")))
        data = d.get("data") or {}
        arcs = data.get("archives") or []
        items.extend(arcs)
        page = data.get("page") or {}
        total = page.get("total") or len(items)
        if not arcs or len(items) >= total:
            break
        pn += 1
        time.sleep(0.4)          # 翻页间隔，避免风控
    return items


def build_programs():
    """实时构建与 data/programs.json 同构的清单（字段、排序、评分全部对齐）"""
    cm = _collect_mod()
    archives = fetch_series_archives()
    if not archives:
        raise RuntimeError("系列接口未返回任何投稿")

    overrides = cm.load_overrides()
    results = {}

    def one(a):
        bvid = a["bvid"]
        try:
            d = bili_get("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid,
                         "https://www.bilibili.com/video/%s" % bvid)
        except Exception:
            return bvid, None
        if d.get("code") != 0:
            return bvid, None
        return bvid, d.get("data")

    # 并发取详情：串行 40+ 个视频要十几秒，首屏等不起
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=VIEW_WORKERS) as ex:
        for bvid, detail in ex.map(one, archives):
            if detail:
                results[bvid] = detail

    tz = __import__("datetime").timezone(
        __import__("datetime").timedelta(hours=8))
    dtime = __import__("datetime").datetime

    programs = []
    for a in archives:
        bvid = a["bvid"]
        detail = results.get(bvid)
        if not detail:
            continue                     # 单个视频失败不影响整份清单
        title = detail["title"]
        category = overrides.get(bvid, {}).get("category") or cm.classify(title)
        pages = detail.get("pages") or []
        total_dur = sum(p["duration"] for p in pages) or 1
        video_dm = (detail.get("stat") or {}).get("danmaku", 0)

        parts = [{
            "cid": p["cid"],
            "page": p["page"],
            "part": cm.clean_title(p.get("part") or title),
            "duration": p["duration"],
            "dm_total": int(round(video_dm * p["duration"] / total_dur)),
        } for p in pages]

        stat = detail.get("stat") or {}
        programs.append({
            "bvid": bvid,
            "aid": detail["aid"],
            "title": cm.clean_title(title),
            "raw_title": title,
            "category": category,
            "pubdate": detail["pubdate"],
            "date": dtime.fromtimestamp(detail["pubdate"], tz).strftime("%Y-%m-%d %H:%M"),
            "duration": total_dur,
            "parts": parts,
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "like": stat.get("like", 0),
            "cover": cm.to_https(detail.get("pic", "")),
            "thumb": cm.thumb(detail.get("pic", ""), "320w_200h_1c.webp"),
            "url": "https://www.bilibili.com/video/%s" % bvid,
            "dm_total": video_dm,
            "dm_per_hour": round(video_dm / (total_dur / 3600.0), 1) if total_dur else 0,
        })

    programs.sort(key=lambda p: -p["pubdate"])

    # 评分与离线版保持一致：弹幕密度 60% + 新鲜度 40%
    dens = sorted(p["dm_per_hour"] for p in programs)
    n = len(dens) or 1
    now = time.time()
    for p in programs:
        rank = sum(1 for dd in dens if dd <= p["dm_per_hour"]) / float(n)
        fresh = pow(2.718281828, -((now - p["pubdate"]) / 86400.0) / 30.0)
        p["score"] = round((0.6 * rank + 0.4 * fresh) * 100, 1)
        p["dm_rank"] = round(rank * 100, 1)

    meta = {
        "mid": MID, "series_id": SERIES_ID, "up_name": "四时小路Komichi",
        "count": len(programs),
        "part_count": sum(len(p["parts"]) for p in programs),
        "total_duration": sum(p["duration"] for p in programs),
        "generated_at": int(now),
        "live": True,                    # 标记这份清单来自实时抓取，前端据此提示
    }
    return {"meta": meta, "programs": programs}


# ---------------------------------------------------------------- 自动分段
# 把 tools/auto_segments.py 搬进网页：新回放出现时在后台补分段。
# 分段很重（要下载音频 + ffmpeg 解码 + 抽帧读「已唱」浮层），所以：
#   · 单线程队列，一次只跑一个投稿，不阻塞接口；
#   · 只给「新出现的」回放排队，不自动回头补历史缺口（避免一开就排几十场）；
#   · 结果仍写 data/segments.js，页面刷新即生效。

SEG_LOCK = threading.Lock()
SEG_JOB = {"running": False, "thread": None, "queue": [], "current": None,
           "done": [], "log": [], "error": "", "ffmpeg": None, "refine": None}
SEG_STATE_FILE = os.path.join(APPDIR, "seg_state.json")


def _seg_state():
    try:
        with open(SEG_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _seg_save_state(st):
    try:
        os.makedirs(os.path.dirname(SEG_STATE_FILE), exist_ok=True)
        with open(SEG_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError:
        pass


def _seg_module():
    """加载 auto_segments.py。打包后它在 _MEIPASS 的 tools/ 里（同 collect.py）。

    注意：模块自己算出的 ROOT 会指到 PyInstaller 的解包目录（临时、且不是页面在用的那份），
    所以打包时要把数据目录/缓存目录改到真实应用目录 —— 否则分段结果写在一个用不上的地方。
    """
    path = os.path.join(getattr(sys, "_MEIPASS", ROOT), "tools", "auto_segments.py")
    # 模块内部会 `from seg_refine import detect_keys`，而 importlib 加载不会把 tools/
    # 放进 sys.path（命令行跑时靠 sys.path[0] 恰好是 tools/）。不补这一步，
    # 网页路径下「已唱」边界精修会永远以「缺少依赖」告终 —— 纯音频分段会把歌从中间切开。
    mod_dir = os.path.dirname(path)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    import importlib.util
    spec = importlib.util.spec_from_file_location("auto_segments_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if FROZEN:
        mod.ROOT = ROOT                                    # = %LOCALAPPDATA%\KomichiRadio\www
        mod.DATA = os.path.join(ROOT, "data")              # 页面就是从这儿读 segments.js
        mod.CACHE = os.path.join(APPDIR, ".segcache")
        mod.WORK = os.path.join(APPDIR, ".segaudio")
        mod.FEAT = os.path.join(APPDIR, ".segfeat")
    return mod


def seg_ffmpeg():
    """找一次 ffmpeg 并缓存（查不到时会遍历目录，别每次请求都做）。"""
    if SEG_JOB["ffmpeg"] is None:
        try:
            SEG_JOB["ffmpeg"] = _seg_module().find_ffmpeg() or ""
        except Exception:
            SEG_JOB["ffmpeg"] = ""
    return SEG_JOB["ffmpeg"]


def seg_refine_ready():
    """边界精修（seg_refine）依赖 numpy + pillow，缺了就只能纯音频分段。

    实测两者都没有时不会报错，只是边界粗一些；如实报给界面，别让用户以为是同一套结果。
    """
    if SEG_JOB["refine"] is None:
        try:
            # 用 find_spec 而不是 import：静态 import 会让 PyInstaller 把 numpy/pillow
            # 一起打进单文件 EXE（凭空多几十 MB），而它们对本服务只是可选项。
            import importlib.util
            SEG_JOB["refine"] = bool(importlib.util.find_spec("numpy")
                                     and importlib.util.find_spec("PIL"))
        except Exception:
            SEG_JOB["refine"] = False
    return SEG_JOB["refine"]


def segments_have():
    """已有分段的分P cid 集合 —— 直接读 data/segments.js，不另维护状态。"""
    try:
        with open(os.path.join(ROOT, "data", "segments.js"), encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return set()
    m = re.search(r"window\.SEGMENTS\s*=\s*(\{.*\})\s*;", txt, re.S)
    if not m:
        return set()
    try:
        return set(json.loads(m.group(1)).keys())
    except Exception:
        return set()


def _seg_worker():
    """队列消费者：逐个投稿跑分段。"""
    try:
        while True:
            with SEG_LOCK:
                if not SEG_JOB["queue"]:
                    break
                bvid = SEG_JOB["queue"].pop(0)
            SEG_JOB["current"] = bvid
            SEG_JOB["log"] = []
            SEG_JOB["error"] = ""
            try:
                mod = _seg_module()

                def logf(msg):
                    # 日志只留最近 12 行：页面上够看，也不会把状态接口撑大
                    SEG_JOB["log"] = (SEG_JOB["log"] + [str(msg).strip()])[-12:]

                res = mod.process_bvid(bvid, logf=logf)
                res["at"] = int(time.time())
                if not res.get("ok"):
                    SEG_JOB["error"] = res.get("error") or "分段失败"
                with SEG_LOCK:
                    SEG_JOB["done"] = (SEG_JOB["done"] + [res])[-10:]
            except Exception as e:
                SEG_JOB["error"] = "%s: %s" % (type(e).__name__, e)
            finally:
                SEG_JOB["current"] = None
    finally:
        SEG_JOB["running"] = False


def segments_enqueue(bvid):
    """把投稿排进分段队列；队列空时顺手把 worker 拉起来。"""
    with SEG_LOCK:
        if bvid in SEG_JOB["queue"] or SEG_JOB["current"] == bvid:
            return False
        if any(d.get("bvid") == bvid and d.get("ok") for d in SEG_JOB["done"]):
            return False                       # 这轮已经成功处理过，别重复排队
        SEG_JOB["queue"].append(bvid)
        if not SEG_JOB["running"]:
            SEG_JOB["running"] = True
            t = threading.Thread(target=_seg_worker, daemon=True)
            SEG_JOB["thread"] = t
            t.start()
    return True


def segments_autoscan(programs):
    """新回放自动排队。只认「水位线之后出现」的投稿，历史缺口不自动补。

    首次开启时先把水位线设成当前最新的投稿 —— 否则一打开就把几十场老回放全排上。
    """
    if not _seg_state().get("auto"):
        return
    if seg_ffmpeg() == "":
        return                                 # 没有 ffmpeg，排了也白排
    have = segments_have()
    newest = max([p.get("pubdate") or 0 for p in programs] or [0])
    st = _seg_state()
    mark = st.get("seen_upto") or 0
    if not mark:
        st["seen_upto"] = newest
        _seg_save_state(st)
        return
    fresh = [p for p in programs
             if (p.get("pubdate") or 0) > mark
             and any(str(x["cid"]) not in have for x in p["parts"])]
    if not fresh:
        return
    for p in sorted(fresh, key=lambda x: x.get("pubdate") or 0):
        segments_enqueue(p["bvid"])
    st["seen_upto"] = newest
    _seg_save_state(st)


def api_segments_status():
    st = _seg_state()
    have = segments_have()
    try:
        with open(os.path.join(ROOT, "data", "programs.json"), encoding="utf-8") as f:
            total = sum(len(p["parts"]) for p in json.load(f)["programs"])
    except Exception:
        total = len(have)
    return 200, {
        "auto": bool(st.get("auto")),
        "ffmpeg": seg_ffmpeg(),
        "refine": seg_refine_ready(),
        "running": SEG_JOB["running"],
        "current": SEG_JOB["current"],
        "queue": list(SEG_JOB["queue"]),
        "done": list(SEG_JOB["done"]),
        "log": list(SEG_JOB["log"]),
        "error": SEG_JOB["error"],
        "coverage": {"have": len(have), "total": total},
        "seen_upto": st.get("seen_upto") or 0,
    }


def api_segments_auto(on):
    st = _seg_state()
    st["auto"] = bool(on)
    if on and not st.get("seen_upto"):
        # 开启时先立水位线：只对「从现在起」出现的新回放自动分段
        st["seen_upto"] = int(time.time())
    _seg_save_state(st)
    return 200, {"ok": True, "auto": bool(on), "seen_upto": st.get("seen_upto")}


# ---------------------------------------------------------------- 关掉网页就退出
# 页面关闭/刷新时用 sendBeacon 说一声，服务端等一小会儿没人回来就自己退出；
# 心跳是兜底（浏览器崩了、被强杀时 beacon 发不出来），超时给得宽松，
# 因为后台标签页的定时器会被浏览器降频到每分钟一次。

PAGE_LOCK = threading.Lock()
PAGES = {}                       # 页面 id -> 最近一次心跳时间
PAGES_SEEN = [False]             # 是否曾有页面连过（没有的话不许退出）
EXIT_TIMER = [None]
SERVER_REF = [None]              # main() 里填，退出定时器要用
PAGE_GRACE = 2.0                 # 收到 bye 后等这么久（刷新页面会在这个窗口内重新连上）
PAGE_IDLE = 180.0                # 心跳兜底：这么久没动静就退出


def _exit_now():
    srv = SERVER_REF[0]
    if srv:
        try:
            srv.shutdown()       # main() 的 serve_forever 会随即返回，进程正常退出
        except Exception:
            pass


def _schedule_exit(delay):
    with PAGE_LOCK:
        if EXIT_TIMER[0]:
            EXIT_TIMER[0].cancel()
        t = threading.Timer(delay, _exit_now)
        t.daemon = True
        EXIT_TIMER[0] = t
        t.start()


def _cancel_exit():
    with PAGE_LOCK:
        if EXIT_TIMER[0]:
            EXIT_TIMER[0].cancel()
            EXIT_TIMER[0] = None


def page_alive(cid):
    """页面报到 / 心跳。cid 是页面自己生成的随机串，一个标签页一个。"""
    with PAGE_LOCK:
        PAGES[cid] = time.time()
        PAGES_SEEN[0] = True
    _cancel_exit()
    return 200, {"ok": True}


def page_bye(cid):
    with PAGE_LOCK:
        PAGES.pop(cid, None)
        empty = not PAGES and PAGES_SEEN[0]
    if empty:
        _schedule_exit(PAGE_GRACE)
    return 200, {"ok": True}


def _page_watchdog():
    while True:
        time.sleep(10)
        now = time.time()
        with PAGE_LOCK:
            if not PAGES_SEEN[0]:
                continue                      # 还没有页面来过，别自作主张退出
            for cid in [c for c, t in PAGES.items() if now - t > PAGE_IDLE]:
                PAGES.pop(cid, None)
            if PAGES:
                continue
        _schedule_exit(1.0)


def start_page_watchdog():
    t = threading.Thread(target=_page_watchdog, daemon=True)
    t.start()


# ---------------------------------------------------------------- komichi:// 协议
# 浏览器不能直接执行 EXE，所以注册一个自定义协议：书签指向 komichi://open，
# 点击就由 Windows 把 EXE 拉起来（已在跑的话，新实例会把浏览器指回本服务）。
PROTOCOL = "komichi"


def protocol_registered():
    """返回已注册的命令行；未注册返回空串。源码运行不注册（目标是 python 没意义）。"""
    if not FROZEN:
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Classes\%s\shell\open\command" % PROTOCOL) as k:
            return str(winreg.QueryValueEx(k, "")[0] or "")
    except OSError:
        return ""


def protocol_register():
    if not FROZEN:
        return 400, {"error": "源码运行时不需要注册（可执行文件路径不是本程序）"}
    try:
        import winreg
        exe = os.path.abspath(sys.executable)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes\%s" % PROTOCOL) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:二十四时小路电台")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes\%s\shell\open\command" % PROTOCOL) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, '"%s" "%%1"' % exe)
    except OSError as e:
        return 500, {"error": "写注册表失败：%s" % e}
    return 200, {"ok": True, "command": protocol_registered(), "url": PROTOCOL + "://open"}


def protocol_unregister():
    if not FROZEN:
        return 400, {"error": "源码运行没有注册过"}
    try:
        import winreg
        for sub in (r"%s\shell\open\command" % PROTOCOL, r"%s\shell\open" % PROTOCOL,
                    r"%s\shell" % PROTOCOL, PROTOCOL):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\%s" % sub)
            except OSError:
                pass                          # 不存在就算了，目标是「删干净」
    except OSError as e:
        return 500, {"error": "删注册表失败：%s" % e}
    return 200, {"ok": True, "command": protocol_registered()}


def api_protocol_status():
    cmd = protocol_registered()
    return 200, {"supported": FROZEN, "registered": bool(cmd), "command": cmd,
                 "url": PROTOCOL + "://open"}


def api_programs(refresh=False):
    """GET /api/programs —— 实时回放清单。

    默认走缓存（仅用于并发去重）；页面加载会带 refresh=1 强制重抓。
    抓取失败时回退上一次结果（如果有），再失败让前端用本地 programs.js。
    """
    with PROGRAMS_LOCK:
        hit = PROGRAMS_CACHE["data"]
        fresh = hit and (time.time() - PROGRAMS_CACHE["at"]) < PROGRAMS_TTL
        if fresh and not refresh:
            out = dict(hit)
            out["cached"] = True
            return 200, out

    try:
        out = build_programs()
        out["cached"] = False
    except Exception as e:
        with PROGRAMS_LOCK:
            prev = PROGRAMS_CACHE["data"]
        if prev:
            out = dict(prev)
            out["cached"] = True
            out["error"] = str(e)
            return 200, out
        return 502, {"error": "取回放清单失败：%s" % e}

    with PROGRAMS_LOCK:
        PROGRAMS_CACHE["at"] = time.time()
        PROGRAMS_CACHE["data"] = out
    # 拿到最新清单后顺带看看有没有新回放需要分段（失败不影响清单接口）
    try:
        segments_autoscan(out.get("programs") or [])
    except Exception as e:
        print("自动分段检查失败：%s" % e)
    return 200, out


# ---------------------------------------------------------------- 小路状态

STATUS_CACHE = {"at": 0.0, "data": None}
STATUS_LOCK = threading.Lock()


def fetch_live():
    """当前是否在播。

    走 room/v1/Room/get_status_info_by_uids：只认 uid，不需要真实房间号，
    不挑 Referer、不需要 wbi 签名、也不依赖登录态，字段里直接带
    live_status / 标题 / 分区 / 封面。

    实测被排除的几条路：get_info 要先知道真实房间号且给的是另一个号；
    space/wbi/acc/info 无签名时风控 -352；空间投稿类接口 -799 限流。
    """
    d = bili_get("https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
                 "?uids[]=%s" % MID, "https://live.bilibili.com/")
    if d.get("code") != 0:
        raise RuntimeError("开播接口 code=%s %s" % (d.get("code"), d.get("message")))
    info = ((d.get("data") or {}).get(MID)) or {}
    if not info:
        raise RuntimeError("开播接口未返回该 uid 的数据")

    room = str(info.get("room_id") or ROOM_ID)
    return {
        "living": int(info.get("live_status") or 0) == 1,
        "mid": MID,
        "room_id": room,
        "url": "https://live.bilibili.com/%s" % room,
        "uname": info.get("uname") or "",
        "face": info.get("face") or "",
        "title": info.get("title") or "",
        "area": info.get("area_v2_name") or "",
        "parent_area": info.get("area_v2_parent_name") or "",
        "online": info.get("online") or 0,
        "cover": info.get("cover_from_user") or info.get("keyframe") or "",
        # live_time 未开播时是 0 或负数，前端据此判断「从未开播」还是「已播多久」
        "start": int(info.get("live_time") or 0),
    }


def api_status_board(refresh=False):
    """右下角「小路状态」弹窗的数据源。

    为什么这里没有「最新动态」：动态接口
    api.bilibili.com/x/web-dynamic/v1/feed/space 对不带浏览器指纹的
    服务端请求一律返回 412（实测），加任何 Referer 都无效；同类旧接口已下线。
    所以「有新内容」改用两条能稳定复现的链路来表达——
      ① 回放系列里的最新一集（/api/programs 里有，前端直接用）
      ② 空间投稿更新（本接口带，失败也不影响开播状态）
    前端把这两项与「开播状态」并列展示，语义上都是「小路最近有什么动静」。

    关于缓存：页面加载时就要求拿最新的开播状态，所以这里**不设有效缓存**，
    每次都重新问一次接口。STATUS_LOCK 只用来串行化写操作，避免并发请求
    把回退数据互相覆盖。
    """
    out = {"mid": MID, "cached": False, "at": int(time.time())}
    with STATUS_LOCK:
        hit = STATUS_CACHE["data"]

    try:
        out["live"] = fetch_live()
    except Exception as e:
        # 接口偶发失败时，退回上一次成功的结果，好过让弹窗空白
        prev = (hit or {}).get("live")
        out["live"] = prev or {"living": False, "mid": MID, "room_id": ROOM_ID,
                               "url": "https://live.bilibili.com/%s" % ROOM_ID}
        out["live_error"] = str(e)
    out["ok"] = bool(out.get("live"))
    with STATUS_LOCK:
        STATUS_CACHE["at"] = time.time()
        STATUS_CACHE["data"] = out
    return 200, out



class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/stream" in (self.path or ""):
            return                      # 媒体流日志太吵
        sys.stderr.write("  %s\n" % (fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_chat(self):
        """把服务端收到的实时弹幕用 SSE 推给页面（浏览器直连网关会被拒，见 chat 段注释）。"""
        q, backlog = chat_subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            for item in backlog:
                self.wfile.write(("data: %s\n\n" % item).encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(("data: %s\n\n" % item).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass                      # 观众关页面 / 切走
        finally:
            chat_unsubscribe(q)
        return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/status":
            code, obj = api_status()
            return self._json(code, obj)
        if parsed.path == "/api/ping":
            # 只回答「我是谁」。给启动时的实例探测用 —— 不能复用 /api/status，
            # 那个要问 B 站接口，慢的时候会把本服务误判成「别的程序」。
            return self._json(200, {"app": APP_TAG})
        if parsed.path == "/api/quit":
            # 供「停止」脚本调用。服务只绑 127.0.0.1，外部访问不到。
            # shutdown() 要等 serve_forever 退出，必须另起线程，否则会把当前响应卡死。
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json(200, {"ok": True})
        if parsed.path == "/api/playurl":
            code, obj = api_playurl(q)
            return self._json(code, obj)
        if parsed.path == "/api/img":
            code, ct, body = api_img(q.get("u", [""])[0])
            if code != 200 or body is None:
                return self._json(code, {"error": "取图失败"})
            self.send_response(200)
            self.send_header("Content-Type", ct or "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/dashinfo":
            code, obj = api_dashinfo(q)
            return self._json(code, obj)

        if parsed.path == "/api/dash":
            host = (self.headers.get("X-Forwarded-Host")
                    or self.headers.get("Host") or "127.0.0.1:8765").split(",")[0].strip()
            r = api_dash(host, q)
            if len(r) == 2:                       # 出错
                return self._json(r[0], r[1])
            _, mpd, vq, vdesc = r
            body = mpd.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/dash+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Quality", str(vq))
            self.send_header("X-Quality-Desc", urllib.parse.quote(vdesc))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/login/qrcode":
            code, obj = api_login_qrcode()
            return self._json(code, obj)
        if parsed.path == "/api/login/poll":
            code, obj = api_login_poll(q.get("key", [""])[0])
            return self._json(code, obj)
        if parsed.path == "/api/logout":
            code, obj = api_logout()
            return self._json(code, obj)
        if parsed.path == "/api/status-board":
            code, obj = api_status_board("refresh" in q)
            return self._json(code, obj)
        if parsed.path == "/api/programs":
            code, obj = api_programs("refresh" in q)
            return self._json(code, obj)
        if parsed.path == "/api/segments/status":
            return self._json(*api_segments_status())
        if parsed.path == "/api/protocol":
            return self._json(*api_protocol_status())
        if parsed.path == "/api/page/alive":
            return self._json(*page_alive(q.get("cid", [""])[0] or "anon"))
        if parsed.path == "/api/live/wheel":
            return self._json(*api_live_wheel_status())
        if parsed.path == "/api/live/playinfo":
            return self._json(*api_live_playinfo())
        if parsed.path == "/api/live/danmu-info":
            return self._json(*api_live_danmu_info())
        if parsed.path == "/api/live/chat/stream":
            return self._sse_chat()
        if parsed.path == "/api/live/stream":
            code, obj = api_live_stream(self, q)
            if code is None:
                return
            return self._json(code, obj)
        if parsed.path == "/api/stream":
            code, obj = api_stream(self, q)
            if code is None:
                return
            return self._json(code, obj)

        return super().do_GET()

    def do_POST(self):
        """直播弹幕相关接口。凭据走 POST body，不能走 GET —— 会整个进访问日志。"""
        parsed = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return self._json(400, {"error": "请求体不是合法 JSON"})
        if not isinstance(obj, dict):
            return self._json(400, {"error": "请求体必须是 JSON 对象"})

        if parsed.path == "/api/live/credential":
            return self._json(*api_live_credential(obj))
        if parsed.path == "/api/segments/auto":
            return self._json(*api_segments_auto(obj.get("on")))
        if parsed.path == "/api/page/bye":
            return self._json(*page_bye(str(obj.get("cid") or "anon")))
        if parsed.path == "/api/protocol/register":
            return self._json(*protocol_register())
        if parsed.path == "/api/protocol/unregister":
            return self._json(*protocol_unregister())
        if parsed.path == "/api/segments/run":
            bvid = str(obj.get("bvid") or "").strip()
            if not bvid:
                return self._json(400, {"error": "缺少 bvid"})
            if seg_ffmpeg() == "":
                return self._json(400, {"error": "本机没找到 ffmpeg，无法做分段"})
            queued = segments_enqueue(bvid)
            return self._json(200, {"ok": True, "queued": queued})
        if parsed.path == "/api/live/send":
            return self._json(*api_live_send(obj))
        if parsed.path == "/api/live/wheel/start":
            return self._json(*api_live_wheel_start(obj))
        if parsed.path == "/api/live/wheel/stop":
            return self._json(*api_live_wheel_stop())
        return self._json(404, {"error": "未知接口"})

    def end_headers(self):
        # 本机服务：除缩略图外一律 no-store。静态文件（index.html / assets/ / data/）
        # 若交给浏览器做启发式缓存，重跑 collect.py 或 auto_segments.py 之后
        # 页面仍会显示旧数据。/api/img 自带长缓存（缩略图内容不变）故不覆盖。
        if not (self.path or "").startswith("/api/img"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


class LocalServer(ThreadingHTTPServer):
    """本机服务。

    必须关掉 allow_reuse_address：Windows 的 SO_REUSEADDR 语义和 Unix 不一样 ——
    它允许两个进程绑同一个端口（甚至劫持已在监听的端口）。于是「重复双击 EXE」
    不会报端口占用，而是悄悄起出第二个实例，连 --stop 都停不干净
    （/api/quit 只会命中其中一个）。关掉之后 bind 冲突会正常抛 OSError，
    main() 才能走「复用已有实例」那条分支。
    """
    allow_reuse_address = os.name != "nt"


def open_browser(url):
    """打开默认浏览器。webbrowser 在打包环境里偶尔探不到浏览器，再兜一层 os.startfile。"""
    try:
        import webbrowser
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    try:
        os.startfile(url)               # 仅 Windows
        return True
    except Exception:
        return False


def notify(msg, error=False):
    """把结果告诉用户。

    打包版是 --noconsole 静默运行，控制台输出没人看得到（只进日志文件），
    所以关键信息（启动失败、停止结果）额外弹一个系统对话框。
    """
    print(msg)
    if not FROZEN:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "二十四时小路电台",
                                         0x10 if error else 0x40)
    except Exception:
        pass


def is_our_service(base):
    """该地址上跑的是不是本服务 —— 端口被占用时用来区分「已有实例」和「别的程序」"""
    try:
        with urllib.request.urlopen(base + "api/ping", timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_TAG
    except Exception:
        return False


def stop_running(base):
    try:
        urllib.request.urlopen(base + "api/quit", timeout=5).read()
        return True
    except Exception:
        return False


def bind_server(bind, port, tries=20):
    """绑定端口；被别的程序占用时顺延。返回 (server, port)，全占用则 (None, None)"""
    for p in range(port, port + tries):
        try:
            return LocalServer((bind, p), Handler), p
        except OSError:
            continue
    return None, None


def main():
    # 通过 komichi:// 协议被拉起时，Windows 会把那个 URL 当参数递进来（如 komichi://open/），
    # argparse 不认它就会直接报错退出 —— 先摘掉。
    argv = [a for a in sys.argv[1:] if not a.lower().startswith(PROTOCOL + ":")]
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的实例后退出")
    args = ap.parse_args(argv)

    url = "http://%s:%d/" % (args.bind, args.port)

    if args.stop:
        notify("已停止。" if stop_running(url) else "没有找到正在运行的实例。")
        return 0

    if not os.path.exists(os.path.join(ROOT, "index.html")):
        # 正常情况下走不到这里：单文件模式已从内置资源释放。
        # 能走到说明 %LOCALAPPDATA% 不可写、EXE 同目录也没有网页文件。
        notify("网页文件缺失，且无法从 EXE 内置资源释放。\n\n"
               "请确认 %s 可写，或重新获取完整的 EXE。" % APPDIR,
               error=True)
        return 1

    try:
        srv = LocalServer((args.bind, args.port), Handler)
    except OSError:
        if is_our_service(url):
            # 已经有一个实例在跑（用户又双击了一次）：把浏览器指过去，不重复启动
            print("已有实例在 %s 运行。" % url)
            if not args.no_browser:
                open_browser(url)
            return 0
        # 端口被别的程序占了：顺延，别让用户对着「启动失败」发呆
        srv, port = bind_server(args.bind, args.port + 1)
        if srv is None:
            notify("端口 %d 起连续 20 个都被占用，无法启动。" % args.port, error=True)
            return 1
        print("端口 %d 被别的程序占用，已改用 %d。" % (args.port, port))
        url = "http://%s:%d/" % (args.bind, port)

    s = sessdata()
    print("二十四时小路电台 · 本地服务")
    print("  目录：%s" % ROOT)
    print("  凭据：%s" % (SESS_FILE if s else
                        "未登录（最高 480P）。如需 1080P，点播放器右上角「登录」扫码"))
    print("  地址：%s" % url)
    print("  停止：网页右下角「停止本地服务」，或本程序加 --stop 参数")

    if not args.no_browser:
        # 端口已经绑定，此刻打开不会连接失败
        print("  打开浏览器：%s" % ("成功" if open_browser(url) else "失败（请手动访问上面的地址）"))

    # 页面全关掉就退出（页面会 sendBeacon 说一声；心跳兜底浏览器崩溃的情况）
    SERVER_REF[0] = srv
    start_page_watchdog()
    print("  退出时机：网页全部关闭后自动退出（另有 %d 分钟无心跳兜底）"
          % int(PAGE_IDLE // 60))

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
