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
import http.client
import html
import json
import os
import queue
import re
import shutil
import socket
import ssl
import struct
import subprocess
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


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d
    except Exception:
        return None


STATIONS_VERSION = 2       # v2：series_id 从「必填」改成「可选覆盖」（默认自动发现）


def _merge_stations_file(path, old):
    """重新释放网页时保住用户自定义的主播（stations.json 是用户的配置，不是发布物）。

    规则：以**内置这份为底**（新加的主播/字段能到位），再把用户那份按 id 盖上去。
    用户自己加的主播一定保留 —— 一次升级不该抹掉用户配置。

    **但「用户那份」不等于「用户改过」**：升级时它往往只是上一版打包出来的默认值。
    v2 起 series_id 从「必填」改成「可选覆盖」，上一版留在用户机上的 5157110 就是这种
    陈旧默认 —— 照「用户优先」保住它，主站会一直显示「来源：手填」而拿不到自动发现。
    所以按文件里的 _version 区分：
      · _version >= 2（用户在新语义下写过）：逐字段用户优先
      · 更早（含没有版本号的）：内置那份**整体**生效（不吃陈旧默认），
        只额外保留用户自己加进来的主播
    """
    olds = (old or {}).get("stations") if isinstance(old, dict) else None
    if not isinstance(olds, list) or not olds:
        return
    old_ver = 0
    try:
        old_ver = int((old or {}).get("_version") or 0)
    except (TypeError, ValueError):
        old_ver = 0
    new = _read_json_file(path)
    if not isinstance(new, dict):
        return
    base = [x for x in (new.get("stations") or []) if isinstance(x, dict) and x.get("id")]
    by_id = dict((x["id"], x) for x in base)
    order = [x["id"] for x in base]
    for u in olds:
        if not isinstance(u, dict) or not u.get("id"):
            continue
        if u["id"] in by_id:
            if old_ver >= STATIONS_VERSION:
                m = dict(by_id[u["id"]])
                m.update(u)                # 新语义下用户改过：用户的值优先
                by_id[u["id"]] = m
            # 旧版本：内置那份生效（见上面注释）
        else:
            by_id[u["id"]] = u             # 用户自己加的主播，任何时候都留
            order.append(u["id"])
    out = dict(new)
    out["stations"] = [by_id[i] for i in order]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _merge_series_cache_file(path, old):
    """回放来源缓存同样保住：用户机上已经发现过的结果比内置快照新，省一次接口往返。"""
    if not isinstance(old, dict) or not old:
        return
    new = _read_json_file(path)
    if not isinstance(new, dict):
        new = {}
    new.update(old)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


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
        old_st = _read_json_file(os.path.join(dst, "data", "stations.json"))
        old_sc = _read_json_file(os.path.join(dst, "data", "series_cache.json"))
        for name in WEB_ITEMS:
            s = os.path.join(src, name)
            d = os.path.join(dst, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)   # 覆盖式，不先删目录
            elif os.path.exists(s):
                shutil.copy2(s, d)
        if old_seg:
            _merge_segments_file(os.path.join(dst, "data", "segments.js"), old_seg)
        # 用户自定义的主播 / 已发现的回放系列同样不能被覆盖掉
        _merge_stations_file(os.path.join(dst, "data", "stations.json"), old_st)
        _merge_series_cache_file(os.path.join(dst, "data", "series_cache.json"), old_sc)
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

# ---------------------------------------------------------------- 主播注册表
# 「看谁的内容」由 data/stations.json 决定，改文件即可，不需要改代码。
# 请求带 ?station=<id|mid|房间号> 时，当前线程就切到那一位；不带则用主站。
STATIONS_FILE = os.path.join(ROOT, "data", "stations.json")
_STATIONS = {"at": 0.0, "data": None}
_STATIONS_LOCK = threading.Lock()
_CUR = threading.local()


def load_stations(force=False):
    """读注册表（30 秒缓存）。文件缺失或损坏时退回只含主站的内置表，保证永远可用。"""
    with _STATIONS_LOCK:
        if not force and _STATIONS["data"] and time.time() - _STATIONS["at"] < 30:
            return _STATIONS["data"]
    out = []
    try:
        with open(STATIONS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        items = raw.get("stations") if isinstance(raw, dict) else raw
        out = [s for s in (items or []) if isinstance(s, dict) and s.get("id")]
    except Exception:
        out = []
    if not out:
        out = [{"id": "komichi", "name": "四时小路Komichi", "short": "四时小路",
                "mid": MID, "room": ROOM_ID, "series_id": SERIES_ID,
                "tag": "", "site": "", "accent": "#ff3b4e", "main": True}]
    if not any(s.get("main") for s in out):
        out[0]["main"] = True
    for s in out:
        s.setdefault("series_id", "")
        s.setdefault("short", s.get("name") or s["id"])
    with _STATIONS_LOCK:
        _STATIONS["data"] = out
        _STATIONS["at"] = time.time()
    return out


def main_station():
    for s in load_stations():
        if s.get("main"):
            return s
    return load_stations()[0]


def find_station(key):
    """key 可以是 id / mid / 房间号；找不到（或空）返回 None。"""
    key = str(key or "").strip()
    if not key:
        return None
    for s in load_stations():
        if key in (str(s.get("id")), str(s.get("mid")), str(s.get("room"))):
            return s
    return None


def use_station(key):
    """把当前请求绑定到某位主播（HTTP 线程入口调用）。"""
    _CUR.station = find_station(key)


def cur_station():
    return getattr(_CUR, "station", None) or main_station()


def cur_mid():
    return str(cur_station().get("mid") or MID)


def cur_room():
    return str(cur_station().get("room") or ROOM_ID)


# ---- 回放来源：从「主页 → 合集和系列 → 系列」自动发现 ------------------------
# 端点是空间页自己发的那个请求：
#   GET https://api.bilibili.com/x/polymer/web-space/seasons_series_list?mid=<mid>
# 注意路径是 web-space（连字符）。写成 /x/polymer/web/space/... 会 404 ——
# 这就是最初死活找不到的原因。另：x/series/archives 只能「已知 series_id 后取归档」，
# 不能用来列出系列，所以顺序必须是「先列系列 → 再取归档」。
SERIES_CACHE_FILE = os.path.join(ROOT, "data", "series_cache.json")
_SERIES_LOCK = threading.Lock()
_SERIES_WARM = {"done": False, "running": False}
_SERIES_WARM_LOCK = threading.Lock()
SERIES_OK_TTL = 24 * 3600     # 发现成功：一天内不重复打接口
SERIES_FAIL_TTL = 600         # 发现失败：10 分钟后再试，别把风控惹急


def _series_cache_read():
    try:
        with open(SERIES_CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _series_cache_write(d):
    # 直接覆盖写，不用「写临时文件再原子替换」：本机把删除类操作劫持到回收站且 fail-closed，
    # 替换有可能失败；这个缓存很小，直接写足够。
    try:
        os.makedirs(os.path.dirname(SERIES_CACHE_FILE), exist_ok=True)
        with open(SERIES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def discover_series(mid):
    """列出一个 UP 的「合集和系列」，挑出直播回放那个系列。

    返回 {series_id, name, total, kind, candidates}；一个系列都没有时抛异常。
    """
    mid = str(mid or "").strip()
    if not mid:
        raise RuntimeError("缺少 UID")
    url = ("https://api.bilibili.com/x/polymer/web-space/seasons_series_list"
           "?mid=%s&page_size=20&page_num=1&web_location=333.1387" % mid)
    d = bili_get(url, "https://space.bilibili.com/%s/lists" % mid)
    if d.get("code") != 0:
        raise RuntimeError("系列列表接口 code=%s %s" % (d.get("code"), d.get("message")))
    items = (d.get("data") or {}).get("items_lists") or {}
    cands = []
    for kind, key in (("series", "series_list"), ("season", "seasons_list")):
        for it in (items.get(key) or []):
            if not isinstance(it, dict):
                continue
            meta = it.get("meta") or it
            sid = meta.get("series_id") or meta.get("season_id") or it.get("id")
            if not sid:
                continue
            cands.append({"id": str(sid),
                          "name": str(meta.get("name") or it.get("name") or ""),
                          "total": int(meta.get("total") or it.get("total") or 0),
                          "kind": kind})
    if not cands:
        raise RuntimeError("这位主播的「合集和系列」里还没有系列")
    # 站上默认就叫「直播回放」；没有同名的就取条数最多的那个（最像回放合集）
    replay = [c for c in cands if "回放" in c["name"]]
    pick = max(replay or cands, key=lambda c: c["total"])
    return {"series_id": pick["id"], "name": pick["name"], "total": pick["total"],
            "kind": pick["kind"], "candidates": cands}


def resolve_series(st=None, allow_network=False, force=False):
    """决定当前主播「用哪个系列当回放来源」。

    优先级：stations.json 里手填的 series_id（可选的强制覆盖）> 自动发现（带缓存）。
    自动发现失败不抛异常 —— 结果里带 error，页面才能说清原因而不是留一片空白。
    已缓存到 id 时即使超过 TTL 也继续用它（只是顺带重试）：一次网络抖动不该把频道清空。
    """
    st = st or cur_station()
    conf = str(st.get("series_id") or "").strip()
    if conf:
        return {"series_id": conf, "source": "config", "name": "", "total": 0,
                "kind": "", "at": 0, "error": ""}
    mid = str(st.get("mid") or "")
    with _SERIES_LOCK:
        cache = _series_cache_read()
    hit = cache.get(mid) or {}
    old = str(hit.get("series_id") or "")
    ttl = SERIES_OK_TTL if old else SERIES_FAIL_TTL
    fresh = bool(hit) and (time.time() - float(hit.get("at") or 0)) < ttl
    if (fresh and not force) or not allow_network:
        return {"series_id": old, "source": "auto" if old else "pending",
                "name": hit.get("name") or "", "total": int(hit.get("total") or 0),
                "kind": hit.get("kind") or "", "at": hit.get("at") or 0,
                "error": "" if (old or fresh) else (hit.get("error") or "")}
    try:
        r = discover_series(mid)
        rec = {"series_id": r["series_id"], "name": r["name"], "total": r["total"],
               "kind": r["kind"], "at": time.time(), "error": ""}
    except Exception as e:
        # 失败时保留旧值（如果有）：宁可继续用上一次发现的系列，也不要让频道空掉
        rec = {"series_id": old, "name": hit.get("name") or "",
               "total": int(hit.get("total") or 0), "kind": hit.get("kind") or "",
               "at": hit.get("at") or 0, "error": str(e)}
    with _SERIES_LOCK:
        cache = _series_cache_read()
        cache[mid] = rec
        _series_cache_write(cache)
    return {"series_id": rec["series_id"], "source": "auto" if rec["series_id"] else "pending",
            "name": rec.get("name") or "", "total": int(rec.get("total") or 0),
            "kind": rec.get("kind") or "", "at": rec.get("at") or 0,
            "error": rec.get("error") or ""}


def cur_series():
    """当前主播的回放系列 ID。这条路径只读缓存，不打接口（请求线程里不做网络往返）。"""
    return resolve_series(cur_station(), allow_network=False)["series_id"]


# ---------------------------------------------------------------- 主播管理（网页里增删）
#
# 之前想换主播只能手改 data/stations.json。这里给出写接口，页面设置页直接调。
# 两个约束（都是踩过的）：
#   · 写盘必须带 "_version": 2 —— 升级时的 _merge_stations_file 靠它判断
#     「用户改过」还是「上一版默认值」，版本号不到位用户加的主播会被内置值盖掉。
#   · 整份覆盖写，不用「临时文件 + os.replace」—— 本机删除被劫持到回收站且 fail-closed。

STATION_FIELDS = ("name", "short", "room", "series_id", "weibo", "accent", "site", "tag")
MAX_STATIONS = 50
STATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _stations_doc_read():
    try:
        with open(STATIONS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    if not isinstance(raw.get("stations"), list):
        raw["stations"] = []
    return raw


def _stations_doc_write(doc):
    os.makedirs(os.path.dirname(STATIONS_FILE), exist_ok=True)
    with open(STATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")       # 补个结尾换行，不然每次保存都会在 diff 里多出这一行噪音


def _station_field(k, v):
    """白名单字段 + 长度/格式收敛。绝不能 dict.update(用户输入)。"""
    v = "" if v is None else str(v).strip()
    if k in ("name", "short", "site", "tag"):
        return v[:128]
    if k == "accent":
        if v and not re.match(r"^#[0-9a-fA-F]{3,8}$", v):
            raise ValueError("主题色要写成 #rrggbb 这样的十六进制")
        return v
    if k in ("weibo", "room", "series_id"):
        if v and not v.isdigit():
            raise ValueError({"weibo": "微博 UID", "room": "房间号",
                              "series_id": "系列 ID"}[k] + "只要数字")
        return v
    return v


def probe_mid(mid):
    """按 UID 查主播名 / 房间号 / 可用的回放来源（只查不写）。

    来源列表直接复用 discover_series()，不另写一套发现逻辑 ——
    「哪个系列算回放」的判断只有一份，才不会出现两处挑出不同结果。
    """
    mid = str(mid or "").strip()
    if not mid.isdigit():
        raise RuntimeError("UID 必须是纯数字（B 站空间号：个人主页网址"
                           " space.bilibili.com/<数字> 里那串数字）")
    live = bili_get("https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
                    "?uids[]=%s" % mid, "https://live.bilibili.com/")
    if live.get("code") != 0:
        raise RuntimeError("查主播接口 code=%s %s" % (live.get("code"), live.get("message")))
    info = ((live.get("data") or {}).get(mid)) or {}
    if not info:
        raise RuntimeError("没查到 UID %s：请确认是 B 站空间号（个人主页网址里的数字）" % mid)

    cands, err, suggested = [], "", ""
    try:
        found = discover_series(mid)
        # 归档只支持「系列」（x/series/archives）；合集要走另一个接口，
        # 而且 season_id 非法时那个接口会返回与本人无关的固定数据 —— 所以不给用户选。
        cands = [{"id": c["id"], "name": c["name"], "total": c["total"]}
                 for c in (found.get("candidates") or []) if c.get("kind") == "series"]
        if found.get("kind") == "series":
            suggested = found.get("series_id") or ""
        elif cands:
            suggested = max(cands, key=lambda c: c["total"])["id"]
        if not cands:
            err = "这位的「合集和系列」里还没有可用的系列（只有合集不行，需要是「系列」）"
    except Exception as e:
        err = str(e)
    return {"mid": mid, "name": str(info.get("uname") or "").strip(),
            "room": str(info.get("room_id") or ""),
            "living": int(info.get("live_status") or 0) == 1,
            "sources": cands, "suggested": suggested, "error": err}


def _stations_payload():
    """给前端的精简列表（不含内部字段）。"""
    return [{"id": s.get("id"), "name": s.get("name"), "short": s.get("short"),
             "mid": s.get("mid"), "room": s.get("room"), "main": bool(s.get("main")),
             "accent": s.get("accent") or "", "weibo": s.get("weibo") or "",
             "series_id": s.get("series_id") or ""}
            for s in load_stations(force=True)]


def api_stations_probe(obj):
    try:
        return 200, {"ok": True, "probe": probe_mid(obj.get("mid"))}
    except Exception as e:
        return 502, {"error": str(e)}


def api_stations_save(obj):
    """新增或更新一位主播。id 为空时按 mid 生成（mid 是纯数字，天然 ASCII 安全且唯一）。"""
    mid = str(obj.get("mid") or "").strip()
    if not mid.isdigit():
        return 400, {"error": "UID 必须是纯数字（B 站空间号：个人主页网址里的数字）"}
    sid = str(obj.get("id") or "").strip() or mid
    if not STATION_ID_RE.match(sid):
        return 400, {"error": "标识只能用字母 / 数字 / _ / -（它同时是数据目录名）"}
    try:
        fields = dict((k, _station_field(k, obj.get(k))) for k in STATION_FIELDS if k in obj)
    except ValueError as e:
        return 400, {"error": str(e)}

    doc = _stations_doc_read()
    items = [s for s in doc["stations"] if isinstance(s, dict)]
    cur = None
    for s in items:
        if str(s.get("id")) == sid:
            cur = s
            break
    if cur is None:                       # 同一个 UID 换个 id 来存 → 还是更新原来那位
        for s in items:
            if str(s.get("mid")) == mid:
                cur = s
                break
    if cur is None:
        if len(items) >= MAX_STATIONS:
            return 400, {"error": "最多 %d 位主播" % MAX_STATIONS}
        # 新增先探测：UID 不存在就别写进去（否则会留一个"主播 123"的空壳板块）。
        # 更新时不再探测 —— 改个主题色不该因为对方接口抖动就失败。
        try:
            p = probe_mid(mid)
        except Exception as e:
            return 502, {"error": str(e)}
        base_name = p["name"] or ("主播 " + mid)
        cur = {"id": sid, "mid": mid, "main": False,
               "name": base_name, "short": base_name[:6], "room": p["room"]}
        items.append(cur)
    cur["id"] = sid
    cur["mid"] = mid
    cur.update(fields)
    if not cur.get("name"):
        cur["name"] = "主播 " + mid
    if not cur.get("short"):
        cur["short"] = str(cur["name"])[:6]
    doc["stations"] = items
    doc["_version"] = STATIONS_VERSION     # 没有它，下次释放网页时这一位会被内置值盖掉
    try:
        _stations_doc_write(doc)
    except OSError as e:
        return 500, {"error": "写 data/stations.json 失败：%s" % e}
    # load_stations 有 30 秒缓存：不主动失效，用户得等半分钟才看到新主播，
    # 会以为保存失败了。
    return 200, {"ok": True, "id": sid, "stations": _stations_payload()}


def api_stations_delete(obj):
    sid = str(obj.get("id") or "").strip()
    if not sid:
        return 400, {"error": "缺少 id"}
    doc = _stations_doc_read()
    items = [s for s in doc["stations"] if isinstance(s, dict)]
    hit = [s for s in items if str(s.get("id")) == sid]
    if not hit:
        return 400, {"error": "没有这位主播"}
    if hit[0].get("main"):
        return 400, {"error": "主站不能删除"}
    if len(items) <= 1:
        return 400, {"error": "至少要留一位主播"}
    doc["stations"] = [s for s in items if str(s.get("id")) != sid]
    doc["_version"] = STATIONS_VERSION
    try:
        _stations_doc_write(doc)
    except OSError as e:
        return 500, {"error": "写 data/stations.json 失败：%s" % e}
    # 不清理 data/stations/<id>/ 里的缓存：本机删除被劫持到回收站且 fail-closed，
    # 而那些缓存留着完全无害。
    return 200, {"ok": True, "id": sid, "stations": _stations_payload()}


def _series_warmup():
    """后台逐个主播发现回放系列（错开间隔，避免触发风控 412）。

    新用户只填 name + UID 时，回放清单全靠这一步补齐。
    """
    try:
        time.sleep(1.5)                     # 让启动流程先走完
        for st in load_stations():
            if str(st.get("series_id") or "").strip():
                continue                    # 手填过就尊重它，不去打扰接口
            try:
                r = resolve_series(st, allow_network=True)
                print("[series] %-8s → %s  %s" % (
                    st.get("short") or st.get("id"), r["series_id"] or "(未发现)",
                    r.get("error") or ("%s / %s 场" % (r.get("name") or "", r.get("total") or 0))))
            except Exception as e:
                print("[series] %s 失败：%s" % (st.get("id"), e))
            time.sleep(4.0)
    finally:
        with _SERIES_WARM_LOCK:
            _SERIES_WARM["running"] = False
            _SERIES_WARM["done"] = True


def series_kick():
    """确保「发现各主播回放系列」跑过（幂等，可反复调用）。"""
    with _SERIES_WARM_LOCK:
        if _SERIES_WARM["running"]:
            return False
        _SERIES_WARM["running"] = True
    threading.Thread(target=_series_warmup, daemon=True).start()
    return True


def api_series_status():
    """GET /api/series —— 当前主播的回放来源（含是自动发现的还是手填的）。

    字段名与 station_head()["series"] 保持一致（用 id 而不是 series_id）——
    前端两处都在读 .id，键名不统一会让它走到「还没拿到」的分支。
    """
    st = cur_station()
    rs = resolve_series(st, allow_network=False)
    return 200, {"ok": bool(rs["series_id"]), "station": station_head(st),
                 "series": {"id": rs["series_id"], "series_id": rs["series_id"],
                            "source": rs["source"], "name": rs.get("name") or "",
                            "total": rs.get("total") or 0, "kind": rs.get("kind") or "",
                            "at": rs.get("at") or 0, "error": rs.get("error") or ""},
                 "cache_file": SERIES_CACHE_FILE}


def api_series_refresh(body=None):
    """POST /api/series/refresh —— 强制重新发现（改了 UID 或换了系列之后用）。

    不带参数只刷当前主播；body 里 all=true 则刷全部（每个之间留间隔防风控）。
    """
    body = body or {}
    every = str(body.get("all") or "").lower() in ("1", "true", "yes")
    targets = load_stations() if every else [cur_station()]
    out = []
    for st in targets:
        conf = str(st.get("series_id") or "").strip()
        if conf:
            out.append({"id": st.get("id"), "series_id": conf, "source": "config",
                        "error": "", "note": "stations.json 里已手填，未走自动发现"})
            continue
        r = resolve_series(st, allow_network=True, force=True)
        out.append({"id": st.get("id"), "series_id": r["series_id"], "name": r.get("name") or "",
                    "total": r.get("total") or 0, "source": r["source"],
                    "error": r.get("error") or ""})
        if every:
            time.sleep(4.0)
    return 200, {"ok": all(not x.get("error") for x in out), "results": out}


def station_head(st=None):
    """给前端用的主播摘要（不含任何敏感信息）。"""
    st = st or cur_station()
    rs = resolve_series(st, allow_network=False)
    return {"id": st.get("id"), "name": st.get("name"), "short": st.get("short"),
            "mid": st.get("mid"), "room": st.get("room"),
            "has_programs": bool(rs["series_id"]), "tag": st.get("tag") or "",
            "site": st.get("site") or "", "accent": st.get("accent") or "",
            "main": bool(st.get("main")),
            "weibo": str(st.get("weibo") or ""),
            # 回放来源：auto=自动发现 / config=stations.json 手填 / pending=还没拿到
            "series": {"id": rs["series_id"], "source": rs["source"],
                       "name": rs.get("name") or "", "total": rs.get("total") or 0,
                       "error": rs.get("error") or ""}}


def station_dir(st=None):
    """数据目录：主站沿用 data/（与历史数据完全兼容），副站放 data/stations/<id>/。"""
    st = st or cur_station()
    if st.get("main"):
        return os.path.join(ROOT, "data")
    return os.path.join(ROOT, "data", "stations", str(st.get("id")))


def station_segments_js(key):
    """按主播取 segments.js 内容；主站返回 None（继续走静态文件，行为不变）。

    index.html 里那条 <script src="data/segments.js"> 是写死的，只能拿到主站那份。
    副站的分段在各自目录（data/stations/<id>/segments.js），前端切换主播后
    会用 ?station= 再取一次覆盖 window.SEGMENTS —— 不覆盖的话，频道会拿主站的
    cid 去匹配副站的分P，一个都对不上，等于这位主播没有分段。
    """
    st = find_station(key)
    if not st or st.get("main"):
        return None
    try:
        with open(os.path.join(station_dir(st), "segments.js"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("/* %s 还没有分段数据（在设置页点「给最新一期分段」开始积累） */\n"
                "window.SEGMENTS = {};\n" % st.get("id"))


def api_stations():
    """GET /api/stations —— 主播列表（含各自是否有回放清单）。"""
    series_kick()          # 还没发现过就在后台补，接口本身不等它
    return 200, {"ok": True,
                 "stations": [station_head(s) for s in load_stations()],
                 "main": (main_station() or {}).get("id")}



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


def api_status(force=False):
    """本机登录态。带 ?refresh=1 时绕过缓存（登录/登出之后必须拿到新结果）。"""
    if not force:
        with _STATUS_LOCK:
            hit = _STATUS_CACHE["data"]
            if hit and (time.time() - _STATUS_CACHE["at"]) < _STATUS_TTL:
                return 200, dict(hit)
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
    with _STATUS_LOCK:
        _STATUS_CACHE["data"] = dict(info)
        _STATUS_CACHE["at"] = time.time()
    return 200, info


# ---------------------------------------------------------------- 到 CDN 的连接复用
#
# 实测（到 upos-sz-*.bilivideo.com，取 256KB）：**新建连接 TTFB ≈57ms，复用 ≈12ms**，
# 也就是每个请求白付约 44ms 的 TCP+TLS 握手钱。而缩略图和媒体分片都是密集小请求
# （一屏十几张图、一段视频几百个分片），这笔钱加起来很显眼。
# 所以按 host 在**线程内**复用连接 —— http.client 的连接不是线程安全的。
# 池必须**跨线程共享**：ThreadingHTTPServer 每个请求新起一个线程，
# 按线程存的池永远不会命中（第一版就是这么写的，等于白做）。
# 连接本身不是线程安全的，所以用「借用 → 用完归还」加锁管理。
_POOL = {}
_POOL_LOCK = threading.Lock()
_POOL_MAX = 8                   # 每个 host 最多留几条空闲连接
_POOL_CTX = ssl.create_default_context()


def _borrow(host, port):
    key = (host, port)
    with _POOL_LOCK:
        lst = _POOL.get(key)
        if lst:
            return key, lst.pop()
    return key, http.client.HTTPSConnection(host, port, timeout=40, context=_POOL_CTX)


def _give_back(key, conn):
    with _POOL_LOCK:
        lst = _POOL.setdefault(key, [])
        if len(lst) < _POOL_MAX:
            lst.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass


class _Cached(object):
    """一次「借用连接 → 请求 → 归位」。

    响应**读到底**（isclosed）才把连接放回池；出错或没读完就丢掉 ——
    没读完的连接留着会被下一个请求撞上，那才是真正的坑。
    """

    def __init__(self, key, conn, resp):
        self.key, self.conn, self.resp = key, conn, resp

    def __enter__(self):
        return self.resp

    def __exit__(self, exc_type, exc, tb):
        if self.conn is not None:
            if exc_type is None and self.resp.isclosed():
                _give_back(self.key, self.conn)
            else:
                try:
                    self.conn.close()
                except Exception:
                    pass
        return False


def _prefetch(target):
    """提前「碰一下」这个轨道文件，把 CDN 那次回源先启动起来。

    实测（同一批**还没被访问过**的文件，成对 A/B）：先碰一下、紧接着发正式请求，
    首分片 TTFB 中位数 **206ms**（三次数值 183/207/312，很稳）；
    不碰则是 **703ms**（最坏 1555ms）。差的就是 CDN 对该文件的首次回源 ——
    碰一下让它提前开始，正式请求能搭上同一趟。

    只取 2 字节：够触发回源，又几乎不占带宽，不会和真正要用的请求抢。

    注：早先我判过「这个没用」——那次是拿**已经播放过、早就热了**的文件测的，
    自然看不出差别。要验证它必须用没访问过的文件（列表靠后随便挑）。
    """
    try:
        u = urllib.parse.urlsplit(target)
        if u.scheme != "https" or not u.hostname:
            return
        hdrs = {"User-Agent": UA, "Referer": REFERER, "Accept": "*/*",
                "Range": "bytes=0-1"}
        with cached_get(target, hdrs, 20) as r:
            r.read(64)              # 读掉这 2 个字节，连接才能干净地回到池里
    except Exception:
        pass


def cached_get(target, headers, timeout=40):
    """复用连接的 GET，配合 `with` 使用。

    复用失败（连接被对端关了等）就退回原来的 urlopen（新建连接 + 系统代理回落）——
    那条回落路径是 2026-09-27 代理故障后加的，不能因为这次优化丢掉。
    """
    try:
        u = urllib.parse.urlsplit(target)
    except ValueError:
        u = None
    if not u or u.scheme != "https" or not u.hostname:
        return _Cached(None, None,
                       urlopen(urllib.request.Request(target, headers=headers), timeout))
    key, conn = _borrow(u.hostname, u.port or 443)
    try:
        path = u.path + (("?" + u.query) if u.query else "")
        conn.request("GET", path, headers=headers)
        return _Cached(key, conn, conn.getresponse())
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return _Cached(None, None,
                       urlopen(urllib.request.Request(target, headers=headers), timeout))


IMG_CACHE = {}          # 缩略图内存缓存：url → (content_type, bytes)

# ---------------------------------------------------------------- 代取目标白名单
#
# /api/stream 与 /api/img 的目标地址来自 URL 参数（base64url）。原先只校验
# `startswith("http")`，等于开了个「任意 URL 代理」；而 SESSDATA 又无条件附带 ——
# 于是任意本机页面插一个 <img src="…/api/stream?u=<attacker>">，本机服务就会带着
# 用户的 B 站会话凭据去请求 attacker 的主机（服务只绑 127.0.0.1，所以是
# 「本机任意页面可触发」，不是互联网可触发，但正常使用路径下确实可利用）。
# 所以这里改成**主机白名单**：只放行 B 站自己的媒体/图片域名。
# 清单来自实测：播放地址 = upos-sz-*.bilivideo.com，缩略图 = i*.hdslb.com，
# 微博图床 = *.sinaimg.cn（不凭印象写，写窄了会打断播放）。
MEDIA_HOSTS = ("hdslb.com", "bilivideo.com", "bilibili.com", "akamaized.net",
               "szbdyd.com", "sinaimg.cn", "weibo.com")
# SESSDATA 只跟着发给 B 站系域：CDN 不需要用户身份，带上等于把凭据交出去。
BILI_HOSTS = ("bilibili.com", "hdslb.com", "bilivideo.com", "szbdyd.com")
MAX_PROXY_BYTES = 8 * 1024 * 1024     # 代取图片的上限，防止一个 URL 拖爆内存


def _safe_target(raw):
    """解出 base64url 里的目标地址；不是 https 或主机不在白名单内 → 返回空串。"""
    if not raw:
        return ""
    pad = "=" * (-len(raw) % 4)
    try:
        t = base64.urlsafe_b64decode(raw + pad).decode("utf-8")
    except Exception:
        return ""
    try:
        u = urllib.parse.urlsplit(t)
    except ValueError:
        return ""
    if u.scheme != "https" or not u.hostname:
        return ""
    h = u.hostname.lower()
    if not any(h == d or h.endswith("." + d) for d in MEDIA_HOSTS):
        return ""
    return t


def _is_bili_host(target):
    """目标是不是 B 站自己的域（决定要不要带登录 Cookie）。"""
    try:
        h = (urllib.parse.urlsplit(target).hostname or "").lower()
    except ValueError:
        return False
    return any(h == d or h.endswith("." + d) for d in BILI_HOSTS)


def api_img(raw):
    """代取缩略图。

    浏览器直连 i2.hdslb.com 在部分网络下会失败，而本机服务能取到 —— 所以让浏览器只跟 127.0.0.1 通信，图片由这里代取并缓存。
    """
    target = _safe_target(raw)
    if not target:
        return 400, None, None

    if target in IMG_CACHE:
        ct, body = IMG_CACHE[target]
        return 200, ct, body

    # 微博图床（sinaimg.cn）不认 B 站 Referer，按域名换一个
    ref = "https://weibo.com/" if "sinaimg.cn" in target else REFERER
    try:
        # 没读完（超长响应）时上下文管理器自己会把连接丢掉，不用额外处理
        with cached_get(target, {"User-Agent": UA, "Referer": ref,
                                 "Accept": "image/*,*/*"}, 20) as r:
            ct = r.headers.get("Content-Type") or "image/jpeg"
            body = r.read(MAX_PROXY_BYTES)
    except Exception:
        return 502, None, None

    # 上游给什么类型就回什么类型的话，一个返回 text/html 的目标会在**本机源**上
    # 渲染 HTML（同源即可调用全部 /api/*）。图片代理只回图片类型。
    if not ct.lower().startswith("image/"):
        ct = "image/jpeg"

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
    ladder = pick_video_ladder(data, qn)
    v = ladder[-1] if ladder else None
    return v, pick_audio(data)


def pick_audio(data):
    """音频取最高码率那条 —— 音轨的数据量比视频小两个量级，不值得为起播牺牲音质。"""
    auds = (data.get("dash") or {}).get("audio") or []
    return max(auds, key=lambda x: x.get("bandwidth", 0)) if auds else None


def pick_video_ladder(data, qn):
    """返回**整条清晰度阶梯**（从小到大），而不是只挑最高的那一档。

    只写一档时 dash.js 只能拿它起播：用户选 1080P，起播就得先把一个 1080P 分片
    （500KB~1MB）下完才出画面。把 ≤ qn 的档都写进 mpd 之后，dash.js 会按自己的
    ABR 从**最低档**起播、再往上抬 —— 出画面快得多，而 qn 依然是天花板
    （用户选了 480P 就不会拿到 1080P）。
    同一档位可能有多条（不同 codec / 码率），每档只留码率最高的一条。
    """
    dash = data.get("dash") or {}
    vids = dash.get("video") or []
    cands = [v for v in vids if v.get("codecid") == 7] or vids
    ok = [v for v in cands if v["id"] <= qn] or cands
    best = {}
    for v in ok:
        k = v["id"]
        if k not in best or v.get("bandwidth", 0) > best[k].get("bandwidth", 0):
            best[k] = v
    return [best[k] for k in sorted(best)]


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
    ladder = pick_video_ladder(data, qn)
    if not ladder:
        return 502, {"error": "没有可用的视频轨"}
    v = ladder[-1]                  # 最高档：返回值里的 id / 描述用它
    a = pick_audio(data)
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
           '</Period></MPD>') % (dur_s,
                                "".join(rep(t, "video") for t in ladder),
                                rep(a, "audio"))

    # 生成了 mpd 就意味着马上要取数据。后台先碰一下两条轨道的文件，把 CDN 回源
    # 提前启动 —— 比 dash.js 真正来要数据早约 50~130ms，够它搭上同一趟回源。
    for _t in (v, a):
        _u = (_t or {}).get("baseUrl")
        if _u:
            threading.Thread(target=_prefetch, args=(_u,), daemon=True).start()

    return 200, mpd, v["id"], QN_DESC.get(v["id"], str(v["id"]))


def api_stream(handler, query):
    raw = query.get("u", [""])[0]
    if not raw:
        return 400, {"error": "缺少 u"}
    target = _safe_target(raw)
    if not target:
        return 400, {"error": "非法地址（只接受 B 站自己的媒体域名）"}

    hdrs = {"User-Agent": UA, "Referer": REFERER, "Accept": "*/*"}
    rng = handler.headers.get("Range")
    if rng:
        hdrs["Range"] = rng
    s = sessdata()
    # 登录凭据只发给 B 站自己的域：白名单里的 CDN 不需要身份，
    # 无条件附带等于把 SESSDATA 交给任何被放行的主机。
    if s and _is_bili_host(target):
        hdrs["Cookie"] = "SESSDATA=%s" % s

    try:
        with cached_get(target, hdrs, 40) as resp:
            handler.send_response(resp.status)
            ctype = resp.headers.get("Content-Type") or ""
            if (not ctype) or ctype.startswith("application/octet-stream"):
                ctype = "video/mp4"      # 上游给的是通用类型，<video> 需要具体的
            handler.send_header("Content-Type", ctype)
            for k in ("Content-Length", "Content-Range", "Accept-Ranges"):
                v = resp.headers.get(k)
                if v:
                    handler.send_header(k, v)
            handler.end_headers()
            while True:
                # 用 read1 而不是 read：read(n) 会**阻塞到攒满 n 字节**才开始回写，
                # 于是「第一个字节」要等 256KB 全到（1.4MB/s 下约 183ms）才发得出去，
                # 播放器也就晚这么久才开始 append。read1 是有多少发多少（上限给到
                # 256KB 以免小块太多），首字节跟着数据到达就走。
                # 历史上的 64KB 瓶颈是**每块都阻塞等满**造成的，不是块小本身。
                chunk = resp.read1(262144) if hasattr(resp, "read1") else resp.read(262144)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except urllib.error.HTTPError as e:
        # 地址过期（签名带时效）时把状态原样透出，前端会重新取地址
        handler.send_response(e.code)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return None, None
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # 拖进度条 / 关页面时客户端会直接掐断，属正常现象，不该刷一屏 traceback
        # （连响应头都还没发出去就断开的情况也在这里 —— 关页面时很常见）
        # 上游还有没读完的数据，`with` 退出时会自动丢掉这条连接。
        pass
    except Exception as e:
        return 502, {"error": "转发失败：%s" % e}
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


# 本机登录态（登录名 / 大会员 / 有没有 bili_jct）几乎不变，但页面每次加载都要问
# 两三次，每次都得打一次 B 站的 nav 接口（实测 100~150ms）。缓存 30 秒，
# 并在「写凭据 / 登出」时立刻作废 —— 否则刚扫码登录完会看到 30 秒的「未登录」。
_STATUS_CACHE = {"at": 0.0, "data": None}
_STATUS_TTL = 30
_STATUS_LOCK = threading.Lock()


def status_cache_clear():
    with _STATUS_LOCK:
        _STATUS_CACHE["at"] = 0.0
        _STATUS_CACHE["data"] = None


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
    status_cache_clear()          # 凭据变了：登录态缓存立刻作废


DANMAKU_MAX_LEN = 30        # B 站直播弹幕长度上限，超长 B 站自己也会拒
WHEEL_INTERVAL_MIN = 1.0    # 独轮车最小间隔（秒）：再快就是纯刷屏，只会加速触发风控
WHEEL_COUNT_MAX = 200       # 独轮车单轮条数上限

# 独轮车的内容把关：命中这些词就不让发。
# 同一句话循环几十遍，比单条弹幕刺眼得多，也更容易把直播间氛围带坏 ——
# 这里挡的是骂人 / 人身攻击 / 明显擦边低俗 / 恶俗黑话这类「过于低俗」的内容。
# 清单刻意保守（宁可漏掉，也不误伤正常玩梗与称呼），要加减词直接改这里。
WHEEL_BLOCK_WORDS = (
    # 辱骂 / 人身攻击
    "傻逼", "沙比", "煞笔", "傻b", "智障", "脑残", "弱智", "白痴",
    "废物", "蠢货", "蠢材", "蠢猪", "神经病", "有病", "傻子",
    "尼玛", "尼美", "草泥马", "死妈", "你妈", "妈的", "妈蛋",
    "滚蛋", "去死", "人渣", "垃圾人", "狗东西", "贱人", "贱货",
    # 恶俗黑话
    "nmsl", "cnm", "wcnm", "sb",
    # 明显低俗 / 擦边
    "约炮", "一夜情", "援交", "包夜", "裸聊", "福利姬", "痴汉",
    "走光", "偷拍", "打飞机", "撸管", "自慰", "色情", "情色",
    "黄片", "黄色", "骚货", "骚逼", "浪货", "婊子", "妓女",
)
WHEEL_MOTTO = "我们需要更多匠心手摇的车，高质量小众的车。"


def wheel_block_hit(msg):
    """独轮车内容把关：返回命中的词，没问题返回 None。

    比较前先去掉空格与常见分隔符，「傻 逼」「傻*逼」这类变体也拦得住。
    """
    s = re.sub(r"[\s\*\-\._,，。!！？?~～]+", "", str(msg)).lower()
    for w in WHEEL_BLOCK_WORDS:
        if w in s:
            return w
    return None


def _danmaku_ready():
    sess, jct = load_credentials()
    return bool(sess and jct)


def _send_danmaku(msg):
    """发一条弹幕，返回 (B 站 code, message)。code=0 才是成功。"""
    _, jct = load_credentials()
    body = urllib.parse.urlencode({
        "bubble": "0", "msg": msg, "color": "16777215", "mode": "1",
        "fontsize": "25", "roomid": cur_room(), "rnd": str(int(time.time())),
        "csrf": jct, "csrf_token": jct,
    }).encode("utf-8")
    sess = load_credentials()[0]
    req = urllib.request.Request("https://api.live.bilibili.com/msg/send", data=body, headers={
        "User-Agent": UA,
        "Referer": "https://live.bilibili.com/%s" % cur_room(),
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
    # 内容把关放在登录校验之前：没登录也该知道这条不合适，而不是先去扫码
    hit = wheel_block_hit(msg)
    if hit:
        return 400, {"error": "这条内容过于低俗（命中「%s」），换一句吧。%s"
                     % (hit, WHEEL_MOTTO), "blocked": hit}
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
    st["room_id"] = cur_room()
    st["danmaku_ready"] = _danmaku_ready()
    st["motto"] = WHEEL_MOTTO
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
           % (urllib.parse.quote(cur_room()), urllib.parse.quote(str(qn))))
    d = bili_get(url, "https://live.bilibili.com/%s" % cur_room())
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
    out = {"room_id": cur_room(), "mid": cur_mid(), "station": station_head()}
    try:
        live = fetch_live()
    except Exception as e:
        out.update(living=False, error="开播状态获取失败：%s" % e)
        return 200, out
    out.update(living=bool(live.get("living")), title=live.get("title") or "",
               online=live.get("online") or 0, uname=live.get("uname") or "",
               face=live.get("face") or "", cover=live.get("cover") or "",
               area=live.get("area") or "", start=live.get("start") or 0,
               url=live.get("url") or "")
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
        "User-Agent": UA, "Referer": "https://live.bilibili.com/%s" % cur_room(),
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
                     {"id": cur_room(), "type": "0"})
    try:
        d = bili_get(url, "https://live.bilibili.com/%s" % cur_room())
    except Exception as e:
        return 502, {"error": "弹幕网关获取失败：%s" % e}
    if d.get("code") != 0:
        return 502, {"error": "弹幕网关 code=%s %s" % (d.get("code"), d.get("message"))}
    data = d.get("data") or {}
    hosts = [{"host": h.get("host"), "wss_port": h.get("wss_port")}
             for h in (data.get("host_list") or []) if h.get("host")]
    return 200, {"room_id": cur_room(), "token": data.get("token") or "",
                 "hosts": hosts, "logged": bool(sess)}


# ---- 实时弹幕：服务端收、SSE 推给页面 -------------------------------------
# 为什么不让浏览器直连弹幕网关：浏览器握手带不上 bilibili 域的 Cookie，
# 实测认证后立刻被服务端断开（close 1006）；本机服务端带 Cookie + 官方 Origin
# 连接则正常。所以由服务端维持 WebSocket、解析后经 SSE 推给页面。
# **一位主播一份**：合在一起会让副站的直播间显示别人的弹幕
# （worker 在后台线程里跑，cur_station() 会回落到主站 —— 必须显式按 sid 取）。
CHAT = {}                      # sid -> {"subs": [...], "backlog": [...], "worker": {...}}
CHAT_LOCK = threading.Lock()
CHAT_BACKLOG_MAX = 30


def chat_state(sid=None):
    sid = str(sid or cur_station().get("id"))
    with CHAT_LOCK:
        st = CHAT.get(sid)
        if st is None:
            st = {"subs": [], "backlog": [], "worker": {
                "thread": None, "running": False, "popularity": 0, "error": ""}}
            CHAT[sid] = st
        return st
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


def _chat_broadcast(sid, item):
    st = chat_state(sid)
    payload = json.dumps(item, ensure_ascii=False)
    with CHAT_LOCK:
        st["backlog"].append(payload)
        del st["backlog"][:-CHAT_BACKLOG_MAX]
        subs = list(st["subs"])
    for q in subs:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def _chat_parse(sid, data):
    off = 0
    while off + 16 <= len(data):
        ln = struct.unpack(">I", data[off:off + 4])[0]
        if ln < 16 or off + ln > len(data):
            return
        ver = struct.unpack(">H", data[off + 6:off + 8])[0]
        op = struct.unpack(">I", data[off + 8:off + 12])[0]
        body = data[off + 16:off + ln]
        if op == 3 and len(body) >= 4:
            pop = struct.unpack(">I", body[:4])[0]
            chat_state(sid)["worker"]["popularity"] = pop
            _chat_broadcast(sid, {"type": "popularity", "value": pop})
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
                _chat_parse_nested(sid, payload)
        off += ln


def _chat_parse_nested(sid, payload):
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
                    _chat_broadcast(sid, {"type": "danmaku", "uid": who[0] or 0,
                                          "uname": who[1] or "", "text": info[1] or ""})
            except Exception:
                pass
        off += ln


def _chat_worker(sid):
    st = chat_state(sid)
    st["worker"].update(running=True, error="")
    _CUR.station = find_station(sid)      # 后台线程：不绑就会连到主站房间去
    try:
        while True:
            with CHAT_LOCK:
                if not st["subs"]:
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
                    "uid": self_uid(), "roomid": int(cur_room()), "proto_ver": 2,
                    "buvid": buvid3(), "platform": "web", "clientver": "1.14.3",
                    "type": 2, "key": info["token"]}).encode(), 7))
                last_hb = time.time()
                while True:
                    with CHAT_LOCK:
                        if not st["subs"]:
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
                        _chat_parse(sid, data)
                    elif opcode == 9:
                        _ws_send(sock, data, opcode=10)
                    elif opcode == 8:
                        raise RuntimeError("网关要求关闭")
            except Exception as e:
                st["worker"]["error"] = "%s: %s" % (type(e).__name__, e)
                _chat_broadcast(sid, {"type": "state", "text": "弹幕连接中断，正在重试…"})
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            time.sleep(3)                 # 重连间隔
    finally:
        st["worker"]["running"] = False


def chat_subscribe():
    """订阅**当前请求绑定的那位主播**的弹幕（页面每打开一个直播间就一个订阅）。"""
    sid = str(cur_station().get("id"))
    st = chat_state(sid)
    q = queue.Queue(maxsize=200)
    with CHAT_LOCK:
        st["subs"].append(q)
        backlog = list(st["backlog"])
        need_worker = not st["worker"]["running"]
    if need_worker:
        t = threading.Thread(target=_chat_worker, args=(sid,), daemon=True)
        st["worker"]["thread"] = t
        t.start()
    return q, backlog


def chat_unsubscribe(sid, q):
    st = chat_state(sid)
    with CHAT_LOCK:
        if q in st["subs"]:
            st["subs"].remove(q)


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
    status_cache_clear()
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
# 按主播分桶：{station_id: {"at": .., "data": ..}} —— 不分开的话切主播会串数据
PROGRAMS_CACHE = {}
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
    """系列内全部投稿（分页拉全），返回 bilibili 原始 archives 列表

    系列 ID 由 resolve_series 决定（自动发现 / stations.json 覆盖），不再写死。
    """
    _st = cur_station()
    rs = resolve_series(_st, allow_network=True)
    sid = rs["series_id"]
    if not sid:
        raise RuntimeError("还没定位到「%s」的回放系列%s"
                           % (_st.get("name") or _st.get("id"),
                              ("：%s" % rs["error"]) if rs.get("error") else ""))
    items = []
    pn = 1
    while True:
        url = ("https://api.bilibili.com/x/series/archives?mid=%s&series_id=%s"
               "&only_normal=true&sort=desc&pn=%d&ps=30" % (_st["mid"], sid, pn))
        d = bili_get(url, "https://space.bilibili.com/%s/lists/%s?type=series"
                          % (_st["mid"], sid))
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
    """实时构建与 data/programs.json 同构的清单（字段、排序、评分全部对齐）。

    按「当前主播」构建：主站沿用原来的系列，副站用各自的 series_id。
    """
    _st = cur_station()
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
        "mid": _st["mid"], "series": resolve_series(_st, allow_network=False),
        "up_name": _st.get("name") or "", "station": station_head(_st),
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
           "done": [], "log": [], "error": "", "ffmpeg": None, "refine": None,
           # 识别进度：progress 由 auto_segments 的分块循环上报（当前分P 分析到第几秒），
           # batch 是本批次「第几个投稿 / 共几个」+ 已完成音频秒数，started_at 用来算速率。
           "progress": {}, "batch": {"total": 0, "done": 0, "done_seconds": 0.0},
           "started_at": 0.0}
# 排队时把「当时的节目条目」一起记住。清单是实时抓的，而 data/programs.json 只在
# collect.py 运行时才更新 —— 刚发布的新回放不在那个文件里，作业会报「节目单里没有」。
SEG_PROGRAMS = {}
# 排队时记住这条 bvid 属于哪位主播：worker 是后台线程，靠它把状态与分段写回对的主播目录。
SEG_JOB["stations"] = {}


def seg_state_file(st=None):
    """分段状态文件（auto / seen_upto / last），每位主播一份。

    合成一份会让「水位线」互相串：主站已经划过的水位线会挡住副站的历史回放，
    自动分段对副站等于失效。
    """
    st = st or cur_station()
    if st.get("main"):
        return os.path.join(APPDIR, "seg_state.json")   # 主站沿用原文件，历史状态不丢
    return os.path.join(station_dir(st), "seg_state.json")


def _seg_state():
    try:
        with open(seg_state_file(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _seg_save_state(st):
    try:
        path = seg_state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
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
    # 数据目录必须跟着「当前主播」走：模块里 DATA 是写死的 ROOT/data，
    # 不覆盖的话副站的分段会被写进主站的 segments.js（跨主播串数据）。
    # 主站保持原有路径，行为逐字节不变。
    st = cur_station()
    mod.ROOT = ROOT                                        # = %LOCALAPPDATA%\KomichiRadio\www
    if st.get("main"):
        mod.DATA = os.path.join(ROOT, "data")              # 页面就是从这儿读 segments.js
        if FROZEN:
            mod.CACHE = os.path.join(APPDIR, ".segcache")
            mod.WORK = os.path.join(APPDIR, ".segaudio")
            mod.FEAT = os.path.join(APPDIR, ".segfeat")
    else:
        base = station_dir(st)
        mod.DATA = base
        mod.CACHE = os.path.join(base, ".segcache")
        mod.WORK = os.path.join(base, ".segaudio")
        mod.FEAT = os.path.join(base, ".segfeat")
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


def segments_file():
    """本机在用的 segments.js 路径（主站沿用 data/，副站各自一个目录）。"""
    return os.path.join(station_dir(), "segments.js")


def segments_have():
    """已有分段的分P cid 集合 —— 直接读 data/segments.js，不另维护状态。"""
    return set(_read_segments_file(segments_file()).keys())


def _seg_worker():
    """队列消费者：逐个投稿跑分段。"""
    try:
        while True:
            with SEG_LOCK:
                if not SEG_JOB["queue"]:
                    break
                bvid = SEG_JOB["queue"].pop(0)
            # worker 跑在后台线程里，线程局部的「当前主播」是空的 —— 不在这里绑一次，
            # 状态文件和 segments.js 都会落到主站目录去。
            _CUR.station = find_station((SEG_JOB.get("stations") or {}).get(bvid))
            SEG_JOB["current"] = bvid
            SEG_JOB["log"] = []
            SEG_JOB["error"] = ""
            try:
                mod = _seg_module()

                def logf(msg):
                    # 日志只留最近 12 行：页面上够看，也不会把状态接口撑大
                    SEG_JOB["log"] = (SEG_JOB["log"] + [str(msg).strip()])[-12:]

                # 分析过程的明细（每个分P 的秒数/段数/阈值）走的是模块内的 log()，
                # 默认打到 stdout（打包版被重定向进 log.txt）。接到面板里来，
                # 网页上才看得到「跑到哪、切了几段」，而不是只有一行标题。
                def _prog_sink(d):
                    # 进度用「合并」而不是覆盖：投稿级信息（标题/第几个分P）在 process_program
                    # 报、分P 内的秒数在 process_part 报，两边合起来才是完整的一条。
                    cur = SEG_JOB["progress"]
                    if d.get("cid") and cur.get("cid") and d["cid"] != cur["cid"]:
                        # 换了分P：把上一个分P 的整份工作量计入「已完成」，
                        # 页面用它除以已耗时来估算剩余时间
                        SEG_JOB["batch"]["done_seconds"] += seg_work_done(cur)
                    cur.update(d)

                mod.LOG_SINK[0] = logf
                mod.PROGRESS_SINK[0] = _prog_sink

                prog = SEG_PROGRAMS.get(bvid)
                res = mod.process_bvid(bvid, logf=logf,
                                       programs=[prog] if prog else None)
                res["at"] = int(time.time())
                if not res.get("ok"):
                    SEG_JOB["error"] = res.get("error") or "分段失败"
                with SEG_LOCK:
                    SEG_JOB["done"] = (SEG_JOB["done"] + [res])[-10:]
                    # 这一整场算完了：计入批次进度，并把最后一份工作量计入「已完成」
                    # （前面的分P 已在进度回调里逐个累计；速率 = 已完成 / 已耗时）
                    SEG_JOB["batch"]["done"] += 1
                    SEG_JOB["batch"]["done_seconds"] += seg_work_done(SEG_JOB["progress"])
                    SEG_JOB["progress"] = {}
                # 「上次更新时间」必须跨重启保留（内存里的 done 会随进程消失），
                # 所以每次都写回状态文件。不能用 segments.js 的 mtime 代替：
                # 每次启动/换版本都会重写那个文件，mtime 会变成「刚刚」。
                st = _seg_state()
                st["last"] = {
                    "at": res["at"], "ok": bool(res.get("ok")),
                    "title": res.get("title") or bvid, "bvid": bvid,
                    "processed": res.get("processed") or 0,
                    "skipped": res.get("skipped") or 0,
                    "segments": res.get("segments") or 0,
                    "error": res.get("error") or ""}
                _seg_save_state(st)
            except Exception as e:
                SEG_JOB["error"] = "%s: %s" % (type(e).__name__, e)
            finally:
                SEG_JOB["current"] = None
    finally:
        SEG_JOB["running"] = False


def segments_enqueue(bvid, program=None):
    """把投稿排进分段队列；队列空时顺手把 worker 拉起来。"""
    with SEG_LOCK:
        if bvid in SEG_JOB["queue"] or SEG_JOB["current"] == bvid:
            return False
        if any(d.get("bvid") == bvid and d.get("ok") for d in SEG_JOB["done"]):
            return False                       # 这轮已经成功处理过，别重复排队
        if program:
            SEG_PROGRAMS[bvid] = program
        SEG_JOB.setdefault("stations", {})[bvid] = str(cur_station().get("id"))
        SEG_JOB["queue"].append(bvid)
        b = SEG_JOB["batch"]
        if b["done"] >= b["total"]:      # 上一批已经跑完，重新开始计数
            b["total"] = 0
            b["done"] = 0
            b["done_seconds"] = 0.0
            SEG_JOB["progress"] = {}
        b["total"] += 1
        if not SEG_JOB["running"]:
            SEG_JOB["running"] = True
            SEG_JOB["started_at"] = time.time()   # 速率的起算点
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
        segments_enqueue(p["bvid"], p)
    st["seen_upto"] = newest
    _seg_save_state(st)


def _known_parts():
    """频道里已知的全部分P cid。

    优先用页面上那份实时清单 —— PROGRAMS_CACHE 里已经有，读它不会打接口；
    没有缓存时回退到离线快照 data/programs.json。
    """
    sid = str(cur_station().get("id"))
    data = (PROGRAMS_CACHE.get(sid) or {}).get("data")
    if not data:
        try:
            with open(os.path.join(station_dir(), "programs.json"), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return set()
    return set(str(x["cid"])
               for p in (data.get("programs") or [])
               for x in (p.get("parts") or []) if x.get("cid") is not None)


def api_segments_status():
    st = _seg_state()
    seg = _read_segments_file(segments_file())
    # 分子分母必须同源：segments.js 里会含实时清单的新分P，而 programs.json 只是离线快照，
    # 两边不同步时旧写法（分子取 segments.js 键数、分母取 programs.json 分P 数）
    # 会把覆盖率显示成 111%（68/61）。取并集后 have 恒 ≤ total。
    total = len(_known_parts() | set(seg))
    # 进度与「还剩多久」：剩余秒数 = 当前分P 没分析完的 + 排队里每个投稿的全部分P 时长；
    # 速率由页面拿 done_seconds / elapsed 自己算，服务端只提供原始数字。
    prog = SEG_JOB["progress"]
    b = SEG_JOB["batch"]
    todo = max(0.0, float(prog.get("audio_total") or 0) * float(prog.get("phases") or 1)
               - seg_work_done(prog))
    # 排队里的投稿按「音频 1 份 + 精修 1 份」估（与模块内的工作量口径一致）；
    # 精修实际可能回退，宁可让剩余时间估长一点，也不要让用户等得比提示的久
    q_phases = 2 if seg_refine_ready() else 1
    for bv in SEG_JOB["queue"]:
        p = SEG_PROGRAMS.get(bv)
        if p:
            todo += q_phases * sum(float(x.get("duration") or 0) for x in p.get("parts") or [])
    elapsed = (time.time() - SEG_JOB["started_at"]) if SEG_JOB["started_at"] else 0.0
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
        "coverage": {"have": len(seg), "total": total,
                     "segments": sum(len(v) for v in seg.values())},
        "last": st.get("last") or {},
        "seen_upto": st.get("seen_upto") or 0,
        "progress": dict(prog),
        "batch": {"total": b["total"], "done": b["done"]},
        "elapsed": round(elapsed, 1),
        "done_seconds": round(b["done_seconds"] + seg_work_done(prog), 1),
        "todo_seconds": round(todo, 1),
    }


def api_segments_auto(on):
    st = _seg_state()
    st["auto"] = bool(on)
    if on and not st.get("seen_upto"):
        # 开启时先立水位线：只对「从现在起」出现的新回放自动分段
        st["seen_upto"] = int(time.time())
    _seg_save_state(st)
    return 200, {"ok": True, "auto": bool(on), "seen_upto": st.get("seen_upto")}


def seg_work_done(p):
    """已完成的工作量。统一口径：音频已分析秒数 + 精修按同样长度折算
    （精修与音频并行，两边各自上报，合起来才是进度）。"""
    at = float(p.get("audio_total") or 0)
    if not at:
        return min(float(p.get("analyzed") or 0), float(p.get("total") or 0))
    return min(float(p.get("analyzed") or 0), at) + float(p.get("refine_ratio") or 0) * at


def _find_program(bvid):
    """从最近的实时清单里取节目条目（作业要用；programs.json 可能还没这个投稿）。

    必须按主播取桶：PROGRAMS_CACHE 是 {station_id: {...}}，
    沿用分桶之前的 PROGRAMS_CACHE["data"] 会直接 KeyError
    —— 「给最新一期分段」和「更新数据」两个按钮因此都崩过。
    """
    sid = str(cur_station().get("id"))
    with PROGRAMS_LOCK:
        data = (PROGRAMS_CACHE.get(sid) or {}).get("data")
    for p in (data or {}).get("programs") or []:
        if p.get("bvid") == bvid:
            return p
    return None


def api_segments_refresh():
    """手动触发：扫一遍清单，把所有还缺分段的分P 所属投稿排队（新回放优先）。

    与 segments_autoscan 的分工：autoscan 只认水位线之后出现的回放（首次开启不排历史，
    否则一开就排几十场）；这里是用户显式点击，所以历史缺口一起补。
    """
    if seg_ffmpeg() == "":
        return 200, {"ok": False, "error": "未找到 ffmpeg，无法分段"}
    sid = str(cur_station().get("id"))
    with PROGRAMS_LOCK:
        bucket = PROGRAMS_CACHE.get(sid) or {}
        hit = bucket.get("data")
        fresh = bool(hit) and (time.time() - float(bucket.get("at") or 0)) < PROGRAMS_TTL
    programs = (hit or {}).get("programs") if fresh else None
    if not programs:
        try:
            programs = build_programs()["programs"]
        except Exception as e:
            return 200, {"ok": False, "error": "取节目单失败：%s" % e}
    have = segments_have()
    targets = [p for p in programs
               if any(str(x["cid"]) not in have for x in p["parts"])]
    missing = sum(1 for p in targets for x in p["parts"] if str(x["cid"]) not in have)
    # 排队是单线程串行的，新回放先出炉更有意义
    targets.sort(key=lambda p: p.get("pubdate") or 0, reverse=True)
    queued = sum(1 for p in targets if segments_enqueue(p["bvid"], p))
    return 200, {"ok": True, "queued": queued, "programs": len(targets),
                 "missing_parts": missing}


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


def _desktop_dir():
    """桌面目录。可能是 %USERPROFILE%\\Desktop，也可能被 OneDrive 重定向 ——
    后者只在注册表的 Shell Folders 里能拿到，所以要优先问注册表。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            v = os.path.expandvars(str(winreg.QueryValueEx(k, "Desktop")[0] or ""))
            if v and os.path.isdir(v):
                return v
    except OSError:
        pass
    return os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"), "Desktop")


def api_shortcut_create(where=""):
    """创建指向本程序的桌面快捷方式。

    这是「一点启动 + 完全没有任何提示」唯一可靠的做法：快捷方式直接指向 EXE，
    不经过浏览器，所以不存在「外部程序授权」那一层确认。
    浏览器书签做不到这一点 —— http:// 无法关联本地程序，而自定义协议必须先过
    浏览器的授权确认（那是浏览器的安全边界，任何网页都绕不过）。
    """
    if not FROZEN:
        return 400, {"error": "源码运行模式：快捷方式要指向本程序的 EXE，源码运行没有可指向的目标。"}
    exe = os.path.abspath(sys.executable)
    if not os.path.isfile(exe):
        return 500, {"error": "找不到本程序的可执行文件：%s" % exe}
    d = where or _desktop_dir()
    if not os.path.isdir(d):
        return 500, {"error": "目标目录不存在：%s" % d}
    lnk = os.path.join(d, "二十四时小路电台.lnk")

    def q(s):
        return str(s).replace("'", "''")

    ps = ("$ws = New-Object -ComObject WScript.Shell; "
          "$s = $ws.CreateShortcut('%s'); "
          "$s.TargetPath = '%s'; "
          "$s.WorkingDirectory = '%s'; "
          "$s.IconLocation = '%s,0'; "
          "$s.Description = '二十四时小路电台 · 一点直接进直播间'; "
          "$s.Save()") % (q(lnk), q(exe), q(os.path.dirname(exe)), q(exe))
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return 500, {"error": "调用 PowerShell 失败：%s" % e}
    if r.returncode != 0 or not os.path.isfile(lnk):
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return 500, {"error": "创建快捷方式失败：%s" % (err or "未知错误")}
    return 200, {"ok": True, "path": lnk, "target": exe}


def api_protocol_status():
    cmd = protocol_registered()
    return 200, {"supported": FROZEN, "registered": bool(cmd), "command": cmd,
                 "url": PROTOCOL + "://open"}


# ---- 回放清单的缓存策略：磁盘留一份 + 「先给旧的、后台重抓」 --------------
# 现抓一个主播的清单要打 30~70 次 B 站接口，实测 1.5~2.6 秒。切换板块时把它挡在
# 页面面前，就是用户感觉到的「明显卡顿」。所以：
#   · 每次成功抓取都落盘（data/stations/<id>/programs_cache.json），重启后仍能秒开；
#   · 页面请求时先把手上这份给出去，只有「过期」时才起后台线程重抓；
#   · 一份都没有（首次访问该主播）才同步抓 —— 这一步的等待无法避免。
# 新鲜度对这份数据没有苛刻要求：它是「有哪些回放」，一天也变不了几次，
# 页面上显示的「实时数据 HH:MM」直接来自 generated_at，用户看得见。
PROGRAMS_STALE_AFTER = 60          # 秒：比这更新就直接用，连一次上游请求都不发
_PROGRAMS_BUILDING = {}            # sid -> True（同一主播同时只允许一个重抓在跑）
_PROGRAMS_BUILD_LOCK = threading.Lock()


def programs_cache_file(st=None):
    return os.path.join(station_dir(st), "programs_cache.json")


def _read_programs_disk(st):
    try:
        with open(programs_cache_file(st), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("programs") else None
    except Exception:
        return None


def _write_programs_disk(st, data):
    try:
        path = programs_cache_file(st)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def _programs_store(sid, st, out):
    # 「几点了」一律以数据自己的 generated_at 为准：它就印在页面上，
    # 用写入时刻会让磁盘读到的那份看起来比实际新
    at = float((out.get("meta") or {}).get("generated_at") or 0) or time.time()
    with PROGRAMS_LOCK:
        bucket = PROGRAMS_CACHE.setdefault(sid, {"at": 0.0, "data": None})
        bucket["at"] = at
        bucket["data"] = out
    _write_programs_disk(st, out)


def _programs_build(sid, st, label):
    """抓一次并落盘。调用方负责 _PROGRAMS_BUILDING 的置位与清理。"""
    _CUR.station = st            # 后台线程里线程局部的「当前主播」是空的，得自己绑
    out = build_programs()
    out["cached"] = False
    out["stale"] = False
    _programs_store(sid, st, out)
    print("[programs] %s %s：%s 个节目" % (sid, label,
                                         (out.get("meta") or {}).get("count")))
    try:
        segments_autoscan(out.get("programs") or [])
    except Exception as e:
        print("自动分段检查失败：%s" % e)
    return out


def _programs_rebuild(sid, st):
    """后台重抓。失败只记日志 —— 已经在服务的那份继续用。"""
    try:
        _programs_build(sid, st, "后台重抓完成")
    except Exception as e:
        print("[programs] %s 后台重抓失败：%s" % (sid, e))
    finally:
        with _PROGRAMS_BUILD_LOCK:
            _PROGRAMS_BUILDING.pop(sid, None)


def api_programs(refresh=False, hard=False):
    """GET /api/programs?[refresh=1][&hard=1] —— 回放清单。

    refresh=1：允许「先给缓存、后台重抓」（页面加载走这条，切换板块才不卡）。
    hard=1   ：强制同步重抓（发布自检、以及确实要全新数据时用）。
    一份都没有时无论如何都得同步抓，抓不到才报错。
    """
    st = cur_station()
    sid = str(st.get("id"))
    now = time.time()

    with PROGRAMS_LOCK:
        bucket = PROGRAMS_CACHE.setdefault(sid, {"at": 0.0, "data": None})
        hit = bucket["data"]
        at = float(bucket["at"] or 0)

    if hard:
        with _PROGRAMS_BUILD_LOCK:
            _PROGRAMS_BUILDING[sid] = True
        try:
            return 200, _programs_build(sid, st, "强制重抓")
        except Exception as e:
            with PROGRAMS_LOCK:
                prev = (PROGRAMS_CACHE.get(sid) or {}).get("data")
            if prev:
                out = dict(prev)
                out["cached"] = True
                out["error"] = str(e)
                return 200, out
            return 502, {"error": "取回放清单失败：%s" % e}
        finally:
            with _PROGRAMS_BUILD_LOCK:
                _PROGRAMS_BUILDING.pop(sid, None)

    if hit is None:
        # 进程里没有 → 看磁盘（上次运行、或上次切到这位时留下的那份）
        disk = _read_programs_disk(st)
        if disk:
            hit = disk
            at = float((disk.get("meta") or {}).get("generated_at") or 0) or 1.0
            with PROGRAMS_LOCK:
                bucket["data"] = hit
                bucket["at"] = at

    if hit is not None:
        age = (now - at) if at else 1e9
        out = dict(hit)
        out["cached"] = True
        out["stale"] = age > PROGRAMS_STALE_AFTER
        out["age"] = int(max(0.0, age))
        if out["stale"]:
            # 先把旧的给出去，重抓丢到后台 —— 这就是「切换板块不卡」的关键
            with _PROGRAMS_BUILD_LOCK:
                if not _PROGRAMS_BUILDING.get(sid):
                    _PROGRAMS_BUILDING[sid] = True
                    threading.Thread(target=_programs_rebuild, args=(sid, st),
                                     daemon=True).start()
                    out["refreshing"] = True
        return 200, out

    # 一份都没有：只能同步抓（首次访问该主播，避免不了等一次）
    with _PROGRAMS_BUILD_LOCK:
        _PROGRAMS_BUILDING[sid] = True
    try:
        return 200, _programs_build(sid, st, "首次抓取")
    except Exception as e:
        return 502, {"error": "取回放清单失败：%s" % e}
    finally:
        with _PROGRAMS_BUILD_LOCK:
            _PROGRAMS_BUILDING.pop(sid, None)


# ---------------------------------------------------------------- 小路状态

# 同样按主播分桶（状态板是小路的副产物，但切人后不能互相覆盖）
STATUS_CACHE = {}
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
                 "?uids[]=%s" % cur_mid(), "https://live.bilibili.com/")
    if d.get("code") != 0:
        raise RuntimeError("开播接口 code=%s %s" % (d.get("code"), d.get("message")))
    info = ((d.get("data") or {}).get(cur_mid())) or {}
    if not info:
        raise RuntimeError("开播接口未返回该 uid 的数据")

    room = str(info.get("room_id") or cur_room())
    return {
        "living": int(info.get("live_status") or 0) == 1,
        "mid": cur_mid(),
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
    out = {"mid": cur_mid(), "cached": False, "at": int(time.time())}
    sid = str(cur_station().get("id"))
    with STATUS_LOCK:
        bucket = STATUS_CACHE.setdefault(sid, {"at": 0.0, "data": None})
        hit = bucket["data"]

    try:
        out["live"] = fetch_live()
    except Exception as e:
        # 接口偶发失败时，退回上一次成功的结果，好过让弹窗空白
        prev = (hit or {}).get("live")
        out["live"] = prev or {"living": False, "mid": cur_mid(), "room_id": cur_room(),
                               "url": "https://live.bilibili.com/%s" % cur_room()}
        out["live_error"] = str(e)
    out["ok"] = bool(out.get("live"))
    with STATUS_LOCK:
        bucket["at"] = time.time()
        bucket["data"] = out
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
        """把服务端收到的实时弹幕用 SSE 推给页面（浏览器直连网关会被拒，见 chat 段注释）。

        按当前板块取：EventSource 带 ?station=，服务端在 do_GET 开头已绑好。
        """
        _sid = str(cur_station().get("id"))
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
            chat_unsubscribe(_sid, q)
        return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        # 主播上下文：?station=<id|mid|房间号>，不带则用主站。必须在所有处理之前设置
        use_station(urllib.parse.parse_qs(parsed.query).get("station", [""])[0])
        q = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/status":
            code, obj = api_status("refresh" in q)
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
            # 类型已在上游收敛成 image/*；再加 nosniff，杜绝浏览器把响应猜成别的东西
            self.send_header("X-Content-Type-Options", "nosniff")
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
        if parsed.path == "/api/stations":
            return self._json(*api_stations())
        if parsed.path == "/api/weibo":
            return self._json(*api_weibo(q))
        if parsed.path == "/api/weibo/login/poll":
            return self._json(*api_weibo_login_poll())
        if parsed.path == "/api/series":
            return self._json(*api_series_status())
        if parsed.path == "/data/segments.js":
            # 带 station 时按主播给；不带（或主站）落回静态文件
            _txt = station_segments_js(q.get("station", [""])[0])
            if _txt is not None:
                _body = _txt.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(_body)))
                self.end_headers()
                self.wfile.write(_body)
                return
        if parsed.path == "/api/programs":
            code, obj = api_programs("refresh" in q, "hard" in q)
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
        use_station(urllib.parse.parse_qs(parsed.query).get("station", [""])[0])
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

        if parsed.path == "/api/stations/probe":
            return self._json(*api_stations_probe(obj))
        if parsed.path == "/api/stations/save":
            return self._json(*api_stations_save(obj))
        if parsed.path == "/api/stations/delete":
            return self._json(*api_stations_delete(obj))
        if parsed.path == "/api/live/credential":
            return self._json(*api_live_credential(obj))
        if parsed.path == "/api/segments/auto":
            return self._json(*api_segments_auto(obj.get("on")))
        if parsed.path == "/api/segments/refresh":
            return self._json(*api_segments_refresh())
        if parsed.path == "/api/series/refresh":
            return self._json(*api_series_refresh(obj))
        if parsed.path == "/api/page/bye":
            return self._json(*page_bye(str(obj.get("cid") or "anon")))
        if parsed.path == "/api/protocol/register":
            return self._json(*protocol_register())
        if parsed.path == "/api/protocol/unregister":
            return self._json(*protocol_unregister())
        if parsed.path == "/api/shortcut/create":
            return self._json(*api_shortcut_create(str(obj.get("dir") or "")))
        if parsed.path == "/api/weibo/cookie":
            return self._json(*api_weibo_cookie(obj))
        if parsed.path == "/api/weibo/logout":
            return self._json(*api_weibo_logout())
        if parsed.path == "/api/weibo/login":
            return self._json(*api_weibo_login_start())
        if parsed.path == "/api/segments/run":
            bvid = str(obj.get("bvid") or "").strip()
            if not bvid:
                return self._json(400, {"error": "缺少 bvid"})
            if seg_ffmpeg() == "":
                return self._json(400, {"error": "本机没找到 ffmpeg，无法做分段"})
            queued = segments_enqueue(bvid, _find_program(bvid))
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



# ---------------------------------------------------------------- 微博
#
# 为什么走本机服务代理：微博对「不带浏览器 cookie 的请求」直接回 HTTP 432（访客风控），
# 而前端页面直连又会被 CORS 挡住（接口不带跨域头）。所以由本机服务代取。
#
# 实测（2026-09-30，用真实浏览器 + 导出 cookie 交叉验证）：
#   · 不带 cookie              → HTTP 432
#   · 带访客 cookie            → 200，但正文接口只回一张「登录注册后查看更多微博」的卡片
#   · 个人资料（昵称/头像/简介）→ 游客就能拿到
# 所以：资料卡默认可见，正文要登录态；登录态存本机文件（与 SESSDATA 同级）。

WEIBO_FILE = os.path.join(APPDIR, "weibo.txt")
WEIBO_API = "https://m.weibo.cn/api/container/getIndex"
WEIBO_DESKTOP_API = "https://weibo.com/ajax"
WEIBO_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")
# 桌面站的 UA 必须和登录窗口里那个浏览器一致，否则接口会因为「换了客户端」把
# 登录态当异常会话处理。
WEIBO_UA_DESKTOP = UA
WEIBO_TTL_OK = 300          # 成功结果缓存 5 分钟（微博接口慢，且容易触发风控）
WEIBO_TTL_FAIL = 60
_WEIBO_LOCK = threading.Lock()
_WEIBO_CACHE = {}           # uid -> {"at": ts, "data": {...}}
WEIBO_LOGIN_PORT = 9333
_WB_LOGIN_LOCK = threading.Lock()
WEIBO_LOGIN = {"proc": None, "port": 0, "state": "idle", "error": "",
               "started": 0, "nick": ""}


# ---- cookie 存储：按域分开 ----------------------------------------------------
#
# 微博在 .weibo.com 与 .weibo.cn 各放一份同名 cookie（SUB / SUBP …）。
# 如果把它们拼进同一行请求头，就变成 `SUB=A; SUB=B`，服务端只会取到第一个 ——
# 表现在外就是「桌面站明明登录了，m 站接口却还说没登录」。所以要分开存：


def _wb_store_read():
    """读 cookie 存储：{"weibo.com": "SUB=…", "weibo.cn": "SUB=…", "*": 手动粘贴}。

    "*" 是用户手动粘贴的一整串（没有域信息），两个域都用它兜底。
    旧版本文件是一行纯 cookie 串，按手动粘贴处理。
    """
    try:
        with open(WEIBO_FILE, encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return {}
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
        except Exception:
            return {}
        if isinstance(d, dict):
            return {k: v for k, v in d.items() if isinstance(v, str) and v}
        return {}
    return {"*": raw}


def _wb_store_write(d):
    """覆盖写 —— 本机删除会被劫持到回收站且可能失败，所以清除 = 写空对象。"""
    os.makedirs(os.path.dirname(WEIBO_FILE), exist_ok=True)
    with open(WEIBO_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in (d or {}).items() if v},
                           ensure_ascii=False))
    with _WEIBO_LOCK:
        _WEIBO_CACHE.clear()


def load_weibo_cookie(host=""):
    """取给 host（"weibo.com" / "weibo.cn"）用的 cookie 串；不带 host 时取一份通用的。"""
    d = _wb_store_read()
    if not d:
        return ""
    if host:
        for dom, ck in d.items():
            if dom != "*" and (host == dom or host.endswith("." + dom)):
                return ck
    for k in ("weibo.com", "weibo.cn", "*"):
        if d.get(k):
            return d[k]
    return ""


def save_weibo_cookie(value, host="*"):
    """写入一份 cookie。host="*" 表示手动粘贴（两个域共用）。"""
    v = " ".join((value or "").split())
    if v and (len(v) > 4096 or set(v) & set("\r\n")):
        raise ValueError("Cookie 看起来不合法（应为一行 name=value; name=value…）")
    d = _wb_store_read()
    if d.get(host, "") == v:
        return      # 没变就别写盘、别清缓存 —— 轮询每秒都会走到这里
    if v:
        d[host] = v
    else:
        d.pop(host, None)
    _wb_store_write(d)


def clear_weibo_cookie():
    """清空全部（退出登录）。"""
    _wb_store_write({})


def save_weibo_cookie_soft():
    """保留旧名字：等价于清空。"""
    clear_weibo_cookie()


def weibo_home(uid):
    return "https://weibo.com/u/%s" % uid


def weibo_login_url(uid):
    """桌面版登录页（登录窗口的前台标签开的就是它）。

    **不能**用 m.weibo.cn 的登录页：那是手机版 —— 只有手机号 + 验证码，还一路往
    App 引，电脑上根本走不完（用户反馈的「弹出手机版登录界面，电脑无法登录」就是它）。
    桌面版 passport 页有三种方式：扫描二维码 / 账号密码 / 微信。
    url 参数 = 登录成功后回跳的地址。
    """
    back = weibo_home(uid) if uid else "https://weibo.com/"
    return ("https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url="
            + urllib.parse.quote(back, safe=""))


def weibo_get(url, cookie="", referer=None, timeout=20, mobile=True):
    h = {"User-Agent": WEIBO_UA if mobile else WEIBO_UA_DESKTOP,
         "Accept": "application/json, text/plain, */*",
         "Accept-Language": "zh-CN,zh;q=0.9", "X-Requested-With": "XMLHttpRequest"}
    if mobile:
        h["MWeibo-Pwa"] = "1"       # 不加这个，m 站会当成桌面浏览器而改回 HTML 页面
    if referer:
        h["Referer"] = referer
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, headers=h)
    with urlopen(req, timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except Exception:
        raise RuntimeError("微博返回的不是 JSON（可能被风控拦了）")


def weibo_text(html_text):
    """把微博正文的 HTML 压成纯文本。

    必须在这里压平：微博正文里带 a / img（表情）/ br，而它的内容是不可信的外部输入，
    直接丢给前端等于把注入面交给它。压成纯文本之后前端照常转义输出即可。
    表情是 <img alt="[哈哈]">，保留 alt 才能看出表情。
    """
    if not html_text:
        return ""
    t = re.sub(r"<img[^>]*\balt=\"([^\"]*)\"[^>]*>", r"\1", html_text)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"</p>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return t.strip()


def weibo_parse_posts(cards, max_n=20):
    """把 container 的 cards 压成前端要的最小字段集。"""
    out = []
    for c in cards or []:
        mb = c.get("mblog")
        if not mb:
            continue
        pics = []
        for p in (mb.get("pics") or []):
            u = p.get("url") or (p.get("large") or {}).get("url") or ""
            if u:
                pics.append(u)
        rp = mb.get("retweeted_status") or None
        out.append({
            "id": str(mb.get("id") or ""),
            "bid": str(mb.get("bid") or ""),
            "text": weibo_text(mb.get("text")),
            "at": str(mb.get("created_at") or ""),
            "from": weibo_text((mb.get("source") or "")),
            "pics": pics[:9],
            "reposts": int(mb.get("reposts_count") or 0),
            "comments": int(mb.get("comments_count") or 0),
            "likes": int(mb.get("attitudes_count") or 0),
            "long": bool(mb.get("isLongText")),
            "retweet": ({
                "name": (rp.get("user") or {}).get("screen_name") or "",
                "text": weibo_text(rp.get("text"))[:400],
                "pics": [p.get("url") for p in (rp.get("pics") or [])][:3],
            } if rp else None),
        })
        if len(out) >= max_n:
            break
    return out


def weibo_profile_parse(uid, ui):
    return {
        "uid": str(uid),
        "name": ui.get("screen_name") or "",
        "avatar": ui.get("profile_image_url") or "",
        "desc": ui.get("description") or "",
        "followers": int(ui.get("followers_count") or 0),
        "follows": int(ui.get("follow_count") or 0),
        "posts": int(ui.get("statuses_count") or 0),
        "verified": ui.get("verified_reason") or "",
        "home": weibo_home(uid),
    }


def _wb_int(v):
    """微博的数字字段有时是字符串（桌面接口就是 "3961"），统一转一下，坏了当 0。"""
    try:
        return int(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _wb_desktop_time(s):
    """桌面接口的 created_at 是 "Sat Sep 27 20:11:05 +0800 2026" 这种英文串，
    与 m 站的中文风格（"9-27" / "刚刚"）差太远，折成 "09-27 20:11" 再给前端。"""
    try:
        return time.strftime("%m-%d %H:%M",
                             time.strptime(str(s).strip(), "%a %b %d %H:%M:%S %z %Y"))
    except Exception:
        return str(s or "")


def _wb_desktop_pics(mb):
    """桌面接口的图在 pic_infos 字典里（values → largest/large/bmiddle/thumbnail.url）。"""
    urls = []
    infos = mb.get("pic_infos")
    if isinstance(infos, dict):
        for v in infos.values():
            if not isinstance(v, dict):
                continue
            for key in ("largest", "large", "bmiddle", "thumbnail"):
                u = v.get(key)
                if isinstance(u, dict) and u.get("url"):
                    urls.append(u["url"])
                    break
    for p in (mb.get("pics") or []):        # 有的卡片走这套老字段
        if isinstance(p, dict) and p.get("url"):
            urls.append(p["url"])
    out = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out[:9]


def weibo_parse_posts_desktop(items, max_n=20):
    """桌面接口（/ajax/statuses/mymblog）的 list → 与 m 站完全相同的前端字段集。

    两端字段名不一样（mblogid / text_raw / pic_infos），必须在服务端抹平，
    否则前端要为「数据是从哪条通道来的」写两套渲染。
    """
    out = []
    for mb in items or []:
        if not isinstance(mb, dict):
            continue
        rp = mb.get("retweeted_status") or None
        txt = mb.get("text_raw")
        out.append({
            "id": str(mb.get("id") or ""),
            "bid": str(mb.get("mblogid") or mb.get("bid") or ""),
            "text": (txt if txt else weibo_text(mb.get("text") or "")).strip(),
            "at": _wb_desktop_time(mb.get("created_at")),
            "from": weibo_text(mb.get("source") or ""),
            "pics": _wb_desktop_pics(mb),
            "reposts": _wb_int(mb.get("reposts_count")),
            "comments": _wb_int(mb.get("comments_count")),
            "likes": _wb_int(mb.get("attitudes_count")),
            "long": bool(mb.get("isLongText")),
            "retweet": ({
                "name": (rp.get("user") or {}).get("screen_name") or "",
                "text": ((rp.get("text_raw") or weibo_text(rp.get("text") or ""))[:400]),
                "pics": _wb_desktop_pics(rp)[:3],
            } if rp else None),
        })
        if len(out) >= max_n:
            break
    return out


def weibo_desktop_profile(uid, u):
    return {
        "uid": str(uid),
        "name": u.get("screen_name") or "",
        "avatar": u.get("profile_image_url") or "",
        "desc": u.get("description") or "",
        "followers": _wb_int(u.get("followers_count")),
        "follows": _wb_int(u.get("friends_count") or u.get("follow_count")),
        "posts": _wb_int(u.get("statuses_count")),
        "verified": u.get("verified_reason") or "",
        "home": weibo_home(uid),
    }


def weibo_mobile(uid, cookie):
    """m 站通道：访客也能拿到资料卡；登录态下正文一起给。"""
    prof, posts, err = None, [], ""
    try:
        d = weibo_get("%s?type=uid&value=%s&containerid=100505%s" % (WEIBO_API, uid, uid),
                      cookie=cookie, referer=weibo_home(uid))
        ui = (d.get("data") or {}).get("userInfo") or {}
        if ui:
            prof = weibo_profile_parse(uid, ui)
    except Exception as e:
        err = "资料获取失败：%s" % e
    try:
        d2 = weibo_get("%s?type=uid&value=%s&containerid=107603%s" % (WEIBO_API, uid, uid),
                       cookie=cookie, referer=weibo_home(uid))
        posts = weibo_parse_posts((d2.get("data") or {}).get("cards") or [])
    except Exception as e:
        err = (err + " / " if err else "") + "正文获取失败：%s" % e
    return prof, posts, err


def weibo_desktop(uid, cookie):
    """桌面站通道（weibo.com/ajax）：必须登录，但登录后数据最全、最不容易被风控。"""
    prof, posts, err = None, [], ""
    ref = "https://weibo.com/u/%s" % uid
    try:
        d = weibo_get("%s/profile/info?uid=%s" % (WEIBO_DESKTOP_API, uid),
                      cookie=cookie, referer=ref, mobile=False)
        if _wb_int(d.get("ok")) != 1:
            raise RuntimeError(d.get("msg") or "接口拒绝（多为未登录）")
        u = (d.get("data") or {}).get("user") or {}
        if u:
            prof = weibo_desktop_profile(uid, u)
    except Exception as e:
        err = "桌面资料失败：%s" % e
    try:
        d2 = weibo_get("%s/statuses/mymblog?uid=%s&page=1&feature=0" % (WEIBO_DESKTOP_API, uid),
                       cookie=cookie, referer=ref, mobile=False)
        if _wb_int(d2.get("ok")) != 1:
            raise RuntimeError(d2.get("msg") or "接口拒绝（多为未登录）")
        posts = weibo_parse_posts_desktop((d2.get("data") or {}).get("list") or [])
    except Exception as e:
        err = (err + " / " if err else "") + "桌面正文失败：%s" % e
    return prof, posts, err


def weibo_fetch(uid, force=False):
    """取一位的「资料 + 正文」。同一位的结果缓存一会儿：接口慢且容易触发风控。

    两条通道，各自的价值不同：
      ① m 站 —— **游客也有资料卡**，所以没登录时它至少能把资料填上；
      ② 桌面站 —— 必须登录，但登录后正文最稳、字段最全。
    先 ①，缺正文时用 ② 补，两个都没有才算真失败。这样「未登录 → 只看资料」
    与「登录 → 看正文」是同一段代码，不用分叉。
    """
    uid = str(uid or "").strip()
    if not uid.isdigit():
        return {"error": "没有配置微博 UID"}
    now = time.time()
    with _WEIBO_LOCK:
        hit = _WEIBO_CACHE.get(uid) or {}
    if not force and hit and now - hit.get("at", 0) < (
            WEIBO_TTL_OK if (hit.get("data") or {}).get("posts") else WEIBO_TTL_FAIL):
        return hit["data"]

    ck_cn = load_weibo_cookie("weibo.cn")
    ck_com = load_weibo_cookie("weibo.com")
    out = {"uid": uid, "logged": bool(ck_cn or ck_com), "profile": None,
           "posts": [], "need_login": False, "error": "", "via": ""}

    prof, posts, err = weibo_mobile(uid, ck_cn)
    if prof:
        out["profile"] = prof
    if posts:
        out["posts"] = posts
        out["via"] = "mobile"
    if err:
        out["error"] = err

    if not out["posts"] and ck_com:
        prof2, posts2, err2 = weibo_desktop(uid, ck_com)
        if prof2 and not out["profile"]:
            out["profile"] = prof2
        if posts2:
            out["posts"] = posts2
            out["via"] = "desktop"
        if err2:
            out["error"] = (out["error"] + " / " if out["error"] else "") + err2

    if not out["posts"]:
        out["need_login"] = True

    with _WEIBO_LOCK:
        _WEIBO_CACHE[uid] = {"at": now, "data": out}
    return out


def api_weibo(query):
    """GET /api/weibo —— 当前板块的微博（资料 + 正文）。"""
    st = cur_station()
    uid = str(st.get("weibo") or "").strip()
    if not uid:
        return 200, {"ok": False, "station": station_head(st),
                     "error": "这位还没配微博（在 data/stations.json 里加 weibo 字段）"}
    data = weibo_fetch(uid, force=("refresh" in query))
    out = dict(data)
    out["ok"] = bool(data.get("profile") or data.get("posts"))
    out["station"] = station_head(st)
    return 200, out


def api_weibo_cookie(obj):
    """POST /api/weibo/cookie —— 手动粘贴 cookie（扫码登录之外的一条稳的路）。"""
    v = str(obj.get("cookie") or "").strip()
    if v and "=" not in v:
        return 400, {"error": "这不像 Cookie：应当形如 SUB=…; SUBP=…"}
    try:
        if v:
            save_weibo_cookie(v)
        else:
            save_weibo_cookie_soft()
    except ValueError as e:
        return 400, {"error": str(e)}
    # 存完立刻试一次：只有真的能拿到正文才算「登录成功」
    uid = str(cur_station().get("weibo") or "")
    chk = weibo_fetch(uid, force=True) if uid else {}
    works = bool(chk.get("posts"))
    profile = bool(chk.get("profile"))
    if works:
        msg = ""
    elif profile:
        msg = "已保存：能看到资料卡，但正文仍取不到 —— 这一份 Cookie 是未登录（访客）状态"
    else:
        msg = "已保存，但连资料都取不到：Cookie 可能已失效或过期"
    return 200, {"ok": True, "logged": works,
                 "works": works, "profile": profile,
                 "posts": len(chk.get("posts") or []),
                 "error": msg}


def api_weibo_logout():
    save_weibo_cookie_soft()
    return 200, {"ok": True, "logged": False}


# ---------------- 应用内登录：启动一个浏览器登录，再用 CDP 把 cookie 取回来 ----------------
#
# 为什么要这样：微博的正文必须登录才给。让用户在本程序里重新实现一遍微博登录
# （扫码 / 密码 / 验证码）既不现实也容易随对方改版失效；不如开一个**独立 profile**
# 的浏览器窗口让用户在熟悉的环境里完成登录，登录态由本服务通过调试协议读回来。
# 读 cookie 用的 WS 客户端是项目里本来就有的那套（弹幕连 B 站用的，纯标准库）。

BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser():
    for p in BROWSER_CANDIDATES:
        if os.path.isfile(p):
            return p
    for name in ("chrome.exe", "msedge.exe"):
        w = shutil.which(name)
        if w:
            return w
    return ""


def _cdp_ws_open(host, port, path):
    """CDP 用的是明文 ws://（本地调试端口）。

    注意不能复用 _ws_handshake —— 那个函数默认套 TLS（给 wss 用的），
    对本地调试口会直接握手失败。
    """
    sock = socket.create_connection((host, port), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("CDP 握手期间连接被关闭")
        buf += chunk
    first = buf.split(b"\r\n", 1)[0].decode("latin-1")
    if "101" not in first:
        raise RuntimeError("CDP 握手失败：%s" % first)
    return sock


def cdp_targets(port):
    """列出调试端口下的所有 target（页面 / iframe …）。"""
    with urlopen("http://127.0.0.1:%d/json/list" % port, 6) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def cdp_call(ws_url, method, params=None, timeout=6):
    """在某个 target 上发一条 CDP 命令，返回 result（失败返回 None）。"""
    m = re.match(r"ws://([^:/]+):(\d+)(/.*)$", ws_url or "")
    if not m:
        return None
    try:
        sock = _cdp_ws_open(m.group(1), int(m.group(2)), m.group(3))
    except Exception:
        return None
    try:
        _ws_send(sock, json.dumps({"id": 1, "method": method,
                                   "params": params or {}}).encode(), 1)
        for _ in range(40):
            op, data = _ws_recv(sock, timeout)
            if op is None:
                return None
            if op == 9:                     # ping → pong
                _ws_send(sock, data, 10)
                continue
            if op != 1:
                continue
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("id") == 1:
                return msg.get("result") or {}
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def cdp_cookies(port):
    """连上调试端口，取浏览器当前的全部 cookie。"""
    for t in cdp_targets(port):
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            res = cdp_call(t["webSocketDebuggerUrl"], "Network.getAllCookies")
            if res is not None:
                return res.get("cookies") or []
    raise RuntimeError("没能从浏览器取到 cookie")


def cdp_navigate(port, url):
    """把登录窗口里的某个标签导航到 url。

    优先挑「不是 m 站」的那个标签：用户点「登录微博」时，窗口里应该落在桌面站上。
    窗口是复用的 —— 上一次可能停在 m 站（手机版登录页在电脑上根本走不通），
    不重新导航的话，用户点了按钮看到的还是上次那个手机版界面。
    """
    pages = [t for t in cdp_targets(port) if t.get("type") == "page"]
    if not pages:
        return False
    pick = next((t for t in pages if "m.weibo.cn" not in (t.get("url") or "")), pages[0])
    return cdp_call(pick.get("webSocketDebuggerUrl"),
                    "Page.navigate", {"url": url}) is not None


def _cookie_domain_match(dom, host):
    dom = (dom or "").lstrip(".")
    return bool(dom) and (host == dom or host.endswith("." + dom))


def weibo_cookie_header(cookies, host="weibo.com"):
    """把 CDP 的 cookie 列表拼成某个域要用的请求头。

    必须按域筛：浏览器对 weibo.com 和 m.weibo.cn 各有一份 SUB，全塞进一行会变成
    `SUB=A; SUB=B`，微博只会取第一个 —— 于是「桌面站登录了、m 站说没登录」。
    """
    out, seen = [], set()
    for c in cookies:
        name = c.get("name") or ""
        if not name or not _cookie_domain_match(c.get("domain"), host):
            continue
        if name in seen:               # 同一域里更深的那份优先（浏览器也这么做）
            out = [x for x in out if not x.startswith(name + "=")]
        seen.add(name)
        out.append("%s=%s" % (name, c.get("value")))
    return "; ".join(out)


def weibo_login_probe(cookies):
    """把浏览器里的 cookie 取回来试一次，看走到哪一步。

    返回 (state, nick, via)：
      "visitor" —— 拿到访客 cookie：资料卡能看，正文还不行（微博要求登录）
      "logged"  —— 正文也能取到：完整可用（via 说明是哪条通道给的）
      ""        —— cookie 还没成形（页面还没跑完访客流程）
    判据是**真的拿一次数据**，而不是看 cookie 里有没有某个名字 ——
    "SUB" 这个名字连访客态都有，只有能不能取到数据才是唯一标准。
    """
    jars = {}
    for host in ("weibo.com", "weibo.cn"):
        ck = weibo_cookie_header(cookies, host)
        if ck:
            jars[host] = ck
    if not jars:
        return "", "", ""
    for host, ck in jars.items():
        try:
            save_weibo_cookie(ck, host)
        except ValueError:
            return "", "", ""
    uid = str(cur_station().get("weibo") or "")
    if not uid:
        return "", "", ""
    chk = weibo_fetch(uid, force=True)
    nick = ((chk.get("profile") or {}).get("name")) or ""
    if chk.get("posts"):
        return "logged", nick, chk.get("via") or ""
    if chk.get("profile"):
        return "visitor", nick, ""
    return "", "", ""


def api_weibo_login_start():
    """POST /api/weibo/login —— 开一个浏览器窗口让用户登录微博。"""
    exe = find_browser()
    if not exe:
        return 400, {"error": "本机没找到 Chrome / Edge，无法开登录窗口。"
                              "可以在设置页手动粘贴 Cookie。"}
    # 前台标签直接开**桌面版登录页**（扫码 / 账号 / 微信三种方式都能用）；
    # 第二个标签开 m 站，是为了让访客 cookie 落到 .weibo.cn 上 ——
    # 不登录的时候，「微博」视图还能照样显示资料卡。
    uid = str(cur_station().get("weibo") or "")
    desk = weibo_login_url(uid)
    mob = ("https://m.weibo.cn/u/%s" % uid) if uid else "https://m.weibo.cn/"
    with _WB_LOGIN_LOCK:
        p = WEIBO_LOGIN.get("proc")
        if p is not None and p.poll() is None:
            # 复用已有窗口，但先把它导航回桌面站：上次可能停在 m 站，
            # 而用户再点「登录」就是想登录 —— 停在手机版页面等于白点。
            port0 = WEIBO_LOGIN.get("port") or WEIBO_LOGIN_PORT
            try:
                cdp_navigate(port0, desk)
            except Exception:
                pass
            return 200, {"ok": True, "state": WEIBO_LOGIN["state"], "reused": True}
        prof = os.path.join(APPDIR, "weibo-profile")
        os.makedirs(prof, exist_ok=True)
        args = [exe, "--remote-allow-origins=*",
                "--remote-debugging-port=%d" % WEIBO_LOGIN_PORT,
                "--user-data-dir=" + prof, "--no-first-run",
                "--no-default-browser-check", "--new-window", desk, mob]
        try:
            # 不要加 CREATE_NO_WINDOW：用户得在那个窗口里完成登录
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return 500, {"error": "启动浏览器失败：%s" % e}
        WEIBO_LOGIN.update({"proc": proc, "port": WEIBO_LOGIN_PORT, "state": "opened",
                            "error": "", "started": time.time(), "nick": ""})
    return 200, {"ok": True, "state": "opened",
                 "hint": "已打开微博电脑版登录页（扫码 / 账号 / 微信都可以）；"
                         "登录完成后这一页会自动解锁正文。"}


def api_weibo_login_poll():
    """GET /api/weibo/login/poll —— 轮询登录结果（前端每秒问一次）。"""
    if load_weibo_cookie():
        uid = str(cur_station().get("weibo") or "")
        d = weibo_fetch(uid) if uid else {}
        nick = ((d.get("profile") or {}).get("name")) or ""
        if d.get("posts"):
            return 200, {"state": "done", "logged": True, "nick": nick}
        if d.get("profile"):
            # 访客态：资料能看、正文不行。窗口还开着，用户还能在里头登录。
            return 200, {"state": "visitor", "logged": False, "nick": nick}
    with _WB_LOGIN_LOCK:
        st = WEIBO_LOGIN
        if not st.get("port") or st.get("state") == "idle":
            return 200, {"state": "idle", "logged": False}
        if time.time() - (st.get("started") or 0) > 900:
            st["state"] = "timeout"
            return 200, {"state": "timeout", "logged": False,
                         "error": "登录窗口开太久（超过 15 分钟），已放弃等待"}
        port = st["port"]
    try:
        cookies = cdp_cookies(port)
    except Exception as e:
        return 200, {"state": "waiting", "logged": False, "reason": str(e)[:80]}
    state, nick, via = weibo_login_probe(cookies)
    if state == "visitor":
        # 访客 cookie 先存下 —— 页面立刻就能显示资料卡，不用等用户登录
        with _WB_LOGIN_LOCK:
            WEIBO_LOGIN.update({"state": "visitor", "nick": nick})
        return 200, {"state": "visitor", "logged": False, "nick": nick}
    if state != "logged":
        return 200, {"state": "waiting", "logged": False, "reason": "还没检测到登录"}
    with _WB_LOGIN_LOCK:
        WEIBO_LOGIN.update({"state": "done", "nick": nick})
        proc = WEIBO_LOGIN.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()            # 登录态已经拿到，这个专用窗口就没用了
        except Exception:
            pass
    return 200, {"state": "done", "logged": True, "nick": nick}


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
    # 回放清单自动发现：只填了 name + UID 的主播靠这一步补齐（后台跑，不挡启动）
    series_kick()
    print("  退出时机：网页全部关闭后自动退出（另有 %d 分钟无心跳兜底）"
          % int(PAGE_IDLE // 60))

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
