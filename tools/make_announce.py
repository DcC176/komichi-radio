# -*- coding: utf-8 -*-
"""从 CHANGELOG.md 生成各版本的 Release 描述文件。

用法：
    python tools/make_announce.py

产物：build/announce/<tag>.md —— 每个文件即可直接用作 `gh release edit --notes-file`
或 `gh release create --notes-file` 的内容。

**为什么要有这个脚本**：版本公告的单一来源是仓库根的 CHANGELOG.md。
发版时只需在 CHANGELOG 顶部加一节，再跑本脚本，各版本描述的风格就天然一致 ——
不用手写、也不会出现「早期是使用说明模板、后期是技术叙事」那种风格断裂。

**标题骨架**（对齐 https://github.com/linshenkx/prompt-optimizer/releases 的写）：
    概括 → 亮点 → 产品更新 → 修复 → 破坏性变更 / 升级说明 → 开发者说明
前五节写在 CHANGELOG 的版本块里；「开发者说明」由本脚本按 git 实际提交生成，
「下载 / 校验 / 通用说明 / 第三方组件」作为公共尾部追加。

每个描述 = 该版本的公告章节 + 该版本自己的下载文件名与 SHA-256。
校验值优先取 GitHub 上已发布 Release 的数据；**新版本还没有 Release 时**
回退到本地产物（上传的就是它）。
"""
import hashlib
import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
OUT = os.path.join(BUILD, "announce")
os.makedirs(OUT, exist_ok=True)

# 1) 解析 CHANGELOG.md：版本块 + 两段公共尾部（通用说明 / 第三方组件）
# 按**所有**二级标题切分，再筛出需要的块 —— 只按版本号标题切的话，最后一个版本块
# 会一路吃到文末的「通用说明」（实测 v1.0.0 因此多出 500 字符）。
text = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
h2 = list(re.finditer(r'^## (.+)$', text, re.M))
blocks, common = {}, {}
for i, m in enumerate(h2):
    title = m.group(1).strip()
    start = m.end()
    end = h2[i + 1].start() if i + 1 < len(h2) else len(text)
    body = re.sub(r'\n---\s*$', '', text[start:end].strip()).strip()   # 去掉块尾的 --- 分隔
    vm = re.match(r'v\d+\.\d+\.\d+ · \d{4}-\d{2}-\d{2}\s*$', title)
    if vm:
        blocks[title.split(" ·")[0]] = body
    elif title in ("通用说明", "第三方组件"):
        common[title] = body

if "通用说明" not in common:
    raise SystemExit("CHANGELOG.md 缺少「## 通用说明」小节，无法生成公共尾部")


def version_key(tag):
    return [int(x) for x in tag.lstrip("v").split(".")]


ordered = sorted(blocks, key=version_key)


def git(*args):
    """跑 git 并回传文本；失败返回 None（别让脚本在没网 / 没 tag 的环境里崩）。"""
    try:
        return subprocess.run(("git",) + args, cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()
    except Exception:
        return None


def commit_range(tag):
    """返回 (范围描述, 提交数, 其中版本号提交数)；tag 不在本地时降级为 (None, None, None)。"""
    prev = None
    for t in ordered:
        if version_key(t) < version_key(tag):
            prev = t
    ranges = "%s..%s" % (prev, tag) if prev else tag
    if not git("rev-parse", "--verify", "--quiet", tag):
        return None, None, None
    subjects = [s.strip() for s in git("log", "--no-merges", "--pretty=%s",
                                       ranges).splitlines() if s.strip()]
    bumps = sum(1 for s in subjects if s.startswith("版本号"))
    return ranges, len(subjects), bumps

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
for tag in reversed(ordered):
    body = blocks[tag]
    m = meta.get(tag) or local_asset(tag)
    if not m or not m.get("name"):
        print("！%s：GitHub 上没有 Release、本地也没有产物，跳过（描述未生成）" % tag)
        continue

    # 开发者说明：可核查的事实（提交范围 / 提交数），不写验证细节 —— 早期版本没有记录，
    # 凭空补一句「已通过 XX 验证」比留空更糟。
    dev = ["", "### 开发者说明", ""]
    ranges, n, bumps = commit_range(tag)
    if ranges and n is not None:
        line = "- 本次发布范围是 `%s`，共 %d 个提交" % (ranges, n)
        dev.append(line + ("（其中 %d 个为版本号更新）。" % bumps if bumps else "。"))
    elif ranges:
        dev.append("- 本次发布范围是 `%s`。" % ranges)
    else:
        dev.append("- 该版本的本地标签缺失，无法自动给出提交范围。")

    tail = ["", "---", "",
            "**下载**：`%s`（%s 字节，Windows 64 位）" % (m["name"], format(m["size"], ","))]
    if m["sha"]:
        tail.append("**校验**：SHA-256 `%s`" % m["sha"])
    tail += ["", common["通用说明"]]
    # 第三方许可：随包第三方二进制 / 库的合规信息，分发产物必须携带。
    # v1.0.0 没有随包 FFmpeg 与 mpegts.js（这两个是 v1.1.0 才引入的），故不加。
    if tag != "v1.0.0" and "第三方组件" in common:
        tail += ["", "**第三方组件**", "", common["第三方组件"]]

    out = "\n".join([body] + dev + tail) + "\n"
    p = os.path.join(OUT, tag + ".md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(out)
    written.append(tag)
    print("%-8s %6d 字符  已生成" % (tag, len(out)))

print("\n共生成 %d 个文件 -> %s" % (len(written), OUT))
missing = [t for t in meta if t not in blocks]
if missing:
    print("⚠️ GitHub 上有 Release 但 CHANGELOG 里没有：%s" % ", ".join(sorted(missing)))
