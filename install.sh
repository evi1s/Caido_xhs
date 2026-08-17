#!/usr/bin/env bash
# ============================================================
# Caido_xhs 一键安装脚本
#   1. 检查 Docker / Docker Compose 环境
#   2. 首次运行自动生成 config.yaml 模板
#   3. 构建并启动 Caido_xhs + caido_listener 两个容器
# 用法: ./install.sh
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "======================================"
echo " Caido_xhs 一键安装"
echo "======================================"

# ---------- 1. 检查环境 ----------
if ! command -v docker >/dev/null 2>&1; then
  echo "[错误] 未检测到 Docker，请先安装: https://docs.docker.com/get-docker/"
  exit 1
fi

COMPOSE="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    echo "[错误] 未检测到 Docker Compose"
    exit 1
  fi
fi
echo "[OK] 环境就绪: docker + $COMPOSE"

# ---------- 2. 生成配置 ----------
if [ ! -f config.yaml ]; then
  cp config.yaml.example config.yaml
  echo ""
  echo "[提示] 已生成 config.yaml 模板（请先编辑填写）:"
  echo "  1. auth.pat   - Caido Personal Access Token"
  echo "                  获取: 启动后浏览器打开 http://<服务器IP>:18000"
  echo "                        登录认领 -> Profile -> Personal Access Tokens -> Create"
  echo "  2. mongo.*    - MongoDB 连接信息（host/port/账号/库/集合）"
  echo "  3. targets    - 抓取目标（host + path 前缀）"
  echo "  4. extract_fields - 提取字段"
  echo ""
  echo "填写完成后重新运行: ./install.sh"
  exit 0
fi
echo "[OK] config.yaml 已存在"

# ---------- 3. 构建并启动 ----------
echo "[1/3] 构建并启动容器（首次拉取 Caido 镜像可能需要几分钟）..."
$COMPOSE up -d --build

echo "[2/3] 容器状态:"
sleep 5
$COMPOSE ps

echo "[3/3] 完成!"
echo ""
echo "======================================"
echo " 后续步骤（仅首次需要）:"
echo ""
echo " 1. 浏览器打开 http://<服务器IP>:18000"
echo "    -> 用 Caido 账号登录并认领实例（新实例必须手动认领一次）"
echo ""
echo " 2. 创建 PAT:"
echo "    Profile -> Personal Access Tokens -> Create"
echo "    把生成的 PAT 填入 config.yaml 的 auth.pat"
echo ""
echo " 3. 使配置生效:"
echo "    $COMPOSE restart listener"
echo ""
echo " 4. 客户端代理指向 <服务器IP>:18000"
echo "    并安装信任 Caido CA 证书（HTTPS 解密必需）"
echo "======================================"
