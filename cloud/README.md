# 二十四时小路电台 · 云端部署包

这是给云平台用的发布包，**只有前端与一个代理服务**，不含任何本地分析产物。

```
cloud/
├── server/app.py      云端服务（纯 Python 标准库，无第三方依赖）
└── site/              静态站点（index.html + assets + data）
```

## 运行在什么服务器上

**不是传统云主机（ECS/VPS），也不是 Vercel/Netlify 那类静态托管。**
它是 WorkBuddy 的「发布为应用」托管平台，底座是腾讯 Cloud Studio：

| 项 | 实际情况 |
| --- | --- |
| 域名 | `*.sg.agentos-app.run`（新加坡区节点） |
| 运行方式 | 平台起一个容器，按 `Procfile` 跑 `python server/app.py` |
| 端口 | 平台注入 `PORT`，**只暴露这一个端口**，外部经 HTTPS 反代进入 |
| 运行时 | Python 3.11（容器内实测），纯标准库，无第三方依赖 |
| 数据库 / 缓存 / 消息队列 | **均不提供**。所有状态都在进程内存里 |
| 计费 | 无需自行购买服务器；出口带宽走平台额度 |

### 部署机制的两个重要事实（实测）

1. **每次发布都会分配一个全新的随机域名，旧链接立即失效。**
   实测 9 次发布得到 9 个不同 hash 前缀；早期链接（如第 1、8 次）现在返回
   400。**发布前请把旧链接告知使用者，避免他们拿着失效地址。**
2. **`domainPrefix` 参数不会改变最终链接。**
   传 `domainPrefix="komichi-radio-24h"` 后，链接仍是随机 hash；
   而 `komichi-radio-24h.sg.agentos-app.run` 的 DNS 虽已解析
   （指向 `43.160.158.96`）但网关返回 400，即该前缀域名**未被绑定到应用**。
   → 想要可读域名，只能走「自有域名 + CNAME」或在应用的域名设置里绑定，
   不能靠发布参数实现。

## 为什么需要这个代理

浏览器的 `<video>` 无法直接播 B 站视频流：CDN 校验 `Referer`（缺了返回 403），
而网页无法伪造 `Referer`。所以必须由服务端代取地址、代转媒体流。

## 与本地版 `tools/serve.py` 的差异（都是刻意的）

| 项 | 本地版 | 云端版 |
| --- | --- | --- |
| 监听 | `127.0.0.1:8765` | `0.0.0.0:$PORT`（平台注入端口） |
| 登录凭据 | 读 `tools/sessdata.txt`（本机文件），可到 1080P | **不落盘**：凭据来自访客自己的浏览器 Cookie，随请求透传 |
| CORS | 不限制（单机自用） | **只允许白名单 Host**，默认仅同源 |
| 媒体转发 | 不限域名 | **只允许 B 站 CDN**（`*.bilivideo.com/.cn`、`*.bilibili.com`） |
| 静态根 | 项目根目录（含 tools/） | 仅 `site/` |

**凭据为什么不能"放到服务器上"**：`sessdata.txt` 是账号级 Cookie，等价于登录态。
把它写进公网服务器的文件里，等于公开你的 B 站账号 —— 服务器一旦被入侵或被运维者
读取，账号即失守；而且所有访客的请求都会带着同一个身份。

**但这不等于云端不能有 1080P。** 正确做法是让**每个访客用他自己的账号**：

```
访客扫码 → 后端从 B 站 Set-Cookie 取出 SESSDATA
         → 写进**访客自己**的浏览器 Cookie（ksess，HttpOnly + SameSite=Lax）
         → 之后每次请求自动带上，后端读出、仅用于向 B 站取流
         → 服务端不写文件、不记日志
```

匿名访客不受影响，仍是 480P —— **登录只是可选的画质升级，不是访问门槛**。

## 接口

```
GET /api/playurl?bvid=&cid=&qn=   取播放地址与可选清晰度（DASH 优先，失败退回 MP4）
GET /api/stream?u=<base64url>     转发媒体流（支持 Range，可拖动进度）
GET /api/dash?u=<base64url>       转发 DASH 分片
GET /api/diag                     自检：暴露每一跳的原始结果（排查线上故障第一步）
GET /api/status                   当前访客的登录态与可选清晰度（匿名 → 480P 上限）
GET /api/login/qrcode             申请 B 站扫码登录二维码
GET /api/login/poll?key=          轮询扫码；成功时把凭据写进访客自己的 Cookie
GET /api/logout                   清除访客自己的凭据 Cookie
GET /api/programs                 实时回放清单（页面加载时重取；?refresh=1 强制）
GET /api/status-board             小路状态：开播情况
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8765` | 监听端口，云平台会注入 |
| `BIND` | `0.0.0.0` | 绑定地址 |
| `ALLOWED_HOSTS` | 空 | 允许跨域的 Host，逗号分隔。**部署后请设为平台分配的域名** |
| `PUBLIC_BASE` | 空 | 覆盖 MPD 里的站点前缀。留空则用协议相对的 `//<Host>` |

> 前端全部使用相对路径（`/api/...`），所以线上是同源请求，不受 CORS 限制。
> `ALLOWED_HOSTS` 是纵深防御：万一有人把接口地址贴到别处，非白名单来源会被 403。

## 本地验证

```bash
cd cloud
PORT=8903 python server/app.py
curl -s http://127.0.0.1:8903/api/status
```

## 已验证项（部署前实测）

**接口**

| 项 | 结果 |
| --- | --- |
| `/` 与 `/data/segments.js` | 200 |
| `/api/status` | 200，云端恒为 `logged:false` |
| `/api/status-board` | 200，实时返回开播状态（房间号 1700301235） |
| `/api/programs` | 200，30 条，含 `meta` 与全部前端所需字段 |
| `/api/dashinfo` | 200，返回 DASH 阶梯 |
| `/api/dash` | 200，`application/dash+xml`，MPD 生成正常 |
| `/api/img` | 200，`image/webp`（两种尺寸均通过） |
| `/api/stream` | 200，转发 455 MB 完整分片 |
| `Range: bytes=0-1023` | **206 / 1024 bytes**（拖动进度可用） |
| 登录三接口 | 403 + 明确文案（不是 404，避免页面卡住） |

**安全**

| 项 | 结果 |
| --- | --- |
| 真实凭据是否进入发布包 | **无**（按凭据值全文扫描 `cloud/`，零命中） |
| 非 B 站媒体域名 | 403 拒绝 |
| 非白名单 `Origin` | 403 拒绝 |
| 白名单主域 / 子域 | 200 允许 |

**浏览器端到端**（真实 Chrome + CDP，无头）

| 项 | 结果 |
| --- | --- |
| 节目单渲染行数 | 20 行（一页 20 条） |
| 统计文本 | 「共 30 个节目 · 44 个片段 · 80.2 小时」 |
| 首行内容 | 标题 / 日期 / 分类 / 时长 / 弹幕 五列齐全 |
| 缩略图 | 20/23 成功（3 张为 WebP 慢加载，接口本身 200） |

## 踩过的坑（改动前请先读）

1. **`/api/programs` 必须返回完整字段，不能"精简"**。
   前端 `boot()` 读 `data.meta`，`programRow()` 读 `category / date / dm_total /
   thumb / score / parts[].dm_total`。最初只返回 `{ok,count,programs}` 且条目是
   B 站原始字段时，页面 **无任何 JS 报错**，但列表停在一行占位符
   （`body` 只有 453 字符）—— 因为 `state.all` 有值，只是每个字段都取不到。
   这类"静默不渲染"最难查，改这个接口时务必对齐字段表。
2. **静态根目录要基于 `cloud/` 而不是 `server/`**。
   `SITE` 用 `dirname(dirname(abspath(__file__)))/site`。
3. **不要把服务端凭据写进文件**。`sessdata.txt` 是账号级 Cookie，
   放进公网服务器等于公开账号。云端一律走「访客自带凭据」：
   凭据只存在于访客浏览器的 Cookie（`ksess`）里，服务端只透传、不落盘。
   > 注意：这条约束针对的是**凭据归属**，不是"不能有登录功能"。
   > 让每个访客用他自己的账号，既解锁 1080P，又不产生账号归属风险。
4. **凭据必须参与缓存分区**。`api_playurl` / `dash_data` 的缓存 key 里带了
   `sess_tag()`（凭据的 md5 前 10 位）。少了它，匿名与登录用户会互相污染 ——
   先到者决定后者能看到什么（匿名先请求就把 1080P 请求也压成 480P）。
   注意只放哈希，**绝不把凭据本身写进 key 或日志**。

## 只在云端复现的四个故障（2026-09-26 全部修复并线上验证）

本地全绿、发布后不能用的四类问题。**注意：这些都不是本机代码问题，
而是「数据中心 IP」和「HTTPS 反代」两个环境差异造成的。**

### 1. `x/web-interface/view` 被风控拦成 412（最根本）

**现象**：`/api/programs` 返回 `count: 0`，页面列表空。

**定位**：加 `/api/diag` 自检端点（见下），它给出决定性的对照表：

| 接口 | 云端 | 本地 |
| --- | --- | --- |
| `x/web-interface/view` | **412** | 200 |
| `x/web-interface/view/detail` | **412** | 200 |
| `x/player/pagelist` | **200** | 200 |
| `x/web-interface/wbi/view`（WBI 签名） | **200** | 200 |

系列接口 `x/series/archives` 在两处都是 200 —— 所以很容易误判「接口没坏」。

**修复**：`one_view()` 四路依次回退
`view` → `wbi/view` → `view/detail` → `pagelist`；
`pagelist` 兜底时缺的 `title/pubdate/aid/pic` 从 series 条目补齐。
**并且全部失败时抛 `RuntimeError`**，不再静默返回空列表
（静默是本项目排查成本最大的一处）。

### 2. `qn=undefined` 把接口打成 500

前端在还没拿到清晰度列表时会发 `qn=undefined`，`int("undefined")` 抛 ValueError。
**该 bug 同时存在于 `tools/serve.py`（本地版）**，是移植时带过来的。
修复：加 `parse_int(s, default)`，两处都改。

### 3. 清晰度下拉框渲染成「undefined」

`api_playurl` 原本把 `accept_quality`（`[80,64,...]` 纯数字）原样返回，
而前端 `setQualityList` 读 `q.qn` / `q.desc` → 5 个「undefined」。
修复：后端展开成 `[{qn, desc}]`（中文名取 `accept_description`），
**前端同时加兼容**（数字数组也能渲染）。

### 4. HTTPS 混合内容：MPD 的 `<BaseURL>` 写死 `http://`（最隐蔽）

**现象**：列表和清晰度都正常了，但视频 `error.code = 4`、`networkState = 3`，
`src` 是 `/api/stream?u=dW5kZWZ...`（base64 解出来是字符串 **`undef`**）。

**根因**：`base = "http://%s" % host`。线上是 HTTPS 反代，
`http://` 的 `<BaseURL>` 被浏览器按**混合内容**拦掉 → dash.js 拿不到分片
→ 前端退回 MP4 兜底 → 兜底路径的 `d.media` 恰好是 `undefined`。

**修复**：`public_base()` 改用**协议相对 URL**（`//host`），
并优先读 `X-Forwarded-Host`（反代后 `Host` 头未必是公网域名）。
可用 `PUBLIC_BASE` 环境变量整体覆盖。
附带收益：分片 `Content-Type` 从 `application/octet-stream` 变成正确的 `video/mp4`。

## `/api/diag` 自检端点

排查「云端和本地不一致」时**第一件事就是打它**，不要靠猜：

```bash
curl -s "$BASE/api/diag"
```

返回：环境变量（PYTHON/PORT）、系列接口原始字节与 `code`、
`build_count`（详情抓取成功数）、`build_error`、
以及 `endpoint_probe`（逐个候选接口的 HTTP 状态对照表）。

## 已知限制

1. **匿名上限 480P；访客登录后可达 1080P。** 凭据是访客自己的、服务端不保存，
   所以每个想看 1080P 的访客都要自己扫一次码（扫码后 Cookie 保存 30 天）。
2. 回放清单的抓取是**串行逐条**取详情的（42 条约 15~25 秒）。
   本地版用 8 线程并发；云端为了少占用免费的出口带宽没有并发。
3. 视频流经服务器中转，**带宽走平台额度**。多个访客同时观看可能触发限额。
4. 平台只暴露一个端口，本服务与静态文件共用。
5. **链接不可自定义、且每次发布都会变**（见上文「部署机制的两个重要事实」）。
   对外分享前请以最近一次发布的链接为准。

