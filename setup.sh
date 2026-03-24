#!/bin/bash
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERR]${NC}  $1"; exit 1; }

echo ""
echo -e "${BLUE}╔══════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   DashScope Proxy — 部署向导         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════╝${NC}"
echo ""

# ── 检查依赖 ────────────────────────────────
info "检查系统依赖..."
command -v docker  &>/dev/null || error "未安装 Docker，请先安装：https://docs.docker.com/engine/install/"
command -v docker compose &>/dev/null || \
  docker compose version &>/dev/null 2>&1 || \
  error "未安装 Docker Compose V2，请升级 Docker Desktop 或安装插件"
success "Docker 已就绪"

# ── 生成 .env ────────────────────────────────
if [ -f ".env" ]; then
  warn ".env 文件已存在，跳过生成（如需重新配置请删除后再运行）"
else
  info "开始配置 .env ..."
  echo ""

  read -p "  阿里云 API Key (sk-xxx): " ALIYUN_KEY
  [ -z "$ALIYUN_KEY" ] && error "API Key 不能为空"

  # 生成随机 Admin Token
  ADMIN_TOKEN=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
  echo ""
  echo -e "  ${YELLOW}Admin Token（请保存好，只显示一次）:${NC}"
  echo -e "  ${GREEN}${ADMIN_TOKEN}${NC}"
  echo ""

  # 生成 4 个随机子Key
  KEY_1="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 20)"
  KEY_2="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 20)"
  KEY_3="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 20)"
  KEY_4="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 20)"

  # Redis 密码
  REDIS_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)

  cat > .env << ENVEOF
# ── 阿里云 ──────────────────────────────────
ALIYUN_API_KEY=${ALIYUN_KEY}
ALIYUN_BASE_URL=https://coding.dashscope.aliyuncs.com/v1

# ── Redis ────────────────────────────────────
REDIS_URL=redis://:${REDIS_PASS}@redis:6379

# ── 管理员 Token ─────────────────────────────
ADMIN_TOKEN=${ADMIN_TOKEN}

# ── 4个子Key ─────────────────────────────────
KEY_1=${KEY_1}
KEY_2=${KEY_2}
KEY_3=${KEY_3}
KEY_4=${KEY_4}
ENVEOF

  success ".env 已生成"
  echo ""
  echo -e "  ${YELLOW}4个子Key（分发给用户）:${NC}"
  echo -e "  用户1: ${GREEN}${KEY_1}${NC}"
  echo -e "  用户2: ${GREEN}${KEY_2}${NC}"
  echo -e "  用户3: ${GREEN}${KEY_3}${NC}"
  echo -e "  用户4: ${GREEN}${KEY_4}${NC}"
  echo ""
  warn "以上信息只显示一次，请立即保存！"
  echo ""
  read -p "  已保存，按 Enter 继续..."
fi

# ── 更新 docker-compose.yml 加 Redis 密码 ────
# 直接用生成时的变量，不需要再从 .env 解析
REDIS_PASS_VAL=$REDIS_PASS

cat > docker-compose.yml << DCEOF
services:

  proxy:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    # 使用 sh -c 从环境变量读取密码，避免密码出现在 docker logs / ps 中
    command: sh -c 'redis-server --requirepass "$$REDIS_PASSWORD" --save 60 1 --loglevel warning'
    environment:
      REDIS_PASSWORD: "${REDIS_PASS_VAL}"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "sh", "-c", "redis-cli -a \"$$REDIS_PASSWORD\" ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

volumes:
  redis_data:
DCEOF

# ── 构建并启动 ───────────────────────────────
echo ""
info "构建镜像（首次可能需要几分钟）..."
docker compose build 2>&1 | tail -5
success "镜像构建完成"

info "启动服务..."
docker compose up -d
sleep 3

# ── 健康检查 ─────────────────────────────────
info "健康检查..."
for i in {1..10}; do
  if curl -sf http://localhost:8000/_panel/usage > /dev/null 2>&1; then
    success "服务已启动"
    break
  fi
  [ $i -eq 10 ] && error "服务启动失败，请运行 docker compose logs 查看日志"
  sleep 2
done

# ── 完成 ─────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   部署完成 🎉                        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""
echo -e "  管理后台:  ${BLUE}http://localhost:8000/_panel/admin${NC}"
echo -e "  用量面板:  ${BLUE}http://localhost:8000/_panel/usage${NC}"
echo -e "  API 地址:  ${BLUE}http://localhost:8000${NC}"
echo ""
echo -e "  ${YELLOW}常用命令:${NC}"
echo -e "  查看日志:  docker compose logs -f proxy"
echo -e "  重启服务:  docker compose restart proxy"
echo -e "  停止服务:  docker compose down"
echo ""

