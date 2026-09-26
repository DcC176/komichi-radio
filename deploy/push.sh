#!/usr/bin/env bash
# 二十四时小路电台 · 本地推送部署
#
# 在本地（项目根 小路回放台/）执行，把 cloud/ 与 deploy/ 推到服务器并部署。
#
# 用法：
#   bash deploy/push.sh --target root@1.2.3.4
#   bash deploy/push.sh --target ubuntu@1.2.3.4 --key ~/.ssh/tc.pem --port 8080
#   bash deploy/push.sh --target root@1.2.3.4 --docker
#
# 选项：
#   --target user@host   服务器地址（必填）
#   --dir 路径           服务器上的部署目录（默认 /opt/komichi-radio）
#   --key 私钥文件       指定 SSH 私钥
#   --port N             监听端口（默认 8080）
#   --host 域名          写入 CORS 白名单
#   --docker             使用容器模式部署
#
# 传输走 tar 管道（不落中间压缩包），避免在本地留下需要清理的临时文件。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET=""
REMOTE_DIR="/opt/komichi-radio"
KEY=""
PORT="8080"
HOSTS=""
MODE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="${2:?}"; shift 2 ;;
        --dir)    REMOTE_DIR="${2:?}"; shift 2 ;;
        --key)    KEY="${2:?}"; shift 2 ;;
        --port)   PORT="${2:?}"; shift 2 ;;
        --host)   HOSTS="${2:?}"; shift 2 ;;
        --docker) MODE="--docker"; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
    esac
done

[ -n "$TARGET" ] || { echo "缺少 --target，例如：--target root@1.2.3.4" >&2; exit 2; }
command -v ssh >/dev/null 2>&1 || { echo "本机没有 ssh/scp，请先安装 OpenSSH 客户端。" >&2; exit 2; }
command -v tar >/dev/null 2>&1 || { echo "本机没有 tar。" >&2; exit 2; }

SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")

REMOTE_ARGS="--port $PORT"
[ -n "$HOSTS" ] && REMOTE_ARGS="$REMOTE_ARGS --host $HOSTS"
[ -n "$MODE" ]  && REMOTE_ARGS="$REMOTE_ARGS $MODE"

echo "目标：$TARGET:$REMOTE_DIR"

echo "1/3 准备远端目录"
ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p '$REMOTE_DIR'"

echo "2/3 上传 cloud/ 与 deploy/"
# 排除 __pycache__：本机跑过的字节码在服务器上无意义，且可能因版本不同而失效
tar czf - \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    -C "$PROJECT_DIR" \
    cloud deploy .dockerignore \
  | ssh "${SSH_OPTS[@]}" "$TARGET" "tar xzf - -C '$REMOTE_DIR'"

echo "3/3 在服务器上执行部署脚本"
# 分配 tty：非 root 登录时 sudo 需要交互输入密码
ssh -t "${SSH_OPTS[@]}" "$TARGET" \
    "cd '$REMOTE_DIR' && if [ \"\$(id -u)\" = 0 ]; then bash deploy/deploy.sh $REMOTE_ARGS; else sudo bash deploy/deploy.sh $REMOTE_ARGS; fi"
