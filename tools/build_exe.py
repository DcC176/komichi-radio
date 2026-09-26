# -*- coding: utf-8 -*-
"""把本地服务打包成单文件 EXE，并组装出可直接分发的「发布」目录。

用法：
    python tools/build_exe.py

产物（全部在 发布/ 里）：
    二十四时小路电台.exe   单文件，自带 Python 运行时，双击即用
    index.html / assets/ / data/    网页文件，与 EXE 同目录
    停止.bat                停止正在运行的服务
    使用说明.txt

为什么要这样分
    EXE 里只放 Python 运行时和 collect.py；网页文件外置在同目录 ——
    改 app.js / style.css 立刻生效，不用重新打包。

注意：网页文件是外置的，分发时必须整个「发布」目录一起拷，不能只拷 EXE。

    PyInstaller 的 --name 用 ASCII（KomichiRadio），打包完再改名成中文，
    避开中文名在 spec / build 中间产物上的编码坑。
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "发布")
BUILD = os.path.join(ROOT, "build")

PYI_NAME = "KomichiRadio"                 # PyInstaller 内部用名（ASCII）
FINAL_NAME = "二十四时小路电台.exe"        # 给用户的文件名
WEB = ["index.html", "favicon.ico", "assets", "data"]   # 与 EXE 同目录的网页文件

STOP_BAT = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0{name}" --stop
""".format(name=FINAL_NAME)

README = """二十四时小路电台 · 使用说明
========================================

【怎么用】
双击「二十四时小路电台.exe」。它会自动打开浏览器并开始播放，
之后想再看，直接开浏览器访问 http://127.0.0.1:8765/ 就行。

【怎么关】
双击「停止.bat」。也可以在任务管理器里结束「二十四时小路电台.exe」。

【注意】
1. EXE 必须和 index.html、assets、data 放在同一个文件夹里，不要只拷 EXE，
   否则会提示「缺少网页文件 index.html」。
2. 每次打开页面都会重新拉取最新回放列表，不用手动刷新。
   第一次约 1~2 秒，期间页面会先显示内置快照。
3. 想看 1080P：点播放器右上角「登录」，用 B 站 App 扫码。
   登录凭据存在 %%LOCALAPPDATA%%\\KomichiRadio\\sessdata.txt，只发给 B 站接口，不外传。
4. 出问题看日志：%%LOCALAPPDATA%%\\KomichiRadio\\log.txt
5. 如果 8765 端口被别的程序占用，会自动顺延到 8766、8767……
   实际地址看日志。
"""


def ensure_pyinstaller():
    try:
        import PyInstaller          # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，请先运行：")
        print("    %s -m pip install pyinstaller" % sys.executable)
        sys.exit(1)


def build_exe():
    """打包成单文件、无控制台窗口的 EXE"""
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",                  # 覆盖上次产物，不弹交互确认
        "--onefile",                    # 单文件：Python 运行时一起打进去
        "--noconsole",                  # 静默：双击后不出现黑窗口
        "--name", PYI_NAME,
        "--distpath", OUT,
        "--workpath", os.path.join(BUILD, "pyi"),
        "--specpath", BUILD,
        # collect.py 是运行时用 importlib 动态加载的，PyInstaller 静态分析看不到，
        # 必须显式带上，否则 /api/programs 实时清单会整体失败。
        # 路径必须写绝对路径：--specpath 之后，相对路径会被按 spec 所在目录解析。
        "--add-data", os.path.join(ROOT, "tools", "collect.py") + os.pathsep + "tools",
    ]
    icon = os.path.join(ROOT, "favicon.ico")     # 由 tools/make_icon.py 生成
    if os.path.exists(icon):
        args += ["--icon", icon]
    args.append(os.path.join(ROOT, "tools", "serve.py"))
    print("$ " + " ".join(args))
    subprocess.check_call(args, cwd=ROOT)

    src = os.path.join(OUT, PYI_NAME + ".exe")
    dst = os.path.join(OUT, FINAL_NAME)
    os.replace(src, dst)                # 直接覆盖，不产生需要删除的中间文件
    return dst


def copy_web():
    """网页文件拷到 EXE 旁边（已存在则覆盖，不删目录）"""
    for item in WEB:
        src = os.path.join(ROOT, item)
        dst = os.path.join(OUT, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        print("  + %s" % item)


def write_text(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(text)
    print("  + %s" % name)


def main():
    ensure_pyinstaller()
    os.makedirs(OUT, exist_ok=True)

    exe = build_exe()
    print("\n组装发布目录：%s" % OUT)
    copy_web()
    write_text("停止.bat", STOP_BAT)
    write_text("使用说明.txt", README)

    print("\n完成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1048576.0))
    print("分发时请整个「发布」目录一起拷。")


if __name__ == "__main__":
    sys.exit(main())
