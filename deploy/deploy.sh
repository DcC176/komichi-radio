#!/usr/bin/env bash
# 二十四时小路电台 · 服务器端一键部署
#
# 在服务器上执行（目录需含 cloud/ 与 deploy/）：
#   sudo bash deploy/deploy.sh                # 裸机 + systemd（默认，最省资源）
#   sudo bash deploy/deploy.sh --docker       # 容器 + docker compose
#   bash deploy/deploy.sh --check             # 只跑自检，不改动系统
#
# 选项：
#   --port N      监听端口（默认 8080）
#   --host 域名   写入 CORS 白名单（多个用逗号分隔）
#   --check       只自检
#   --docker      使用容器模式
#
# 为什么默认选裸机而不是容器：本服务是纯 Python 标准库、无第三方依赖、
# 单进程、无状态（缓存在内存），容器带来的隔离收益很小，却要多装一整套
# Docker 运行时。裸机 + systemd 是这条链路上最少的活动部件。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=8080
HOSTS=""
MODE="systemd"
ONLY_CHECK=0
SERVICE=komichi-radio

while [ $# -gt 0 ]; do
    case "$1" in
        --port)   PORT="${2:?--port 需要一个端口号}"; shift 2 ;;
        --host)   HOSTS="${2:?--host 需要一个域名}"; shift 2 ;;
        --check)  ONLY_CHECK=1; shift ;;
        --docker) MODE="docker"; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\n[中止] %s\n' "$*" >&2; exit 1; }

if [ "$ONLY_CHECK" = "1" ]; then
    exec bash "$PROJECT_DIR/deploy/selfcheck.sh" "http://127.0.0.1:$PORT"
fi

[ -f "$PROJECT_DIR/cloud/server/app.py" ] || die "找不到 $PROJECT_DIR/cloud/server/app.py —— 请把 cloud/ 与 deploy/ 一起上传。"
[ -d "$PROJECT_DIR/cloud/site" ]          || die "找不到 $PROJECT_DIR/cloud/site —— 静态站点未上传。"

# 端口占用检查：先看是不是本服务已经在跑
if command -v curl >/dev/null 2>&1 \
   && curl -sS --max-time 3 "http://127.0.0.1:$PORT/api/status" 2>/dev/null | grep -q '"cloud"'; then
    say "端口 $PORT 上已有本服务在运行，直接重启它。"
fi

wait_ready() {
    command -v curl >/dev/null 2>&1 || return 0
    for _ in $(seq 1 30); do
        if curl -sS --max-time 3 "http://127.0.0.1:$PORT/api/status" 2>/dev/null | grep -q '"cloud"'; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ------------------------------------------------------------------ 容器模式
if [ "$MODE" = "docker" ]; then
    say "容器模式部署（端口 $PORT）"
    command -v docker >/dev/null 2>&1 || die "未安装 docker。可改用默认的裸机模式：sudo bash deploy/deploy.sh"
    docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用，请升级 docker，或改用默认的裸机模式。"

    [ "$PORT" = "8080" ] || die "--docker 模式下端口请在 deploy/docker-compose.yml 里改（避免两处配置不一致）。"

    cd "$PROJECT_DIR"
    docker compose -f deploy/docker-compose.yml up -d --build

    wait_ready || die "容器已启动，但 $PORT 端口未就绪。排查：docker compose -f deploy/docker-compose.yml logs --tail 50"
    say "部署完成"
    exec bash "$PROJECT_DIR/deploy/selfcheck.sh" "http://127.0.0.1:$PORT"
fi

# ------------------------------------------------------------------ 裸机模式
say "裸机模式部署（端口 $PORT）"

[ "$(id -u)" = "0" ] || die "裸机模式需要 root 权限来安装 systemd 服务，请用 sudo 执行。"

PYBIN="$(command -v python3 || true)"
[ -n "$PYBIN" ] || die "未找到 python3。请先安装：sudo apt install -y python3"
"$PYBIN" - <<'PY' || die "python3 版本过低，需要 3.6 以上。"
import sys
sys.exit(0 if sys.version_info >= (3, 6) else 1)
PY
echo "使用解释器：$PYBIN（$("$PYBIN" -V 2>&1)）"

# 专用系统账号：服务只需要读文件 + 出网，不应以 root 运行
if ! id -u komichi >/dev/null 2>&1; then
    useradd --system --home-dir "$PROJECT_DIR" --shell /usr/sbin/nologin komichi
    echo "已创建系统账号 komichi"
fi
chmod o+rX "$PROJECT_DIR" "$PROJECT_DIR/cloud" 2>/dev/null || true

UNIT=/etc/systemd/system/$SERVICE.service
say "写入 $UNIT"
sed -e "s|^User=.*|User=komichi|" \
    -e "s|^Group=.*|Group=komichi|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
    -e "s|^ExecStart=.*|ExecStart=$PYBIN $PROJECT_DIR/cloud/server/app.py|" \
    -e "s|^Environment=PORT=.*|Environment=PORT=$PORT|" \
    -e "s|^Environment=ALLOWED_HOSTS=.*|Environment=ALLOWED_HOSTS=$HOSTS|" \
    "$PROJECT_DIR/deploy/komichi-radio.service" > "$UNIT"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

if ! wait_ready; then
    systemctl status "$SERVICE" --no-pager -l | head -30 || true
    die "服务已启动但 $PORT 端口未就绪，日志见上。"
fi

say "部署完成"
exec bash "$PROJECT_DIR/deploy/selfcheck.sh" "http://127.0.0.1:$PORT"
