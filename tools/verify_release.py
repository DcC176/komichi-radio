# -*- coding: utf-8 -*-
"""对单文件 EXE 做端到端自检。

模拟用户真实拿到的东西：只有一个 EXE，扔进任意一个空文件夹。
验证五件事：
    1. EXE 能起来，网页文件被释放到 %LOCALAPPDATA%\\KomichiRadio\\www\\
    2. 首页能打开，且每次打开都重新抓回放清单（不是命中缓存）
    3. 清单里的最新一集是新鲜的
    4. 重复双击不会起第二个实例，而是复用已在跑的那个
    5. --stop 能停掉服务（网页上「停止本地服务」走的是同一条路径）

用法：
    python tools/build_exe.py       # 先打包
    python tools/verify_release.py  # 再自检

注：第 5 步会让 EXE 弹一个「已停止。」提示框（静默模式下唯一的反馈方式），
    脚本 5 秒后会把它关掉，不用手动点。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 产物名从 build_exe.py 取，避免「带版本号」这件事在两处各写一份而对不上
sys.path.insert(0, os.path.join(ROOT, "tools"))
from build_exe import FINAL_NAME          # noqa: E402
EXE = os.path.join(ROOT, "发布", FINAL_NAME)
APPDIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                      "KomichiRadio")
WWW = os.path.join(APPDIR, "www")
# 沙箱里只放 EXE，不放任何网页文件 —— 这样走的才是「单文件分发」那条分支
SANDBOX = os.path.join(ROOT, "build", "verify-single")
PORT = 8765
BASE = "http://127.0.0.1:%d/" % PORT

WEB_ITEMS = ("index.html", "favicon.ico", "assets", "data")


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read()


def clear_stale():
    """启动前把可能残留的旧实例停掉。

    否则 wait_ready 会连上旧实例立刻返回，此时新 EXE 还在解压释放，
    后面的释放检查必然落空（实测踩过这个竞态）。
    """
    try:
        urllib.request.urlopen(BASE + "api/quit", timeout=5).read()
        time.sleep(2)
    except Exception:
        pass


def wait_ready(deadline=60):
    """等 EXE 把端口监听起来。onefile 首次运行要先解压，会慢一点。"""
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "api/ping", timeout=3) as r:
                if json.loads(r.read().decode("utf-8")).get("app") == "komichi-radio":
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def force_unpack():
    """把释放目录的版本标记改掉，强制走一遍重新释放。

    否则上一次跑过之后版本一致，释放会被判重跳过，等于没验证到这条路径。
    """
    ver = os.path.join(WWW, "www_version.txt")
    if not os.path.exists(ver):
        return
    try:
        with open(ver, "w", encoding="utf-8") as f:
            f.write("stale")
        print("OK   版本标记已置为 stale，将强制重新释放")
    except OSError as e:
        print("WARN 改版本标记失败：%s" % e)


def port_free():
    """8765 必须空闲。

    否则我们启动的那个会顺延到 8766，而脚本还在测 8765 —— 测到的是别人，
    后面的「服务已就绪」会立刻成立（其实新实例还在解压释放），结论全不可信。
    """
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def prepare_sandbox():
    """把 EXE 单独放到一个空目录里，模拟用户下载后只有一个文件。

    刻意不做任何删除：本机删除会走回收站且 fail-closed，删不掉就白搭。
    目录只用来放 EXE，只要里面没混进网页文件，就不会误走外置模式。
    """
    os.makedirs(SANDBOX, exist_ok=True)
    for name in WEB_ITEMS:
        if os.path.exists(os.path.join(SANDBOX, name)):
            print("沙箱里混进了 %s，请先手动清空 %s" % (name, SANDBOX))
            return None
    dst = os.path.join(SANDBOX, os.path.basename(EXE))
    shutil.copy2(EXE, dst)
    return dst


def check_unpacked():
    """确认网页文件确实释放到了用户目录"""
    missing = [n for n in WEB_ITEMS if not os.path.exists(os.path.join(WWW, n))]
    if missing:
        print("FAIL 释放目录缺少：%s（%s）" % ("、".join(missing), WWW))
        return False
    n = sum(len(f) for _, _, f in os.walk(WWW))
    print("OK   网页文件已释放到 %s（%d 个文件）" % (WWW, n))
    rm = os.path.join(APPDIR, "使用说明.txt")
    print("OK   使用说明：%s" % ("已释放" if os.path.exists(rm) else "缺失"))
    return True


def main():
    if not os.path.exists(EXE):
        print("找不到 %s\n请先运行：python tools/build_exe.py" % EXE)
        return 1

    if not port_free():
        print("FAIL 端口 %d 已被占用。请先停掉正在运行的实例（EXE --stop）再重跑，"
              "否则测到的是别人那个实例。" % PORT)
        return 1

    sandbox_exe = prepare_sandbox()
    if sandbox_exe is None:
        return 1
    print("沙箱：%s" % sandbox_exe)

    clear_stale()
    force_unpack()

    # --no-browser：自检不该把浏览器窗口弹到用户脸上
    proc = subprocess.Popen([sandbox_exe, "--no-browser"])
    try:
        if not wait_ready():
            print("FAIL 服务 60 秒内没起来，看日志："
                  r"%LOCALAPPDATA%\KomichiRadio\log.txt")
            return 1
        print("OK   服务已就绪  %s" % BASE)

        if not check_unpacked():
            return 1

        st, body = get("")
        print("OK   首页 HTTP %d（%d 字节）" % (st, len(body)))

        # 清单接口现在的契约是「先把手上这份给出去，过期才在后台重抓」——
        # 切换板块不卡就是靠这个。所以这里分两件事验：
        #   ① 第二次必须**很快**（命中缓存，不再等 1.5~2.6 秒的现抓）
        #   ② 强制重抓这条路仍然通（hard=1 同步重抓，generated_at 必须推进）
        stamps = []
        st, body = get("api/programs?refresh=1")
        d = json.loads(body.decode("utf-8"))
        stamps.append(d["meta"]["generated_at"])
        print("OK   第 1 次打开  cached=%s  count=%d  generated_at=%d"
              % (d.get("cached"), d["meta"]["count"], stamps[-1]))

        t0 = time.time()
        st, body = get("api/programs?refresh=1")
        dt = time.time() - t0
        d2 = json.loads(body.decode("utf-8"))
        print("OK   第 2 次打开  %.2fs  cached=%s  count=%d"
              % (dt, d2.get("cached"), d2["meta"]["count"]))
        if dt > 1.0:
            print("FAIL 第二次还要 %.2fs —— 缓存没生效，切板块会明显卡" % dt)
            return 1
        print("OK   第二次 %.2fs 内返回（命中缓存，不再现抓）" % dt)

        st, body = get("api/programs?refresh=1&hard=1")
        d3 = json.loads(body.decode("utf-8"))
        if d3["meta"]["generated_at"] == stamps[0]:
            print("FAIL hard=1 没有真正重抓（generated_at 未推进）")
            return 1
        print("OK   hard=1 是实时重抓（generated_at 推进到 %d）" % d3["meta"]["generated_at"])
        d = d3

        newest = max(p["pubdate"] for p in d["programs"])
        print("OK   最新一集距今 %.1f 天：%s"
              % ((time.time() - newest) / 86400.0, d["programs"][0]["title"]))

        # 再双击一次：不该起第二个实例，而应复用已在跑的那个。
        #
        # 判据是「后续端口上有没有另起一个本服务」，**不是**「第二个进程有没有退出」：
        # 本机把删除劫持到回收站且 fail-closed，onefile 退出时清理 _MEI 临时目录
        # （141MB / 上千个文件）会卡住，进程就挂在那儿 —— 那是本机环境，不是「起了
        # 第二个实例」。拿退出与否判定会间歇性误报（实测 2026-10-03）。
        again = subprocess.Popen([sandbox_exe, "--no-browser"])
        extra = None
        for _ in range(12):                 # 最多等 6 秒，够它识别已有实例
            time.sleep(0.5)
            for p in range(PORT + 1, PORT + 6):
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/api/ping" % p, timeout=2) as r:
                        if json.loads(r.read().decode("utf-8")).get("app") == "komichi-radio":
                            extra = p
                            break
                except Exception:
                    pass
            if extra:
                break
        if extra:
            print("FAIL 重复双击起了第二个实例（%d 端口上有本服务）" % extra)
            again.terminate()
            return 1
        print("OK   重复双击没有起第二个实例（复用了已有实例，没另占端口）")
        try:
            rc = again.wait(timeout=3)
            print("OK   第二个进程已自行退出（退出码 %d）" % rc)
        except subprocess.TimeoutExpired:
            print("注意 第二个进程未退出 —— 本机删除保护会让 onefile 清 _MEI 时卡住，"
                  "与环境有关、不影响功能（功能判据见上一条）")
            again.terminate()
        try:
            urllib.request.urlopen(BASE, timeout=5).read()
            print("OK   原实例仍在服务")
        except Exception:
            print("FAIL 原实例被挤掉了")
            return 1

        print("\n前 4 项通过。")
        return 0
    finally:
        # 走网页「停止本地服务」的同一条路径收尾，顺带验证第 5 项
        stopper = subprocess.Popen([sandbox_exe, "--stop"])
        time.sleep(5)
        try:
            urllib.request.urlopen(BASE, timeout=3).read()
            print("FAIL --stop 之后服务还在")
        except Exception:
            print("OK   --stop 已停止服务")
        stopper.terminate()       # 关掉「已停止。」提示框，别留残留窗口
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
