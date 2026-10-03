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
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "发布")
BUILD = os.path.join(ROOT, "build")

APP_VERSION = "1.5.9"                     # 程序版本号，每版递增
PYI_NAME = "KomichiRadio"                 # PyInstaller 内部用名（ASCII）
# 给用户的文件名带版本号 —— 发布目录里会同时存在多个版本，一眼能看出哪个是新的。
# 内部标识（APP_TAG、释放目录、实例探测）都走 HTTP 或固定字符串，**不依赖这个文件名**，
# 所以改名不影响单实例检测与 --stop。
FINAL_NAME = "二十四时小路电台-v%s.exe" % APP_VERSION
WEB = ["index.html", "favicon.ico", "assets", "data"]   # 网页文件，释放到 www\
README_SRC = "readme.txt"                 # 使用说明，释放到用户目录根

README = """二十四时小路电台 · 使用说明
========================================

【怎么用】
双击「{{NAME}}」。它会自动打开浏览器并开始播放。
之后想再看，直接开浏览器访问 http://127.0.0.1:8765/ 就行。

【怎么关】
在播放页面右下角点「停止本地服务」。
也可以在任务管理器里结束「{{NAME}}」。

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
"""                                 # 下面再替换文件名：说明里有 %LOCALAPPDATA%，
                                    # 走 % 格式化会把它当成占位符报错，所以用 replace
README = README.replace("{{NAME}}", FINAL_NAME)


def ensure_pyinstaller():
    try:
        import PyInstaller          # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，请先运行：")
        print("    %s -m pip install pyinstaller" % sys.executable)
        sys.exit(1)


# data/ 下**不该进分发包**的东西：全是运行时数据，体积可到几十上百 MB，
# 而且每位用户各一份、可重算。`--add-data` 不读 .gitignore，所以必须显式剔除
# （实测漏剔除时 EXE 从 54MB 涨到 80MB：chu2u 的两个临时 chunk.wav 就占 32MB）。
DATA_EXCLUDE_DIRS = {"stations"}            # 各主播的清单缓存/分段结果/音频中间产物
DATA_EXCLUDE_SUFFIX = (".bak", ".bak_test")


def _stage_data():
    """把 data/ 复制一份**不含运行时数据**的暂存副本，返回它的路径。

    用唯一的目录名（`data-stage-<时间戳>`）而不是固定名：本机删除被劫持到回收站
    且 fail-closed，固定名会因为「上一次的副本删不掉」而反复失败。
    而且不删旧副本也不影响正确性 —— 每次都用新的那个来打包。

    硬链接优先（省时间省空间，同盘即可），失败则退回普通复制。
    """
    stage = os.path.join(BUILD, "data-stage-%d" % int(time.time()))
    src_root = os.path.join(ROOT, "data")
    os.makedirs(stage, exist_ok=True)

    linked = copied = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        # 顶层就砍掉排除目录（stations/），不进它的子树
        if rel == ".":
            dirnames[:] = [d for d in dirnames if d not in DATA_EXCLUDE_DIRS]
        dst_dir = stage if rel == "." else os.path.join(stage, rel)
        os.makedirs(dst_dir, exist_ok=True)
        for fn in filenames:
            if fn.endswith(DATA_EXCLUDE_SUFFIX):
                continue
            s = os.path.join(dirpath, fn)
            d = os.path.join(dst_dir, fn)
            try:
                os.link(s, d)               # 硬链接：同一份数据，不占额外空间
                linked += 1
            except OSError:
                shutil.copy2(s, d)
                copied += 1

    print("data 暂存：%s（硬链接 %d、复制 %d；已剔除 %s）"
          % (os.path.basename(stage), linked, copied,
             "、".join(sorted(DATA_EXCLUDE_DIRS))))
    return stage


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
        # auto_segments.py 同理：网页里的「自动分段」就是在进程内调它的函数。
        # 路径必须写绝对路径：--specpath 之后，相对路径会被按 spec 所在目录解析。
        "--add-data", os.path.join(ROOT, "tools", "collect.py") + os.pathsep + "tools",
        "--add-data", os.path.join(ROOT, "tools", "auto_segments.py") + os.pathsep + "tools",
        # 边界精修：读画面「已唱」浮层峰值定歌边界，靠它才不会把歌从中间切开。
        # 实测纯音频 25 段 / 带精修 27 段，与已有产物一致的是带精修那份 —— 必须一起打包。
        "--add-data", os.path.join(ROOT, "tools", "seg_refine.py") + os.pathsep + "tools",
        # 它们只被动态加载的 seg_refine 用到，静态分析看不到，得显式声明
        # （numpy 34MB + pillow 7MB，EXE 会从 9.6MB 涨到 40MB 上下）。
        "--hidden-import", "numpy",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
    ]

    # FFmpeg：自动分段要把音频解码成 PCM，没有它这个功能在别人机器上直接不可用
    # （find_ffmpeg 的兜底目录遍历只在个别机器上碰巧命中）。一起打包。
    # 不把二进制放仓库里（84MB 进 git 太蠢），构建时按 find_ffmpeg 的同一套搜索找。
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from auto_segments import find_ffmpeg          # noqa: E402
    ff = find_ffmpeg()
    if ff:
        args += ["--add-data", ff + os.pathsep + "ffmpeg"]
        print("打包 FFmpeg：%s（%.0f MB）" % (ff, os.path.getsize(ff) / 1048576))
    else:
        print("！没找到 ffmpeg —— 打出来的 EXE 自动分段不可用（其余功能正常）")
    lic = os.path.join(ROOT, "ffmpeg.LICENSE.txt")
    if os.path.exists(lic):
        args += ["--add-data", lic + os.pathsep + "."]

    # 网页文件与说明：单个文件放根目录，目录按原名放，与 serve.py 的释放逻辑对应
    for item in WEB:
        p = os.path.join(ROOT, item)
        if not os.path.exists(p):
            print("缺少 %s，无法打包" % p)
            sys.exit(1)
        if item == "data":
            p = _stage_data()          # data 要剔除运行时子目录，见下
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
                # 最常见的原因不是 Defender，而是**改名目标还被占着**：
                # 预览器 / 资源管理器打开过它，或者上一个同名产物正在被另一个进程读。
                # 本机又删不掉它，唯一干净的出路是换版本号重打，所以这里说清楚。
                sys.exit("改名为「%s」失败（PermissionError）：目标多半被预览器/资源管理器占着，"
                         "或该版本号已经打出过一份。\n请先关闭占用它的窗口，"
                         "或把 APP_VERSION 递增一档后重新打包。" % FINAL_NAME)
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
