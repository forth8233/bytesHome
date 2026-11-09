# Home Agent Docker 部署教程

本文档介绍如何把 Home Agent（FastAPI 后端 + Vue 前端）部署到一台 Linux 服务器上，以 Docker 编排为主、原生进程部署为辅。读完本文，你应该能在一条 `docker compose up -d` 之后，通过浏览器访问到完整的智能家居语音管家界面。

## 部署形态概览

Home Agent 由两部分组成：

| 组件 | 技术栈 | 作用 |
| --- | --- | --- |
| 后端 | FastAPI + SQLite | 业务逻辑、语音解析快路径、大模型慢路径、Home Assistant 对接、提案审批、消息渠道 |
| 前端 | Vue 3 + Vite 静态资源 | 管理界面：聊天、场景、提案、配置、记忆 |

后端本身不托管前端静态文件，二者需要一起部署。Docker 方式会把前端构建产物放进 Nginx，由 Nginx 同时托管前端页面并反向代理 `/api` 到后端。

整套系统依赖的外部服务：

| 依赖 | 是否必需 | 说明 |
| --- | --- | --- |
| Home Assistant | 是 | 通过 WebSocket 长连接读取实体、下发控制指令 |
| 大模型 API / Ollama | 是 | 慢路径与主动推理使用，可在 WebUI 配置 provider |
| 飞书开放平台 | 可选 | 作为语音/消息渠道接入 |

## 本机部署与服务器部署的区别

- 本机（Windows/Mac）常见做法：后端起 `uvicorn`、前端起 `vite dev`，用于开发与内测。
- 服务器（Linux）常见做法：Docker 编排,后端容器 + Nginx 容器一次性起好，用于长期稳定运行。

两种方式的后端启动参数都必须是**单进程**。会话、SSE 推送、快路径缓存、慢路径限流计数都保存在进程内，多 worker 会导致状态分裂和重复执行，因此不要用 `--workers` 大于 1 的配置。

## 方法一：Docker 编排部署（推荐）

### 准备条件

- 一台 Linux 服务器（Debian/Ubuntu 均适用）
- 已安装 Docker 与 Docker Compose 插件（`docker compose` 子命令）
- 可访问的 Home Assistant 实例，以及它的长期访问令牌
- 可选：一个已做号的大模型 API Key，或一台可访问的 Ollama

若尚未安装 Docker，以下命令可在 Debian/Ubuntu 上快速装好：

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
docker compose version
```

### 目录结构

把项目放在一个目录里。以下文件是 Docker 部署会用到的：

```text
byteAgent/
  docker-compose.yml      # 编排文件
  .env                    # 秘密配置，不提交到仓库
  backend/
    Dockerfile            # 后端镜像构建
  frontend/
    dist/                 # 前端构建产物（由 docker 构建时生成或纳入镜像）
  data/                   # 运行时数据库落盘目录
  logs/                   # 运行日志落盘目录
```

### 配置 .env

先复制环境变量模板并按需填值。`env_file` 指向 `.env`，里面的值在容器内以环境变量生效：

```bash
cd byteAgent
cp .env.example .env
vi .env
```

必填项：

```dotenv
# Home Assistant 连接
HA_BASE_URL=http://<HA 服务器内网 IP>:8123
HA_TOKEN=<长期访问令牌>

# 安全密钥，生产必须替换成随机长字符串
APP_SECRET_KEY=<随机 32+ 位字符串>
APP_ENCRYPTION_KEY=<可选，随机字符串>

# 首个管理员账号
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=<强密码>
```

说明：

- `HA_TOKEN` 在 Home Assistant 的「个人资料 → 长期访问令牌」页面生成。
- `APP_SECRET_KEY` 用于 JWT 签名，可用 `openssl rand -hex 32` 生成。
- `APP_ENCRYPTION_KEY` 为空时由 `APP_SECRET_KEY` 派生，用于加密保存的 LLM API Key。
- LLM 的 provider、模型、API Key 无需写进 `.env`，在 WebUI 配置页填写即可，保存时会加密。

### 构建前端

前端不会在容器启动时实时编译。先在服务器上（或本机构建后连同 `dist` 一起上传）执行一次生产构建：

```bash
cd frontend
npm install
npm run build
# 构建产物会写入 frontend/dist
cd ..
```

> 内网服务器无法访问 npm 源时，可在有网络的本机执行 `npm run build`，把 `frontend/` 整个目录（含 `dist/`）上传到服务器。

### docker-compose.yml

项目根部的 `docker-compose.yml` 负责起后端容器。为了一次性把前端也托管起来，推荐在下方基础上追加一个 `web` 服务,用 Nginx 托管 `dist` 并反向代理 `/api`。

以下是一份可用的完整编排（把后端与前端一起照管）：

```yaml
services:
  agent:
    build: ./backend
    container_name: home-agent
    restart: unless-stopped
    env_file:
      - .env
    environment:
      DATABASE_URL: sqlite+aiosqlite:////app/data/agent.db
      AUTO_CREATE_SCHEMA: "false"
    expose:
      - "8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s

  web:
    image: nginx:1.27-alpine
    container_name: home-agent-web
    restart: unless-stopped
    depends_on:
      - agent
    ports:
      - "80:80"
    volumes:
      - ./frontend/dist:/usr/share/nginx/html:ro
      - ./web.conf:/etc/nginx/conf.d/default.conf:ro
```

说明：

- 后端容器只暴露内部端口 `8000`，不直接对外，由 Nginx 转发。
- 若你想用后端默认端口 `8000` 直接对外访问 API，可把 `expose` 改回 `ports: ["8000:8000"]`，但这样前端 `/api` 仍需 Nginx 转发。
- `web.conf` 见下节。

### web.conf（Nginx 配置）

新建 `web.conf`，内容如下。它把首页交给前端 `dist`，把 `/api` 与 `healthz` 等后端接口转发给后端容器：

```nginx
server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    # 前端 SPA 路由回退
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 反向代理到后端
    location /api/ {
        proxy_pass http://agent:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # SSE 推送需要关闭缓冲
        proxy_buffering off;
    }

    # 健康检查直通后端
    location = /healthz {
        proxy_pass http://agent:8000;
    }
    location = /readyz {
        proxy_pass http://agent:8000;
    }
}
```

> 后端容器名是 `agent`，Nginx 里用 `http://agent:8000` 即可从 `depends_on` 关联的同一 Docker 网络中访问。

### 启动

全部就绪后，在项目根部执行：

```bash
docker compose up -d --build
docker compose ps
```

等待 `agent` 的健康检查通过（首次 `start_period` 约 20 秒），然后访问：

```text
http://<服务器 IP>/
```

用 `.env` 里 `INITIAL_ADMIN_USERNAME` / `INITIAL_ADMIN_PASSWORD` 登录。

查看日志：

```bash
docker compose logs -f agent
docker compose logs -f web
```

### 升级与新版本

- 拉取新代码后重新构建后端镜像与前端产物：

```bash
cd frontend && npm run build && cd ..
docker compose build agent
docker compose up -d
```

- 数据库沿用 `./data` 落盘目录，容器重建不会丢失数据。
- 变更涉及表结构时，后端容器启动时会先执行 `alembic upgrade head`，再启动 `uvicorn`（见 `backend/Dockerfile`）。

### Docker 部署常见问题

| 现象 | 处理 |
| --- | --- |
| 后端一直重启 / 健康检查失败 | 查看 `docker compose logs agent`；多数是 `.env` 缺项或 LLM/HA 未配置导致 `/readyz` 503，此时应看 `/healthz` 是否存活 |
| 前端能打开但页面白屏 | 确认 `web.conf` 已挂载到 `/etc/nginx/conf.d/default.conf`，且 `frontend/dist` 存在 |
| 登录一直转圈 | 确认 Nginx 的 `/api` 已代理到 `http://agent:8000`；在后端日志或 `curl http://<服务器>:/api/channels` 排查 |
| 访问不到 Home Assistant | 确认 HA 地址是后端容器可达的地址；容器无法访问 `localhost` 指向的 HA，需填 HA 所在主机的局域网 IP |
| 数据库锁错误 | 确认没有被多份 `docker compose` 同时指向同一个 `./data` 目录 |

## 方法二：原生进程部署（备选）

不依赖 Docker 时，可在服务器或本机用 Python 虚拟环境 + Nginx/systemd 运行。

### 后端

```bash
cd backend

# 创建虚拟环境并安装
python -m venv .venv
.\.venv\Scripts\activate          # Windows
# . .venv/bin/activate            # Linux / macOS

pip install -e .
```

准备 `.env`（同 Docker 方式的必填项，`DATABASE_URL` 保持默认即可）：

```bash
cp .env.example .env && vi .env
```

应用数据库迁移并启动（单进程）：

```bash
alembic upgrade head
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
# Linux/macOS 用：
# ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

推荐用 systemd 托管：

```ini
# /etc/systemd/system/home-agent.service
[Unit]
Description=Home Agent backend
After=network.target

[Service]
WorkingDirectory=/opt/byteAgent/backend
ExecStart=/opt/byteAgent/backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now home-agent
```

### 前端

构建产物由任意静态服务器托管即可：

```bash
cd frontend && npm run build
```

配置 Nginx，把 `/` 指向 `frontend/dist`，/`/api/` 反向代理到 `127.0.0.1:8000`，SSE 相关 `proxy_buffering off`。Nginx 配置与上文 `web.conf` 基本一致，只需把 `proxy_pass http://agent:8000` 改为 `http://127.0.0.1:8000`。

Windows 内测可用 NSSM 或任务计划程序托管上述后端启动命令；更细致的运维说明（备份、日志、健康检查语义、安全要求）见《部署与运维.md》。

## 健康检查

- `GET /healthz`：进程存活探针，返回 `{"status": "ok"}`。
- `GET /readyz`：完整能力探针，检查数据库、Home Assistant WebSocket、LLM 是否就绪；任一项不满足返回 503 并在 `checks` 里列原因。

Docker 健康检查使用 `/healthz` 判断存活。LLM 或 HA 未配置时 `/readyz` 返回 503 是预期的，不代表进程崩溃。

## 数据与日志落盘

- 数据库：SQLite，默认落在 `data/agent.db`，通过 volume 持久化。
- 日志：后端结构日志输出到 stdout，由容器采集；也可重定向写入 `logs/`。

备份数据库优先使用 SQLite 在线备份命令，详见《部署与运维.md》。