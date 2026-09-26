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
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

FROZEN = bool(getattr(sys, "frozen", False))    # True = 由 PyInstaller 打成的 EXE

if FROZEN:
    # 打包后网页文件（index.html / assets / data）与 EXE 同目录：改前端不用重新打包。
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
    # 凭据和日志写用户目录，EXE 放在只读位置（Program Files、只读盘）也能跑。
    APPDIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                          "KomichiRadio")
    try:
        os.makedirs(APPDIR, exist_ok=True)
    except OSError:
        APPDIR = ROOT
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
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
        with urllib.request.urlopen(req, timeout=20) as r:
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
        resp = urllib.request.urlopen(req, timeout=40)
    except urllib.error.HTTPError as e:
        # 地址过期（签名带时效）时把状态原样透出，前端会重新取地址
        handler.send_response(e.code)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return None, None
    except Exception as e:
        return 502, {"error": "转发失败：%s" % e}

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
    try:
        while True:
            # 256 KB 一块。64 KB 时代理吞吐成为瓶颈 → 视频加载慢的主因。
            chunk = resp.read(262144)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # 拖进度条 / 关页面时客户端会直接掐断，属正常现象，不该刷一屏 traceback
        pass
    return None, None


def save_sessdata(value):
    os.makedirs(os.path.dirname(SESS_FILE), exist_ok=True)
    with open(SESS_FILE, "w", encoding="utf-8") as f:
        f.write(value.strip() + "\n")


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
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            cookies = r.headers.get_all("Set-Cookie") or []
    except Exception as e:
        return 502, {"error": "轮询失败：%s" % e}

    d = json.loads(raw.decode("utf-8", "replace"))
    dd = d.get("data") or {}
    code = dd.get("code")
    out = {"code": code, "message": dd.get("message") or ""}
    if code == 0:
        for c in cookies:
            m = re.search(r"SESSDATA=([^;]+)", c)
            if m:
                save_sessdata(m.group(1))
                out["logged"] = True
                break
        if not out.get("logged"):
            out["message"] = "登录成功但未取到 SESSDATA"
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
        if parsed.path == "/api/stream":
            code, obj = api_stream(self, q)
            if code is None:
                return
            return self._json(code, obj)

        return super().do_GET()

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的实例后退出")
    args = ap.parse_args()

    url = "http://%s:%d/" % (args.bind, args.port)

    if args.stop:
        notify("已停止。" if stop_running(url) else "没有找到正在运行的实例。")
        return 0

    if not os.path.exists(os.path.join(ROOT, "index.html")):
        notify("缺少网页文件 index.html。\n\n"
               "请把 EXE 与 index.html、assets、data 放在同一个文件夹里再运行。",
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
    print("  停止：Ctrl+C，或运行「停止.bat」")

    if not args.no_browser:
        # 端口已经绑定，此刻打开不会连接失败
        print("  打开浏览器：%s" % ("成功" if open_browser(url) else "失败（请手动访问上面的地址）"))

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
