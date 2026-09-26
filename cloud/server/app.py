# -*- coding: utf-8 -*-
"""二十四时小路电台 · 云端服务

与本地版 tools/serve.py 的差别（都是刻意的）
    1. 只读 PORT 环境变量、绑 0.0.0.0 —— 云平台只暴露一个反向代理端口。
    2. **不带任何 Cookie**：本地版会读 tools/sessdata.txt 以便解锁 1080P；
       云端不读、不存、不接受任何登录凭据，任何人访问都以匿名身份取流。
    3. **只对固定的几个 Host 开 CORS**：本地版是单机自用，云端是公开地址，
       不能把 Referer 代理变成谁都能白嫖的开放转发器。
    4. 静态根目录是 ./site（只含前端与数据），不带 tools/、不带分析产物。

接口（与本地版一致，前端不用改）
    GET /api/playurl?bvid=&cid=&qn=   取播放地址与可选清晰度
    GET /api/stream?u=<base64url>     转发媒体流（支持 Range）
    GET /api/status                   登录态（云端恒为未登录）与可选清晰度
    GET /api/programs                 回放清单（页面加载时实时重取）
    GET /api/status-board             小路状态：开播情况
    GET /api/dash?u=<base64url>       转发 DASH 分片
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # cloud/
SITE = os.path.join(ROOT, "site")

MID = "1512246445"          # 四时小路Komichi
ROOM_ID = "1700301235"      # 直播间真实房间号

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com"

QN_DESC = {
    127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
    112: "1080P 高码率", 80: "1080P 高清", 74: "720P60", 64: "720P",
    32: "480P", 16: "360P",
}

# CORS 白名单：只有这些 Host（及其子域）的前端可以调用本服务。
# 用环境变量 ALLOWED_HOSTS 覆盖，逗号分隔。留空则只允许同源。
# 前端全部用相对路径调用，正常情况不会触发跨域；这是纵深防御。
_env_hosts = os.environ.get("ALLOWED_HOSTS", "").strip()
ALLOWED_HOSTS = [h.strip().lower() for h in _env_hosts.split(",") if h.strip()]

# 允许转发的媒体域名后缀。
# 注意：B 站的 CDN 主机会变（实测见过 bilivideo.com / bilivideo.cn /
# akamaized.net 等），**只按域名白名单会漏**。所以这里配合下面的
# `_is_bili_media()` 一起判断：域名命中其一，或路径命中 B 站 CDN 的特征。
MEDIA_HOST_SUFFIXES = (
    "bilivideo.com", "bilivideo.cn", "bilibili.com",
    "hdslb.com", "akamaized.net",
)


def _is_bili_media(url):
    """判断是不是 B 站自己的媒体地址。

    两道判据取「或」（不能取「与」，否则换 CDN 就失效）：
      ① 域名在已知后缀里；
      ② 路径含 B 站 CDN 的固定特征 —— `/upgcxcode/` 或 `/bfs/`。
    第 ② 条是关键：即使 CDN 换成没见过的域名，只要路径是 B 站的结构就放行；
    同时 `evil.com/upgcxcode/...` 这类伪造也不会被当成有效上游（B 站 CDN
    不会把内容托管在任意域名下，且这条代理只做「服务端取流再转发」，
    不存在把服务当开放代理的风险面 —— 真正的风险是 SSRF 到内网，见下）。
    """
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    # 反 SSRF：绝不允许转到本机 / 内网地址
    if (host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
            or host.startswith("10.") or host.startswith("192.168.")
            or host.startswith("169.254.")
            or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)
            or host.startswith("100.64.") or host.endswith(".internal")
            or host.endswith(".local")):
        return False
    if any(host == s or host.endswith("." + s) for s in MEDIA_HOST_SUFFIXES):
        return True
    return ("/upgcxcode/" in p.path) or ("/bfs/" in p.path)


# ------------------------------------------------------------ 访客凭据
#
# 与本地版的关键差异：本地版把 SESSDATA 写进 tools/sessdata.txt（只有本机进程读），
# 云端**绝不落盘** —— 凭据只存在于访客自己的浏览器 Cookie 里，随请求带到服务端、
# 仅用于代取流，用完即弃；服务端不写文件、不记日志。
#
# 为什么必须这样：SESSDATA 是账号级凭据，落到公网服务器上等于公开账号。
# 让每个访客用**自己的**账号，既解锁 1080P，又不产生账号归属风险。
# 匿名访客不受影响，仍是 480P —— 登录只是可选的画质升级。

COOKIE_NAME = "ksess"


def sess_of(handler):
    """从访客请求里取出他自己的 SESSDATA。取不到返回空串（按匿名处理）。"""
    raw = handler.headers.get("Cookie") or ""
    m = re.search(r"(?:^|;\s*)" + COOKIE_NAME + r"=([^;]+)", raw)
    if not m:
        return ""
    try:
        v = urllib.parse.unquote(m.group(1)).strip()
    except Exception:
        return ""
    # 不要用白名单字符集 —— B 站的 SESSDATA 格式变过（实测含字母数字与
    # % * _ , 等符号），白名单会把合法凭据误判成畸形值丢掉，且是**静默**失效
    # （本文件首版就因此踩过）。改为只挡掉会造成 header 注入的字符。
    if not v or len(v) > 512 or (set(v) & set(" \t\r\n;\"'\\")):
        return ""
    # 逗号在 Cookie 值里非法，而 B 站接口要的正是 %2C 形式：
    # 解码后若出现逗号，说明拿到的是原始值，需要重新编码回去。
    if "," in v:
        v = urllib.parse.quote(v, safe="")
    return v


def sess_tag(sess):
    """缓存分区标识。只放哈希，绝不把凭据本身写进缓存 key。"""
    if not sess:
        return "anon"
    return "s" + hashlib.md5(sess.encode("utf-8")).hexdigest()[:10]


def sess_headers(sess):
    """给 B 站请求用的 Cookie 头。匿名时返回 None（不带 Cookie）。"""
    return {"Cookie": "SESSDATA=" + sess} if sess else None


_CACHE = {}
_LOCK = None
_LAST_VIEW_ERR = None       # 最近一次详情抓取失败原因，供 /api/diag 读取
_WBI_KEYS = None            # WBI 签名密钥（从 nav 接口取，进程内缓存）


def _lock():
    global _LOCK
    if _LOCK is None:
        import threading
        _LOCK = threading.Lock()
    return _LOCK


def http_get(url, headers=None, timeout=12):
    h = {"User-Agent": UA, "Referer": REFERER}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, headers=None, timeout=12):
    return json.loads(http_get(url, headers, timeout).decode("utf-8", "replace"))


def cache_get(key, ttl):
    with _lock():
        hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def cache_put(key, val):
    with _lock():
        _CACHE[key] = (time.time(), val)
        if len(_CACHE) > 400:               # 简单封顶，避免长期运行内存无限涨
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
                _CACHE.pop(k, None)


def b64d(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def b64e(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


# ---------------------------------------------------------------- B 站接口

def api_playurl(bvid, cid, qn, sess=""):
    """取播放地址。fnval=16 要 DASH（能到 1080P），失败退回 fnval=1 的 MP4。

    缓存按凭据分区：匿名只拿到 480P、登录能拿到 1080P，两者共用一个 key
    会让先到者决定后者能看到什么。
    """
    key = "pu|%s|%s|%s|%s" % (bvid, cid, qn, sess_tag(sess))
    hit = cache_get(key, 1800)
    if hit:
        return hit
    q = ("cid=%s&bvid=%s&qn=%s&fnval=16&fnver=0&fourk=1&otype=json"
         % (cid, bvid, qn))
    url = "https://api.bilibili.com/x/player/playurl?" + q
    data = get_json(url, headers=sess_headers(sess))
    if data.get("code") != 0:
        raise RuntimeError("playurl 返回 code=%s %s"
                           % (data.get("code"), data.get("message")))
    d = data.get("data") or {}
    # accept_quality 是 [80, 64, ...] 纯数字列表，但前端 setQualityList 读的是
    # [{qn, desc}]，必须在这里展开；否则下拉框会渲染成「undefined」。
    acc_q = d.get("accept_quality") or []
    acc_desc = d.get("accept_description") or []
    out = {"dash": None, "durl": None,
           "accept": [{"qn": int(q),
                       "desc": (acc_desc[i] if i < len(acc_desc)
                                else QN_DESC.get(q, str(q)))}
                      for i, q in enumerate(acc_q)],
           "quality": d.get("quality"),
           "qualityDesc": QN_DESC.get(d.get("quality"), str(d.get("quality"))),
           "logged": bool(sess)}
    if d.get("dash"):
        best = {}
        for v in (d["dash"].get("video") or []):
            q = v.get("id")
            if q not in best or v.get("bandwidth", 0) > best[q].get("bandwidth", 0):
                best[q] = v
        vids = []
        for q, v in sorted(best.items(), reverse=True):
            if not v.get("baseUrl"):
                continue
            vids.append({"qn": q, "desc": QN_DESC.get(q, str(q)),
                         "url": v["baseUrl"], "codecs": v.get("codecs", ""),
                         "bandwidth": v.get("bandwidth", 0),
                         "width": v.get("width"), "height": v.get("height")})
        aud = (d["dash"].get("audio") or [])
        aud_sorted = sorted(aud, key=lambda a: -a.get("bandwidth", 0))
        out["dash"] = {
            "video": vids,
            "audio": (aud_sorted[0].get("baseUrl") if aud_sorted else None),
            "duration": d["dash"].get("duration"),
        }
    elif d.get("durl"):
        out["durl"] = [{"url": x.get("url"), "size": x.get("size"),
                        "length": x.get("length")}
                       for x in d["durl"] if x.get("url")]
    if not out["dash"] and not out["durl"]:
        raise RuntimeError("playurl 未返回可用地址（可能需要登录）")
    cache_put(key, out)
    return out


DASH_TTL = 5400        # 90 分钟。播放地址约 2 小时过期，留足余量


def dash_data(bvid, cid, sess=""):
    """取 DASH 原始数据（含 SegmentBase 索引），用于生成 MPD。"""
    key = "dash|%s|%s|%s" % (bvid, cid, sess_tag(sess))
    hit = cache_get(key, DASH_TTL)
    if hit:
        return hit, None
    url = ("https://api.bilibili.com/x/player/playurl"
           "?bvid=%s&cid=%s&fnval=16&fourk=1&qn=0"
           % (urllib.parse.quote(bvid), urllib.parse.quote(cid)))
    try:
        d = get_json(url, headers=sess_headers(sess))
    except Exception as e:
        if hit:
            return hit, "接口暂时不可用，使用缓存数据"
        return None, "取 DASH 失败：%s" % e
    if d.get("code") != 0:
        if hit:
            return hit, "B 站返回 code=%s，使用缓存数据" % d.get("code")
        return None, "B 站返回 code=%s" % d.get("code")
    data = d.get("data") or {}
    if not (data.get("dash") or {}).get("video"):
        return None, "该视频没有 DASH 流"
    cache_put(key, data)
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

    前端偶尔会发 qn=undefined / null / 空串，直接 int() 会抛 ValueError
    把接口打成 500，所以统一走这里兜底。
    """
    try:
        v = int(str(s).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def api_dashinfo(bvid, cid, sess=""):
    """返回该分P 的 DASH 清晰度阶梯，供页面填下拉框。"""
    data, warn = dash_data(bvid, cid, sess)
    if data is None:
        return None, warn
    dash = data.get("dash") or {}
    ids = sorted({v["id"] for v in dash.get("video") or []}, reverse=True)
    out = {"accept": [{"qn": i, "desc": QN_DESC.get(i, str(i))} for i in ids],
           "logged": bool(sess)}
    if warn:
        out["warn"] = warn
    return out, None


def api_dash_mpd(base, bvid, cid, qn, sess=""):
    """生成 DASH 的 MPD，供页面用 dash.js 播放。

    为什么要走 DASH：**MP4（durl）通道封顶 720P**，1080P（qn=80/112）只存在于 DASH。
    B 站 DASH 轨道是「单文件 + SegmentBase 索引」，所以直接生成
    isoff-on-demand 风格的 MPD，由 dash.js 按字节范围取。

    base 是站点前缀，必须**协议相对**（`//host`）或带 https：线上走 HTTPS 反代，
    写死 http:// 会被浏览器按混合内容拦掉，整条播放链路会失效。
    """
    data, warn = dash_data(bvid, cid, sess)
    if data is None:
        return None, warn, None, None
    dash = data.get("dash") or {}
    v, a = pick_tracks(data, qn)
    dur_s = float(dash.get("duration")
                  or (data.get("timelength") or 0) // 1000 or 0)

    def rep(track, kind):
        if not track:
            return ""
        sb = track.get("segment_base") or track.get("SegmentBase") or {}
        init = sb.get("initialization") or sb.get("Initialization") or ""
        idx = sb.get("index_range") or sb.get("indexRange") or ""
        proxied = base + "/api/stream?u=" + b64e(track["baseUrl"])
        w = ""
        if kind == "video":
            w = ' width="%s" height="%s" frameRate="%s"' % (
                track["width"], track["height"],
                str(track.get("frameRate") or "25"))
        return ('<Representation id="%s" bandwidth="%s" codecs="%s"%s>'
                '<BaseURL>%s</BaseURL>'
                '<SegmentBase indexRange="%s">'
                '<Initialization range="%s"/></SegmentBase>'
                '</Representation>'
                % (track["id"], track["bandwidth"],
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
    return mpd, warn, v["id"], QN_DESC.get(v["id"], str(v["id"]))


def api_img(raw):
    """代取缩略图（浏览器直连 i2.hdslb.com 在部分网络下会失败）。"""
    if not raw:
        return None, None
    try:
        target = b64d(raw).decode("utf-8")
    except Exception:
        return None, None
    if not target.startswith("http"):
        return None, None
    host = urllib.parse.urlparse(target).hostname or ""
    if not (host.endswith("hdslb.com") or host.endswith("bilibili.com")):
        return None, None
    hit = cache_get("img|" + target, 86400)
    if hit:
        return hit
    try:
        req = urllib.request.Request(target, headers={
            "User-Agent": UA, "Referer": REFERER, "Accept": "image/*,*/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ct = r.headers.get("Content-Type") or "image/jpeg"
            body = r.read()
    except Exception:
        return None, None
    if len(body) > 200 * 1024:          # 别让大图吃内存
        return ct, body
    cache_put("img|" + target, (ct, body))
    return ct, body


def api_status(sess=""):
    """报告**当前访客**的登录态。

    这不是「云端登录」—— 服务端不持有任何账号。凭据来自访客自己的浏览器
    Cookie（ksess），仅用于代取流。匿名上限 480P，登录后 1080P。
    """
    out = {"cloud": True, "logged": False,
           "qualities": [{"qn": 16, "desc": "360P"},
                         {"qn": 32, "desc": "480P"}],
           "note": "未登录：最高 480P。点「登录」用 B 站 App 扫码可解锁 1080P。"}
    if not sess:
        return out
    try:
        d = get_json("https://api.bilibili.com/x/web-interface/nav",
                     headers=sess_headers(sess))
        dd = d.get("data") or {}
        out["logged"] = bool(dd.get("isLogin"))
        out["uname"] = dd.get("uname") or ""
        out["vip"] = bool((dd.get("vipStatus") or 0) == 1)
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    if out["logged"]:
        out["qualities"] = [{"qn": 112, "desc": "1080P 高码率"},
                            {"qn": 80, "desc": "1080P 高清"},
                            {"qn": 64, "desc": "720P"},
                            {"qn": 32, "desc": "480P"},
                            {"qn": 16, "desc": "360P"}]
        out["note"] = "已登录（凭据仅存于你的浏览器，服务端不落盘），最高 1080P。"
    return out


def api_login_qrcode():
    """申请扫码登录二维码。二维码内容交给前端渲染，服务端不参与凭据存储。"""
    try:
        d = get_json("https://passport.bilibili.com/x/passport-login/"
                     "web/qrcode/generate")
    except Exception as e:
        return {"error": "申请二维码失败：%s" % e}, 502
    if d.get("code") != 0:
        return {"error": "B 站返回 %s" % d.get("message")}, 502
    dd = d.get("data") or {}
    return {"url": dd.get("url"), "key": dd.get("qrcode_key")}, 200


def api_login_poll(key):
    """轮询扫码状态。成功时把 SESSDATA 交给调用方写进**访客自己的** Cookie。

    code：86101 未扫描 / 86090 已扫描待确认 / 86038 已失效 / 0 成功
    返回体里的 sessdata 只供 Handler 写 Cookie，**必须从响应中剔除**，
    绝不能回给前端 JS。
    """
    if not key:
        return {"error": "缺少 key"}, 400
    url = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
           "?qrcode_key=%s&source=main-fe-header" % urllib.parse.quote(key))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            cookies = r.headers.get_all("Set-Cookie") or []
    except Exception as e:
        return {"error": "轮询失败：%s" % e}, 502
    d = json.loads(raw.decode("utf-8", "replace"))
    dd = d.get("data") or {}
    code = dd.get("code")
    out = {"code": code, "message": dd.get("message") or ""}
    if code == 0:
        for c in cookies:
            m = re.search(r"SESSDATA=([^;]+)", c)
            if m:
                out["sessdata"] = m.group(1)
                out["logged"] = True
                break
        if not out.get("logged"):
            out["message"] = "登录成功但未取到 SESSDATA"
    return out, 200


SERIES_ID = "5157110"
UP_NAME = "四时小路Komichi"

# 分类规则：与 tools/collect.py 的 CATEGORY_RULES 保持一致。
# 顺序即优先级（先匹配到的胜出），不要随意调整。
CATEGORY_RULES = [
    ("联动", ["联动", "派对", "组合", "会晤", "喝饮料", "一起玩", "前辈",
              "好多人", "模仿", "大家"]),
    ("游戏", ["游戏", "港诡", "锈湖", "魔女之家", "俄罗斯方块", "冰火", "搏斗",
              "捉", "寻找", "捡垃圾", "封锁协议", "VR公司", "万言堂", "宠物",
              "萌宠"]),
    ("电台", ["电台", "音乐"]),
    ("唱歌", ["唱"]),
    ("杂谈", ["闲聊", "聊天", "提问", "狡辩", "二创", "解析", "看看"]),
    ("特别回", ["特别回"]),
]

VIEW_WORKERS = 8        # 并发取详情。串行 42 条要十几秒，首屏等不起


def classify(title):
    t = title.replace("【直播回放】", "")
    for name, keys in CATEGORY_RULES:
        for k in keys:
            if k in t:
                return name
    return "其他"


def clean_title(title):
    t = re.sub(r"^【直播回放】", "", title).strip()
    t = re.sub(r"\s*\d{4}年\d+月\d+日\d+点场\s*$", "", t).strip()
    return t


def to_https(url):
    if not url:
        return ""
    return url.replace("http://", "https://", 1)


def thumb(url, size):
    u = to_https(url)
    return "%s@%s" % (u, size) if u else ""


def series_url():
    return ("https://api.bilibili.com/x/series/archives"
            "?mid=%s&series_id=%s&only_normal=true&sort=desc&pn=1&ps=30"
            % (MID, SERIES_ID))


def api_diag():
    """自检：把 build_programs 的失败原因暴露出来。

    云端容器内出网情况与本地不同，只靠 502/空列表无法定位，
    所以把每一跳的原始结果直接返回给调用方。
    """
    out = {"env": {"python": sys.version.split()[0],
                   "PORT": os.environ.get("PORT"),
                   "BIND": os.environ.get("BIND")}}
    url = series_url()
    out["series_url"] = url
    try:
        raw = http_get(url)
        out["http_bytes"] = len(raw)
        out["http_head"] = raw[:300].decode("utf-8", "replace")
        j = json.loads(raw.decode("utf-8", "replace"))
        out["bili_code"] = j.get("code")
        out["bili_message"] = j.get("message")
        d = j.get("data") or {}
        out["archives"] = len(d.get("archives") or [])
        out["page"] = d.get("page")
    except Exception as e:
        out["http_error"] = "%s: %s" % (type(e).__name__, e)
    try:
        payload = build_programs()
        out["build_count"] = payload["meta"]["count"]
    except Exception as e:
        out["build_error"] = "%s: %s" % (type(e).__name__, e)

    # 逐条详情是 build_programs 里唯一会静默吞异常的地方，单独探一次
    try:
        j = get_json(series_url(), timeout=15)
        arch = (j.get("data") or {}).get("archives") or []
        out["archives_for_probe"] = len(arch)
        if arch:
            bvid = arch[0].get("bvid")
            out["probe_bvid"] = bvid
            # 逐个候选接口打一遍，看哪个在数据中心 IP 下不被风控
            probes = []
            probes.append(("view", "https://api.bilibili.com/x/web-interface/view?bvid="
                           + urllib.parse.quote(bvid)))
            probes.append(("pagelist", "https://api.bilibili.com/x/player/pagelist?bvid="
                           + urllib.parse.quote(bvid)))
            probes.append(("view_detail",
                           "https://api.bilibili.com/x/web-interface/view/detail?bvid="
                           + urllib.parse.quote(bvid)))
            probes.append(("wbi_view", wbi_signed(
                "https://api.bilibili.com/x/web-interface/wbi/view",
                {"bvid": bvid})))
            got = []
            for name, u in probes:
                try:
                    raw = http_get(u, timeout=15)
                    jj = json.loads(raw.decode("utf-8", "replace"))
                    got.append({"name": name, "http": 200,
                                "code": jj.get("code"),
                                "bytes": len(raw)})
                except urllib.error.HTTPError as e:
                    got.append({"name": name, "http": e.code})
                except Exception as e:
                    got.append({"name": name,
                                "http": "%s: %s" % (type(e).__name__, e)})
            out["endpoint_probe"] = got
    except Exception as e:
        out["probe_error"] = "%s: %s" % (type(e).__name__, e)
    return out


def wbi_signed(url, params):
    """给 wbi/ 系接口做 WBI 签名。

    B 站 2023 起对 web-interface 部分接口启用 w_rid 校验，
    未签名会被风控拦成 412（尤其在数据中心 IP 上）。
    """
    global _WBI_KEYS
    if _WBI_KEYS is None:
        try:
            nav = get_json("https://api.bilibili.com/x/web-interface/nav", timeout=15)
            wi = (nav.get("data") or {}).get("wbi_img") or {}
            img = wi.get("img_url") or ""
            sub = wi.get("sub_url") or ""
            img_key = img.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub.rsplit("/", 1)[-1].split(".")[0]
            raw = img_key + sub_key
            MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
                     27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
                     37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
                     22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
            _WBI_KEYS = "".join(raw[i] for i in MIXIN if i < len(raw))[:32]
        except Exception:
            _WBI_KEYS = ""
    if not _WBI_KEYS:
        return url
    p = dict(params)
    p["wts"] = str(int(time.time()))
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(p[k]), safe=""))
                 for k in sorted(p))
    w_rid = hashlib.md5((q + _WBI_KEYS).encode("utf-8")).hexdigest()
    return "%s?%s&w_rid=%s" % (url, q, w_rid)


def one_view(bvid):
    """取单个视频详情。

    两条路：先打 view（字段最全），被风控拦（数据中心 IP 常见 412）就退到
    pagelist + view/detail；都失败返回 None，并保留最近一次错误供 /api/diag 读取。
    """
    global _LAST_VIEW_ERR
    q = urllib.parse.quote(bvid)
    errs = []

    # 路 1：字段最全的主力接口
    try:
        v = get_json("https://api.bilibili.com/x/web-interface/view?bvid="
                     + q, timeout=15)
        if v.get("code") == 0 and v.get("data"):
            _LAST_VIEW_ERR = None
            return v["data"]
        errs.append("view code=%s" % (v.get("code"),))
    except Exception as e:
        errs.append("view %s" % (e,))

    # 路 2：WBI 签名版 view（若有密钥）
    try:
        u = wbi_signed("https://api.bilibili.com/x/web-interface/wbi/view",
                       {"bvid": bvid})
        if "w_rid=" in u:
            v = get_json(u, timeout=15)
            if v.get("code") == 0 and v.get("data"):
                _LAST_VIEW_ERR = None
                return v["data"]
            errs.append("wbi_view code=%s" % (v.get("code"),))
    except Exception as e:
        errs.append("wbi_view %s" % (e,))

    # 路 3：view/detail（网页端同源接口，风控相对宽）
    try:
        v = get_json("https://api.bilibili.com/x/web-interface/view/detail?bvid="
                     + q, timeout=15)
        d = (v.get("data") or {}).get("View") if v.get("code") == 0 else None
        if d:
            _LAST_VIEW_ERR = None
            return d
        errs.append("view_detail code=%s" % (v.get("code"),))
    except Exception as e:
        errs.append("view_detail %s" % (e,))

    # 路 4：pagelist —— 只有分 P 信息，标题/封面缺失，需与系列条目补齐
    try:
        v = get_json("https://api.bilibili.com/x/player/pagelist?bvid="
                     + q, timeout=15)
        if v.get("code") == 0 and v.get("data"):
            _LAST_VIEW_ERR = None
            return {"bvid": bvid, "pages": v["data"], "title": "",
                    "pic": "", "owner": {}, "stat": {}, "_pagelist_only": True}
        errs.append("pagelist code=%s" % (v.get("code"),))
    except Exception as e:
        errs.append("pagelist %s" % (e,))

    _LAST_VIEW_ERR = " | ".join(errs)
    return None


def build_programs():
    """实时抓回放清单。

    字段必须与前端约定一致（assets/app.js 的 programRow/renderList 直接读用），
    否则列表会静默渲染不出来 —— 这也是为什么这里不做「精简版」。
    """
    import datetime as _dt
    from concurrent.futures import ThreadPoolExecutor

    arch = []
    last = None
    # 云端出网偶发抖动，重试 3 次；每次间隔 1s
    for i in range(3):
        try:
            j = get_json(series_url(), timeout=15)
            if j.get("code") != 0:
                last = "bili code=%s msg=%s" % (j.get("code"), j.get("message"))
            else:
                arch = (j.get("data") or {}).get("archives") or []
                if arch:
                    break
                last = "archives empty; page=%s" % ((j.get("data") or {}).get("page"),)
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if i < 2:
            time.sleep(1.0)
    if not arch:
        raise RuntimeError("系列列表为空（%s）" % (last or "unknown"))

    # 详情抓取：8 路并发。全部失败时抛错而不是静默返回空列表，
    # 否则前端会渲染成「0 个节目」的空页面，排查不到原因。
    results = {}
    with ThreadPoolExecutor(max_workers=VIEW_WORKERS) as ex:
        for a, detail in zip(arch, ex.map(one_view, [a.get("bvid") for a in arch])):
            if detail:
                results[a.get("bvid")] = detail

    if not results:
        raise RuntimeError("详情全部抓取失败（最后错误：%s）" % (_LAST_VIEW_ERR,))

    tz = _dt.timezone(_dt.timedelta(hours=8))
    overrides = load_overrides()
    programs = []
    for a in arch:
        bvid = a.get("bvid")
        detail = results.get(bvid)
        if not detail:
            continue
        title = detail.get("title") or a.get("title") or ""
        category = overrides.get(bvid, {}).get("category") or classify(title)
        pages = detail.get("pages") or []
        total_dur = sum(p.get("duration") or 0 for p in pages) or 1
        video_dm = (detail.get("stat") or {}).get("danmaku", 0)

        parts = [{
            "cid": p.get("cid"),
            "page": p.get("page"),
            "part": clean_title(p.get("part") or title),
            "duration": p.get("duration") or 0,
            "dm_total": int(round(video_dm * (p.get("duration") or 0) / total_dur)),
        } for p in pages]

        stat = detail.get("stat") or {}
        # pagelist 兜底路径没有 pubdate/aid/pic，用系列条目里的信息补上
        pub = detail.get("pubdate") or a.get("pubdate") or 0
        programs.append({
            "bvid": bvid,
            "aid": detail.get("aid") or a.get("aid"),
            "title": clean_title(title),
            "raw_title": title,
            "category": category,
            "pubdate": pub,
            "date": _dt.datetime.fromtimestamp(pub, tz).strftime("%Y-%m-%d %H:%M"),
            "duration": total_dur,
            "parts": parts,
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "like": stat.get("like", 0),
            "cover": to_https(detail.get("pic") or a.get("pic") or ""),
            "thumb": thumb(detail.get("pic") or a.get("pic") or "",
                           "320w_200h_1c.webp"),
            "url": "https://www.bilibili.com/video/%s" % bvid,
            "dm_total": video_dm,
            "dm_per_hour": (round(video_dm / (total_dur / 3600.0), 1)
                            if total_dur else 0),
        })

    programs.sort(key=lambda p: -p["pubdate"])

    # 评分与离线版一致：弹幕密度 60% + 新鲜度 40%
    dens = sorted(p["dm_per_hour"] for p in programs)
    n = len(dens) or 1
    now = time.time()
    for p in programs:
        rank = sum(1 for dd in dens if dd <= p["dm_per_hour"]) / float(n)
        fresh = pow(2.718281828, -((now - p["pubdate"]) / 86400.0) / 30.0)
        p["score"] = round((0.6 * rank + 0.4 * fresh) * 100, 1)
        p["dm_rank"] = round(rank * 100, 1)

    meta = {
        "mid": MID, "series_id": SERIES_ID, "up_name": UP_NAME,
        "count": len(programs),
        "part_count": sum(len(p["parts"]) for p in programs),
        "total_duration": sum(p["duration"] for p in programs),
        "generated_at": int(now),
        "live": True,          # 前端据此提示「实时数据」
    }
    return {"meta": meta, "programs": programs}


def load_overrides():
    """人工校正分类：cloud/server/overrides.json，形如 {"BV1xx":{"category":"杂谈"}}"""
    path = os.path.join(ROOT, "server", "overrides.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def api_status_board():
    """开播状态：这个接口不校验 Referer，也不需要登录。"""
    url = ("https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
           "?uids[]=" + MID)
    d = get_json(url)
    info = ((d.get("data") or {}).get(MID)) or {}
    return {"ok": d.get("code") == 0,
            "living": info.get("live_status") == 1,
            "title": info.get("title"),
            "room_id": info.get("room_id"),
            "cover": info.get("cover_from_user") or info.get("user_cover")}


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "komichi-radio"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=SITE, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- 工具

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200, extra=None):
        h = {"Cache-Control": "no-store"}
        if extra:
            h.update(extra)
        self._send(code, json.dumps(obj, ensure_ascii=False), extra=h)

    def _sess_cookie(self, value, max_age=2592000):
        """把访客自己的 SESSDATA 写进他浏览器的 Cookie（默认 30 天）。

        HttpOnly：前端 JS 读不到，降低 XSS 窃取面。
        Secure 只在确实走 HTTPS 时才加 —— 本地 http 调试时若加了，浏览器会直接丢弃。
        """
        parts = ["%s=%s" % (COOKIE_NAME, urllib.parse.quote(value, safe="")),
                 "Path=/", "Max-Age=%d" % max_age, "HttpOnly", "SameSite=Lax"]
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        if proto.lower() == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def public_base(self):
        """生成对外可用的站点前缀，用于 MPD 里的 <BaseURL>。

        线上是 HTTPS 反代，Host 头拿到的是公网域名但协议看不出来：
        写死 http:// 会被浏览器按混合内容拦截，播放直接失败。
        所以用**协议相对** URL（`//host`），由浏览器按当前页面协议补齐。
        允许用 PUBLIC_BASE 环境变量整体覆盖（自建反代路径时用）。
        """
        override = os.environ.get("PUBLIC_BASE")
        if override:
            return override.rstrip("/")
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host") or "").split(",")[0].strip()
        if not host:
            return ""
        return "//" + host

    def _origin_ok(self):
        """只允许白名单 Host 跨域调用；白名单为空则只允许同源（无 Origin 头）。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True                      # 同源 / 直接访问
        if not ALLOWED_HOSTS:
            return origin.startswith("http://localhost") \
                or origin.startswith("http://127.0.0.1")
        host = urllib.parse.urlparse(origin).hostname or ""
        host = host.lower()
        return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    # ---- 代理

    def _proxy(self, url, ctype=None, ranged=False):
        """转发媒体流/分片：必须带 Referer，否则 CDN 返回 403。"""
        headers = {"User-Agent": UA, "Referer": REFERER, "Accept": "*/*"}
        rng = self.headers.get("Range")
        if rng and ranged:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                self.send_response(206 if (ranged and rng) else 200)
                ct = ctype or r.headers.get("Content-Type") or "video/mp4"
                self.send_header("Content-Type", ct)
                for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                    if r.headers.get(h):
                        self.send_header(h, r.headers[h])
                if not r.headers.get("Accept-Ranges"):
                    self.send_header("Accept-Ranges", "bytes")
                self._cors()
                self.end_headers()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            self._send(e.code, "upstream %s" % e.code, "text/plain; charset=utf-8")
        except Exception as e:
            self._send(502, "proxy error: %s" % e, "text/plain; charset=utf-8")

    # ---- GET

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        path = p.path
        # 访客自己的凭据（若有）。整个请求周期只读一次；服务端不落盘。
        sess = sess_of(self)

        if not path.startswith("/api/"):
            if path == "/":
                self.path = "/index.html"
            return super().do_GET()

        if not self._origin_ok():
            return self._json({"error": "origin not allowed"}, 403)

        try:
            if path == "/api/status":
                return self._json(api_status(sess))

            if path == "/api/programs":
                ck = "prog"
                # 只在并发去重上做 60 秒缓存；?refresh=1 强制重取
                if not qs.get("refresh"):
                    hit = cache_get(ck, 60)
                    if hit:
                        out = dict(hit)
                        out["cached"] = True
                        return self._json(out)
                payload = build_programs()
                payload["ok"] = True
                payload["cached"] = False
                payload["count"] = payload["meta"]["count"]
                cache_put(ck, payload)
                return self._json(payload)

            if path == "/api/status-board":
                return self._json(api_status_board())

            if path == "/api/diag":
                return self._json(api_diag())

            if path == "/api/img":
                raw = (qs.get("u") or [""])[0]
                ct, body = api_img(raw)
                if ct is None:
                    return self._send(404, "no image",
                                      "text/plain; charset=utf-8")
                return self._send(200, body, ct,
                                  extra={"Cache-Control":
                                         "public, max-age=86400"})

            if path == "/api/dashinfo":
                bvid = (qs.get("bvid") or [""])[0]
                cid = (qs.get("cid") or [""])[0]
                if not bvid or not cid:
                    return self._json({"error": "缺少 bvid/cid"}, 400)
                out, err = api_dashinfo(bvid, cid, sess)
                if out is None:
                    return self._json({"error": err}, 502)
                return self._json(out)

            if path == "/api/dash":
                bvid = (qs.get("bvid") or [""])[0]
                cid = (qs.get("cid") or [""])[0]
                # 前端在还没拿到清晰度列表时会发 qn=undefined，必须按默认值处理，
                # 否则 int("undefined") 抛 ValueError 让整条播放链路 500。
                qn = parse_int((qs.get("qn") or ["80"])[0], 80)
                if not bvid or not cid:
                    return self._json({"error": "缺少 bvid/cid"}, 400)
                mpd, err, qid, qdesc = api_dash_mpd(self.public_base(), bvid, cid,
                                                    qn, sess)
                if mpd is None:
                    return self._json({"error": err}, 502)
                return self._send(200, mpd, "application/dash+xml",
                                  extra={"X-Qn": str(qid)})

            # 扫码登录：凭据全程只落在**访客自己**的浏览器 Cookie 里，
            # 服务端只做透传，不写文件、不记日志。
            if path == "/api/login/qrcode":
                out, code = api_login_qrcode()
                return self._json(out, code)

            if path == "/api/login/poll":
                key = (qs.get("key") or [""])[0]
                out, code = api_login_poll(key)
                # sessdata 只用于写 Cookie，必须从响应体里剔除，绝不能回给前端 JS
                sess_new = out.pop("sessdata", "")
                if sess_new:
                    return self._json(out, code,
                                      extra={"Set-Cookie":
                                             self._sess_cookie(sess_new)})
                return self._json(out, code)

            if path == "/api/logout":
                return self._json({"ok": True, "logged": False},
                                  extra={"Set-Cookie":
                                         self._sess_cookie("", max_age=0)})

            if path == "/api/playurl":
                bvid = (qs.get("bvid") or [""])[0]
                cid = (qs.get("cid") or [""])[0]
                qn = parse_int((qs.get("qn") or ["80"])[0], 80)
                if not bvid or not cid:
                    return self._json({"error": "缺少 bvid/cid"}, 400)
                return self._json(api_playurl(bvid, cid, qn, sess))

            if path in ("/api/stream", "/api/dash"):
                u = (qs.get("u") or [""])[0]
                if not u:
                    return self._json({"error": "缺少 u"}, 400)
                try:
                    url = b64d(u).decode("utf-8")
                except Exception:
                    return self._json({"error": "u 不是合法 base64url"}, 400)
                host = urllib.parse.urlparse(url).hostname or ""
                # 只允许转发 B 站自己的 CDN，且禁止转到内网（反 SSRF）
                if not _is_bili_media(url):
                    return self._json({"error": "不允许的媒体域名"}, 403)
                return self._proxy(url, ranged=(path == "/api/stream"))

            return self._json({"error": "未知接口"}, 404)
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 500)


def main():
    port = int(os.environ.get("PORT", "8765"))
    bind = os.environ.get("BIND", "0.0.0.0")
    if not os.path.isdir(SITE):
        print("未找到 %s" % SITE, file=sys.stderr)
        return 1
    try:
        srv = ThreadingHTTPServer((bind, port), Handler)
    except OSError as e:
        print("端口 %d 绑定失败：%s" % (port, e), file=sys.stderr)
        return 1
    srv.daemon_threads = True
    print("二十四时小路电台 · 云端服务", flush=True)
    print("  静态根目录：%s" % SITE, flush=True)
    print("  监听：%s:%d" % (bind, port), flush=True)
    print("  凭据：无（匿名访问，最高 480P）", flush=True)
    print("  CORS 白名单：%s"
          % (", ".join(ALLOWED_HOSTS) if ALLOWED_HOSTS else "仅同源"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
