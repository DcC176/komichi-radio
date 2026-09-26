# -*- coding: utf-8 -*-
"""把本地服务打包成单文件 EXE。

用法：
    python tools/build_exe.py

产物：
    发布/二十四时小路电台.exe
        单文件，自带 Python 运行时与全部网页文件，双击即用。
        分发时只需要给这一个文件。

网页文件怎么进去的
    index.html / favicon.ico / assets / data / readme.txt 全部用 --add-data 打进 EXE。
    EXE 首次运行时把它们释放出来（网页进 %LOCALAPPDATA%\\KomichiRadio\\www\\，
    使用说明放同级目录），之后从释放出来的目录提供服务。

    这样分发只要一个 EXE；同时文件确实落在磁盘上，想改前端或看说明都还找得到。
    开发时若 EXE 同目录放了 index.html，则优先用外置的，改前端不用重新打包。

    PyInstaller 的 --name 用 ASCII（KomichiRadio），打包完再改名成中文，
    避开中文名在 spec / build 中间产物上的编码坑。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "发布")
BUILD = os.path.join(ROOT, "build")

PYI_NAME = "KomichiRadio"                 # PyInstaller 内部用名（ASCII）
FINAL_NAME = "二十四时小路电台.exe"        # 给用户的文件名
WEB = ["index.html", "favicon.ico", "assets", "data"]   # 网页文件，释放到 www\
README_SRC = "readme.txt"                 # 使用说明，释放到用户目录根

README = """二十四时小路电台 · 使用说明
========================================

【怎么用】
双击「二十四时小路电台.exe」。它会自动打开浏览器并开始播放。
之后想再看，直接开浏览器访问 http://127.0.0.1:8765/ 就行。

【怎么关】
在播放页面右下角点「停止本地服务」。
也可以在任务管理器里结束「二十四时小路电台.exe」。

【注意】
1. 只需要这一个 EXE，不用额外拷任何文件。
   首次运行会把网页文件释放到 %LOCALAPPDATA%\\KomichiRadio\\www\\。
2. 每次打开页面都会重新拉取最新回放列表，不用手动刷新。
   第一次约 1~2 秒，期间页面会先显示内置快照。
3. 想看 1080P：点播放器右上角「登录」，用 B 站 App 扫码。
   登录凭据存在 %LOCALAPPDATA%\\KomichiRadio\\sessdata.txt，
   只发给 B 站自己的接口，不外传。
4. 出问题看日志：%LOCALAPPDATA%\\KomichiRadio\\log.txt
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
    """打包成单文件、无控制台窗口的 EXE，网页文件与说明一并内置"""
    os.makedirs(BUILD, exist_ok=True)

    # 版本标记：EXE 内一份、释放目录一份，比对决定要不要重新释放
    ver_file = os.path.join(BUILD, "www_version.txt")
    with open(ver_file, "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y%m%d%H%M%S"))

    readme_file = os.path.join(BUILD, README_SRC)
    with open(readme_file, "w", encoding="utf-8") as f:
        f.write(README)

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

    # 网页文件与说明：单个文件放根目录，目录按原名放，与 serve.py 的释放逻辑对应
    for item in WEB:
        p = os.path.join(ROOT, item)
        if not os.path.exists(p):
            print("缺少 %s，无法打包" % p)
            sys.exit(1)
        dest = "." if os.path.isfile(p) else item
        args += ["--add-data", p + os.pathsep + dest]
    args += ["--add-data", ver_file + os.pathsep + "."]
    args += ["--add-data", readme_file + os.pathsep + "."]

    icon = os.path.join(ROOT, "favicon.ico")     # 由 tools/make_icon.py 生成
    if os.path.exists(icon):
        args += ["--icon", icon]
    args.append(os.path.join(ROOT, "tools", "serve.py"))

    print("$ " + " ".join(args))
    subprocess.check_call(args, cwd=ROOT)

    src = os.path.join(OUT, PYI_NAME + ".exe")
    dst = os.path.join(OUT, FINAL_NAME)
    # 直接覆盖，不产生需要删除的中间文件。
    # Windows 上刚写出来的 EXE 会被 Defender / 索引器短暂占用，rename 会报 WinError 5，
    # 所以重试几次再放弃（实测第一次常失败、隔一两秒就好）。
    for i in range(10):
        try:
            os.replace(src, dst)
            break
        except PermissionError:
            if i == 9:
                raise
            time.sleep(1)
    return dst


def main():
    ensure_pyinstaller()
    os.makedirs(OUT, exist_ok=True)

    exe = build_exe()

    print("\n完成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1048576.0))
    print("分发时只需要这一个 EXE。")
    print("首次运行会把网页文件释放到 %LOCALAPPDATA%\\KomichiRadio\\。")


if __name__ == "__main__":
    sys.exit(main())
