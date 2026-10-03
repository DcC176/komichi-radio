# -*- coding: utf-8 -*-
"""从 CHANGELOG.md 生成各版本的 Release 描述文件。

用法：
    python tools/make_announce.py

产物：build/announce/<tag>.md —— 每个文件即可直接用作 `gh release edit --notes-file`
或 `gh release create --notes-file` 的内容。

**为什么要有这个脚本**：版本公告的单一来源是仓库根的 CHANGELOG.md。
发版时只需在 CHANGELOG 顶部加一节，再跑本脚本，各版本描述的风格就天然一致 ——
不用手写、也不会出现「早期是使用说明模板、后期是技术叙事」那种风格断裂。

每个描述 = 该版本的公告章节 + 该版本自己的下载文件名与 SHA-256。
校验值优先取 GitHub 上已发布 Release 的数据；**新版本还没有 Release 时**
回退到本地产物（上传的就是它）。
"""
import hashlib
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
OUT = os.path.join(BUILD, "announce")
os.makedirs(OUT, exist_ok=True)

# 1) 解析 CHANGELOG.md 的版本块
# 按**所有**二级标题切分，再筛出版本块 —— 只按版本号标题切的话，最后一个版本块
# 会一路吃到文末的「通用说明」（实测 v1.0.0 因此多出 500 字符）。
text = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
h2 = list(re.finditer(r'^## (.+)$', text, re.M))
blocks = {}
for i, m in enumerate(h2):
    if not re.match(r'v\d+\.\d+\.\d+ · \d{4}-\d{2}-\d{2}\s*$', m.group(1).strip()):
        continue
    tag = m.group(1).strip().split(" ·")[0]
    start = m.end()
    end = h2[i + 1].start() if i + 1 < len(h2) else len(text)
    body = text[start:end].strip()
    body = re.sub(r'\n---\s*$', '', body).strip()      # 去掉块尾的 --- 分隔
    blocks[tag] = body

# 2) 各版本的资产名、字节数、SHA-256
#    优先用 GitHub 上已发布 Release 的数据（那是使用者实际下载到的东西）；
#    新版本此刻还没有 Release，回退到本地产物 —— 待上传的就是它。
rs = []
_gh = os.path.join(BUILD, "gh-releases.json")
if os.path.exists(_gh):
    rs = json.load(open(_gh, encoding="utf-8"))
meta = {}
for r in rs:
    a = r["assets"][0] if r["assets"] else None
    mm = re.search(r'([0-9a-f]{64})', r.get("body") or "")
    meta[r["tag_name"]] = {
        "name": a["name"] if a else "",
        "size": a["size"] if a else 0,
        "sha": mm.group(1) if mm else "",
    }


def local_asset(tag):
    """从本地产物算出（上传名、字节数、SHA-256）。

    本地文件名是中文（`发布/二十四时小路电台-vX.Y.Z.exe`，发布约定要求 `发布/` 只放中文名），
    而上传到 Release 的资产名固定为 ASCII 的 `KomichiRadio-vX.Y.Z.exe`。
    """
    p = os.path.join(ROOT, "发布", "二十四时小路电台-%s.exe" % tag)
    if not os.path.exists(p):
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"name": "KomichiRadio-%s.exe" % tag,
            "size": os.path.getsize(p), "sha": h.hexdigest()}


written = []
for tag, body in blocks.items():
    m = meta.get(tag) or local_asset(tag)
    if not m or not m.get("name"):
        print("！%s：GitHub 上没有 Release、本地也没有产物，跳过（描述未生成）" % tag)
        continue
    tail = ["", "---", ""]
    tail.append("**下载**：`%s`（%s 字节，Windows 64 位）" % (m["name"], format(m["size"], ",")))
    if m["sha"]:
        tail.append("**校验**：SHA-256 `%s`" % m["sha"])
    tail += ["",
             "怎么用：下载后双击运行即可，只需要这一个文件，会自动打开浏览器并开始播放。",
             "怎么关：播放页面右下角点「停止本地服务」。",
             "",
             "非官方粉丝站，内容版权归各位 UP 主所有。"]
    # 许可声明：随包第三方二进制/库的合规信息，分发产物必须携带。
    # v1.0.0 没有随包 FFmpeg 与 mpegts.js（那两个是 v1.1.0 才引入的），故不加。
    if tag != "v1.0.0":
        tail += ["",
                 "**第三方组件**：",
                 "- 随包 FFmpeg 为 GPL v3 构建（configure 含 `--enable-gpl --enable-version3`），",
                 "  以独立可执行文件经子进程调用，不与本程序代码链接；",
                 "  许可全文与源码出处见仓库内 `ffmpeg.LICENSE.txt`。",
                 "- 页面内嵌 mpegts.js 1.7.3（Apache-2.0），见仓库内 `assets/mpegts.LICENSE.txt`。"]
    out = body + "\n" + "\n".join(tail)
    p = os.path.join(OUT, tag + ".md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(out)
    written.append(tag)
    print("%-8s %6d 字符  已生成" % (tag, len(out)))

print("\n共生成 %d 个文件 -> %s" % (len(written), OUT))
missing = [t for t in meta if t not in blocks]
if missing:
    print("⚠️ GitHub 上有 Release 但 CHANGELOG 里没有：%s" % ", ".join(sorted(missing)))
