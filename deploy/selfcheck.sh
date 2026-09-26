#!/usr/bin/env bash
# 二十四时小路电台 · 部署后自检
#
# 用法：
#   bash deploy/selfcheck.sh                      # 默认 http://127.0.0.1:8080
#   bash deploy/selfcheck.sh http://1.2.3.4:8080  # 指定地址
#
# 设计意图：把「部署完了到底通不通」变成一条命令。
# 其中 /api/diag 是最关键的一项 —— 它直接暴露 B 站各候选接口的真实返回码，
# 能一眼看出这台机器的 IP 是否被 B 站风控（数据中心 IP 的常见问题）。

set -uo pipefail

BASE="${1:-http://127.0.0.1:8080}"
BASE="${BASE%/}"

if ! command -v curl >/dev/null 2>&1; then
    echo "缺少 curl，请先安装：sudo apt install -y curl" >&2
    exit 2
fi

PASS=0
FAIL=0
BODY="$(mktemp)"
trap 'rm -f "$BODY" 2>/dev/null || true' EXIT

# check <名称> <路径> <期望HTTP码> [正文必须包含的字符串]
check() {
    local name="$1" path="$2" want="$3" needle="${4:-}"
    local code
    code="$(curl -sS -o "$BODY" -w '%{http_code}' --max-time 60 "$BASE$path" 2>/dev/null)" || code="000"

    if [ "$code" != "$want" ]; then
        printf '  [失败] %-22s HTTP %s（期望 %s）\n' "$name" "$code" "$want"
        FAIL=$((FAIL + 1))
        return 1
    fi
    if [ -n "$needle" ] && ! grep -q -- "$needle" "$BODY"; then
        printf '  [失败] %-22s HTTP %s 但正文缺少 "%s"\n' "$name" "$code" "$needle"
        FAIL=$((FAIL + 1))
        return 1
    fi
    printf '  [通过] %-22s HTTP %s  %s 字节\n' "$name" "$code" "$(wc -c <"$BODY" | tr -d ' ')"
    PASS=$((PASS + 1))
    return 0
}

echo "自检目标：$BASE"
echo

echo "-- 1. 本地链路（不依赖外网） -------------------------"
check "首页"            "/"                 200 "<title"
check "前端脚本"        "/assets/app.js"    200
check "节目单数据"      "/data/programs.js" 200
check "服务状态"        "/api/status"       200 '"cloud"'

echo
echo "-- 2. 依赖 B 站的外网链路（关键） --------------------"
if check "实时节目单"    "/api/programs"     200 '"count"'; then
    # 取第一个 "count"：响应里 meta 与顶层各有一个同名字段，不 head 会取到多行
    cnt="$(grep -o '"count"[[:space:]]*:[[:space:]]*[0-9]*' "$BODY" | head -1 | grep -o '[0-9]*$')"
    echo "         解析到节目数：${cnt:-?}"
    if [ "${cnt:-0}" = "0" ]; then
        echo "         [!] count 为 0：通常是本机 IP 被 B 站风控，见下面第 3 节"
    fi
fi
check "开播状态"        "/api/status-board" 200

echo
echo "-- 3. B 站接口逐跳对照（判断 IP 是否被风控） ---------"
code="$(curl -sS -o "$BODY" -w '%{http_code}' --max-time 90 "$BASE/api/diag" 2>/dev/null)" || code="000"
if [ "$code" = "200" ]; then
    PASS=$((PASS + 1))
    python3 - "$BODY" <<'PY' 2>/dev/null || cat "$BODY"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
# endpoint_probe 是列表，每项形如 {"name","http","code","bytes"}
probe = d.get("endpoint_probe") or []
if not probe:
    print("  （diag 未返回 endpoint_probe，直接看原始输出）")
for item in probe:
    if not isinstance(item, dict):
        print("  %s" % item)
        continue
    st = item.get("http")
    flag = "OK  " if str(st) == "200" else ("拦截" if str(st) in ("412", "403", "429") else "异常")
    print("  %s %-14s HTTP %-4s code=%s" % (flag, item.get("name", "?"), st, item.get("code")))
print()
print("  详情抓取成功数 build_count = %s" % d.get("build_count"))
if d.get("build_error"):
    print("  build_error = %s" % str(d["build_error"])[:200])
print("  说明：412/403 表示该接口被风控。服务内部有四路回退")
print("  （view -> wbi/view -> view/detail -> pagelist），")
print("  只要「实时节目单」那一项通过，播放链路就成立。")
PY
else
    printf '  [失败] %-22s HTTP %s\n' "/api/diag" "$code"
    FAIL=$((FAIL + 1))
fi

echo
echo "-----------------------------------------------------"
echo "通过 $PASS 项，失败 $FAIL 项"
if [ "$FAIL" -gt 0 ]; then
    echo "结论：未通过。请先解决上面标 [失败] 的项。"
    exit 1
fi
echo "结论：部署成功，服务可用。"
