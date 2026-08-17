FROM python:3.12-slim

WORKDIR /app

# 依赖：pymongo（MongoDB 驱动）、pyyaml（配置文件）、websocket-client（token 自动续期）
RUN pip install --no-cache-dir pymongo pyyaml websocket-client

# 默认配置模板（compose 部署时会被挂载的真实 config.yaml 覆盖）
COPY config.yaml.example /app/config.yaml
COPY caido_listener.py /app/caido_listener.py
COPY token_fetcher.py /app/token_fetcher.py

# 运行目录（挂载 volume 持久化 tokens.json / last_id.json）
VOLUME ["/app/data"]

# 前台运行主监听脚本（容器重启自动拉起）
CMD ["python3", "/app/caido_listener.py"]
