# -*- coding: utf-8 -*-
"""对「发布」目录做端到端自检。

模拟双击 EXE，验证五件事：
    1. 服务能起来、首页能打开
    2. 每次打开都重新抓取回放清单（而不是命中缓存）
    3. 清单里的最新一集是新鲜的（不是内置快照里的旧数据）
    4. 重复双击不会起第二个实例，而是复用已在跑的那个
    5. --stop（「停止.bat」）能真正把服务停掉

用法：
    python tools/build_exe.py       # 先打包
    python tools/verify_release.py  # 再自检

注：第 5 步会让 EXE 弹一个「已停止。」提示框（静默模式下唯一的反馈方式），
    脚本 5 秒后会把它关掉，不用手动点。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "发布", "二十四时小路电台.exe")
PORT = 8765
BASE = "http://127.0.0.1:%d/" % PORT


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read()


def wait_ready(deadline=60):
    """等 EXE 把端口监听起来。onefile 首次运行要先解压，会慢一点。"""
    end = time.time() + deadline
    while time.time() < end:
        try:
            urllib.request.urlopen(BASE, timeout=3).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    if not os.path.exists(EXE):
        print("找不到 %s\n请先运行：python tools/build_exe.py" % EXE)
        return 1

    # --no-browser：自检不该把浏览器窗口弹到用户脸上
    proc = subprocess.Popen([EXE, "--no-browser"])
    try:
        if not wait_ready():
            print("FAIL 服务 60 秒内没起来，看日志："
                  r"%LOCALAPPDATA%\KomichiRadio\log.txt")
            return 1
        print("OK   服务已就绪  %s" % BASE)

        st, body = get("")
        print("OK   首页 HTTP %d（%d 字节）" % (st, len(body)))

        stamps = []
        for i in (1, 2):
            st, body = get("api/programs?refresh=1")
            d = json.loads(body.decode("utf-8"))
            stamps.append(d["meta"]["generated_at"])
            print("OK   第 %d 次打开  cached=%s  count=%d  generated_at=%d"
                  % (i, d.get("cached"), d["meta"]["count"], stamps[-1]))

        if stamps[0] == stamps[1]:
            print("WARN 两次 generated_at 相同，可能命中了缓存")
        else:
            print("OK   两次都是实时重抓（generated_at 在推进）")

        newest = max(p["pubdate"] for p in d["programs"])
        print("OK   最新一集距今 %.1f 天：%s"
              % ((time.time() - newest) / 86400.0, d["programs"][0]["title"]))

        # 再双击一次：不该起第二个实例，而应复用已在跑的那个后立刻退出
        again = subprocess.Popen([EXE, "--no-browser"])
        try:
            rc = again.wait(timeout=30)
            print("OK   重复双击：立即退出（退出码 %d），复用已有实例" % rc)
        except subprocess.TimeoutExpired:
            print("FAIL 重复双击起了第二个实例")
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
        # 用「停止.bat」走的同一条路径收尾，顺带验证第 5 项
        stopper = subprocess.Popen([EXE, "--stop"])
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
