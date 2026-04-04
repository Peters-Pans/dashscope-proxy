#!/bin/bash
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERR]${NC}   $1"; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}▶ $1${NC}"; }

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   DashScope Proxy — 部署向导             ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 确保在项目目录 ──────────────────────────────
[ -f "docker-compose.yml" ] || error "请在项目根目录（含 docker-compose.yml）下运行此脚本"

# ── 检查系统依赖 ────────────────────────────────
step "检查系统依赖"

command -v docker &>/dev/null || error "未安装 Docker，请先安装：https://docs.docker.com/engine/install/"
success "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1) 已就绪"

if docker compose version &>/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
  success "Docker Compose $(docker compose version --short 2>/dev/null || echo 'v2') 已就绪"
else
  error "未找到 Docker Compose V2，请升级 Docker Desktop 或执行：
  apt install docker-compose-plugin  # Ubuntu/Debian
  yum install docker-compose-plugin  # CentOS/RHEL"
fi

command -v curl &>/dev/null || error "未安装 curl，请执行：apt install curl"

# ── 检查端口占用 ────────────────────────────────
if ss -tlnp 2>/dev/null | grep -q ':8000 ' || netstat -tlnp 2>/dev/null | grep -q ':8000 '; then
  warn "端口 8000 已被占用，可能与已运行的实例冲突"
  read -p "  继续部署？[y/N] " _ans
  [[ "$_ans" =~ ^[Yy]$ ]] || error "已取消"
fi

# ── 选择操作模式 ────────────────────────────────
echo ""
if [ -f ".env" ]; then
  echo -e "  检测到已有 ${YELLOW}.env${NC}，请选择操作："
  echo -e "  ${BOLD}1)${NC} 更新代码并重新构建镜像（保留现有 Key 和配置）"
  echo -e "  ${BOLD}2)${NC} 重新配置（删除 .env 重新生成 Key，谨慎使用）"
  echo -e "  ${BOLD}3)${NC} 仅重启服务（不重新构建）"
  echo -e "  ${BOLD}4)${NC} 查看当前 Key 信息"
  echo -e "  ${BOLD}q)${NC} 退出"
  echo ""
  read -p "  请选择 [1/2/3/4/q]: " MODE
  echo ""
  case "$MODE" in
    1) MODE="update" ;;
    2) rm -f .env; MODE="install" ;;
    3) MODE="restart" ;;
    4) MODE="showkeys" ;;
    q|Q) echo "已退出"; exit 0 ;;
    *) error "无效选项" ;;
  esac
else
  MODE="install"
fi

# ── 查看当前 Key ────────────────────────────────
if [ "$MODE" = "showkeys" ]; then
  step "当前配置信息"
  source .env
  echo -e "  ${YELLOW}API 地址:${NC}     http://localhost:8000"
  echo -e "  ${YELLOW}Admin Token:${NC}  ${GREEN}${ADMIN_TOKEN}${NC}"
  echo -e "  ${YELLOW}Key 1:${NC}        ${GREEN}${KEY_1}${NC}"
  echo -e "  ${YELLOW}Key 2:${NC}        ${GREEN}${KEY_2}${NC}"
  echo -e "  ${YELLOW}Key 3:${NC}        ${GREEN}${KEY_3}${NC}"
  echo -e "  ${YELLOW}Key 4:${NC}        ${GREEN}${KEY_4}${NC}"
  echo ""
  exit 0
fi

# ── 仅重启 ─────────────────────────────────────
if [ "$MODE" = "restart" ]; then
  step "重启服务"
  $COMPOSE_CMD restart proxy
  success "服务已重启"
  exit 0
fi

# ── 全新安装：生成 .env ─────────────────────────
if [ "$MODE" = "install" ]; then
  step "配置 API Key"
  echo -e "  请输入阿里云 Coding Plan API Key"
  echo -e "  ${CYAN}格式为 sk-sp- 开头，在百炼控制台 → API-KEY 页面获取${NC}"
  echo ""

  while true; do
    read -p "  API Key: " ALIYUN_KEY
    [ -z "$ALIYUN_KEY" ] && echo -e "  ${RED}不能为空${NC}" && continue
    if [[ ! "$ALIYUN_KEY" =~ ^sk-sp- ]]; then
      warn "Key 格式不符（期望 sk-sp- 开头），Coding Plan key 以 sk-sp- 开始"
      read -p "  确认使用此 Key 继续？[y/N] " _c
      [[ "$_c" =~ ^[Yy]$ ]] && break || continue
    else
      break
    fi
  done

  # 月度配额重置日
  echo ""
  echo -e "  ${BOLD}月度配额重置日${NC}"
  echo -e "  ${CYAN}每月几号 00:00 重置月度配额（直接回车默认第1日）${NC}"
  echo ""
  while true; do
    read -p "  重置日 [1-28，默认 1]: " _rd_input
    if [ -z "$_rd_input" ]; then
      MONTHLY_RESET_DAY=1; break
    elif [[ "$_rd_input" =~ ^[0-9]+$ ]] && [ "$_rd_input" -ge 1 ] && [ "$_rd_input" -le 28 ]; then
      MONTHLY_RESET_DAY=$_rd_input; break
    else
      warn "请输入 1-28 之间的数字"
    fi
  done
  success "月度重置日设为每月第 ${MONTHLY_RESET_DAY} 日"
  echo ""

  # 生成随机凭据
  ADMIN_TOKEN=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)
  REDIS_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
  KEY_1="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 24)"
  KEY_2="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 24)"
  KEY_3="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 24)"
  KEY_4="sk-sub-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 24)"

  cat > .env <<ENVEOF
# ── 阿里云 ──────────────────────────────────────────
ALIYUN_API_KEY=${ALIYUN_KEY}
ALIYUN_BASE_URL=https://coding.dashscope.aliyuncs.com/v1

# ── Redis ────────────────────────────────────────────
REDIS_URL=redis://:${REDIS_PASS}@redis:6379
REDIS_PASSWORD=${REDIS_PASS}

# ── 管理员 Token ──────────────────────────────────────
ADMIN_TOKEN=${ADMIN_TOKEN}

# ── 月度配额重置日（1-28，可在管理后台在线修改）──────────
MONTHLY_RESET_DAY=${MONTHLY_RESET_DAY}

# ── 子 Key（分发给用户）────────────────────────────────
KEY_1=${KEY_1}
KEY_2=${KEY_2}
KEY_3=${KEY_3}
KEY_4=${KEY_4}
ENVEOF

  success ".env 已生成"
  echo ""
  echo -e "  ${RED}${BOLD}⚠ 以下信息只显示一次，请立即保存！${NC}"
  echo ""
  echo -e "  ${YELLOW}Admin Token:${NC}"
  echo -e "  ${GREEN}${BOLD}${ADMIN_TOKEN}${NC}"
  echo ""
  echo -e "  ${YELLOW}4 个子 Key（按需分发给用户）:${NC}"
  echo -e "  用户1: ${GREEN}${KEY_1}${NC}"
  echo -e "  用户2: ${GREEN}${KEY_2}${NC}"
  echo -e "  用户3: ${GREEN}${KEY_3}${NC}"
  echo -e "  用户4: ${GREEN}${KEY_4}${NC}"
  echo ""
  read -p "  已保存以上信息，按 Enter 继续部署..."
fi

# ── 构建镜像 ────────────────────────────────────
step "构建 Docker 镜像"
info "首次构建约需 3-5 分钟（需下载依赖），请耐心等待..."
echo ""

if ! $COMPOSE_CMD build --progress=plain 2>&1; then
  echo ""
  error "镜像构建失败，请检查上方日志。常见原因：
  - 网络超时：脚本已配置阿里云 pip 镜像，重试一次通常可解决
  - Docker 磁盘空间不足：docker system prune -f 清理后重试"
fi

success "镜像构建完成"

# ── 启动服务 ────────────────────────────────────
step "启动服务"
$COMPOSE_CMD up -d
echo ""

# ── 健康检查 ─────────────────────────────────────
step "健康检查"
info "等待服务就绪..."
MAX=30; WAITED=0
while true; do
  if curl -sf http://localhost:8000/_panel/usage > /dev/null 2>&1; then
    success "服务已就绪（等待 ${WAITED}s）"
    break
  fi
  WAITED=$((WAITED+2))
  if [ $WAITED -ge $MAX ]; then
    echo ""
    error "服务在 ${MAX}s 内未就绪，请检查日志：
  $COMPOSE_CMD logs --tail=50 proxy"
  fi
  printf "  等待中... (%ds)\r" "$WAITED"
  sleep 2
done

# ── 部署完成 ─────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   部署成功 ✓                             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""

# 尝试获取公网 IP
PUBLIC_IP=$(curl -sf --max-time 3 https://api.ipify.org 2>/dev/null || echo "your-server-ip")

echo -e "  ${BOLD}服务地址（本机）${NC}"
echo -e "  管理后台: ${BLUE}http://localhost:8000/_panel/admin${NC}"
echo -e "  用量面板: ${BLUE}http://localhost:8000/_panel/usage${NC}"
echo -e "  API 地址: ${BLUE}http://localhost:8000${NC}"
echo ""
echo -e "  ${BOLD}常用命令${NC}"
echo -e "  查看日志: $COMPOSE_CMD logs -f proxy"
echo -e "  重启服务: $COMPOSE_CMD restart proxy"
echo -e "  停止服务: $COMPOSE_CMD down"
echo ""
echo -e "  ${BOLD}${YELLOW}下一步：绑定域名 + HTTPS（推荐）${NC}"
echo -e "  ${CYAN}# 安装 Nginx + Certbot${NC}"
echo -e "  apt install nginx certbot python3-certbot-nginx -y"
echo ""
echo -e "  ${CYAN}# 复制并编辑 Nginx 配置（将 your-domain.com 改为你的域名）${NC}"
echo -e "  cp nginx.conf /etc/nginx/sites-available/dashproxy"
echo -e "  sed -i 's/your-domain.com/你的域名/g' /etc/nginx/sites-available/dashproxy"
echo -e "  ln -s /etc/nginx/sites-available/dashproxy /etc/nginx/sites-enabled/"
echo -e "  nginx -t && systemctl reload nginx"
echo ""
echo -e "  ${CYAN}# 申请 SSL 证书${NC}"
echo -e "  certbot --nginx -d 你的域名"
echo ""
echo -e "  ${CYAN}# 证书申请成功后，对外服务地址为：${NC}"
echo -e "  ${GREEN}https://你的域名${NC}  （API / 管理后台 / 用量面板）"
echo ""
echo -e "  ${YELLOW}如服务器有防火墙，请确保开放 80 和 443 端口：${NC}"
echo -e "  ufw allow 80 && ufw allow 443"
echo ""
