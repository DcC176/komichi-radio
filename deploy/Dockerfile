# 二十四时小路电台 · 云端服务容器镜像
#
# 适配平台：腾讯云（轻量应用服务器 / CVM / 云托管 CloudBase）、
#           阿里云（轻量应用服务器 SWAS / ECS / SAE）
#           以及任何能跑 Docker 的 Linux 主机。
#
# 构建（在项目根 小路回放台/ 下执行）：
#     docker build -f deploy/Dockerfile -t komichi-radio:latest .
#
# 为什么用 3.11：WorkBuddy 托管环境实测为 Python 3.11，锁同一版本可避免
# 语言特性/标准库行为漂移导致的「本地能跑、线上不能跑」。
FROM python:3.11-slim

# 无第三方依赖：服务只用标准库（http.server / urllib / json / hashlib），
# 因此这里刻意不写 pip install —— 没有依赖就没有供应链风险面。
WORKDIR /app

# 目录结构必须与 app.py 的路径推导一致，否则静态站点会 404：
#   ROOT = dirname(dirname(abspath(__file__)))  ->  /app
#   SITE = ROOT/site                            ->  /app/site
#   overrides.json = ROOT/server/overrides.json ->  /app/server/overrides.json（可选）
COPY cloud/server/ /app/server/
COPY cloud/site/   /app/site/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    BIND=0.0.0.0

EXPOSE 8080

# /api/status 是纯本地响应（不请求 B 站），适合做健康检查 ——
# 用它而不是 /api/programs，避免探针周期性触发 B 站接口风控。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
u='http://127.0.0.1:%s/api/status' % os.environ.get('PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(u,timeout=4).status==200 else 1)"

CMD ["python", "/app/server/app.py"]
