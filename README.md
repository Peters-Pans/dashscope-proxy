# DashScope Proxy

阿里云 DashScope API 代理服务，支持多子 Key 管理、配额控制、用量统计。

## 功能特性

- **多子 Key 管理**：自动生成 4 个子 Key，支持动态管理
- **固定周期配额**：5H（整点对齐）/ 自然周 / 自然月
- **模型白名单**：只有 Coding Plan 包含的模型才计入配额
- **管理后台**：可视化查看用量、管理 Key
- **用户面板**：用户自助查看自己的配额使用情况

## 支持的模型

```
qwen3.5-plus
qwen3-max-2026-01-23
qwen3-coder-next
qwen3-coder-plus
MiniMax-M2.5
glm-5
glm-4.7
kimi-k2.5
```

## 部署步骤

### 1. 把项目传到服务器

```bash
scp -r bailian/ user@your-server:~/
```

### 2. 一键部署

```bash
cd ~/bailian
bash setup.sh
```

脚本会自动：

- 检查 Docker 是否安装
- 引导你输入阿里云 API Key
- 自动生成随机 Admin Token、Redis 密码、4 个子 Key（只显示一次，记得保存）
- 构建镜像、启动服务、健康检查

### 3. 绑定域名 + HTTPS（可选）

```bash
# 安装 Nginx
apt install nginx certbot python3-certbot-nginx -y

# 复制配置，改成你的域名
cp nginx.conf /etc/nginx/sites-available/dashproxy
vim /etc/nginx/sites-available/dashproxy  # 改 server_name
ln -s /etc/nginx/sites-available/dashproxy /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 申请 SSL 证书
certbot --nginx -d your-domain.com
```

## 常用命令

```bash
docker compose logs -f proxy    # 查看日志
docker compose restart proxy    # 重启
docker compose down             # 停止
docker compose pull && docker compose up -d  # 更新
```

## API 使用

### 代理请求

```bash
curl https://your-domain.com/v1/chat/completions \
  -H "Authorization: Bearer sk-sub-xxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5-plus", "messages": [{"role": "user", "content": "Hello"}]}'
```

### 查看用量

```bash
curl https://your-domain.com/_usage \
  -H "Authorization: Bearer sk-sub-xxxxx"
```

## 项目结构

```
.
├── main.py              # FastAPI 主程序
├── static/
│   ├── admin.html       # 管理后台
│   └── user.html        # 用户面板
├── Dockerfile
├── docker-compose.yml
├── nginx.conf           # Nginx 反向代理配置
├── setup.sh             # 一键部署脚本
└── requirements.txt
```

## License

MIT