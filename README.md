# Home Agent · 主动式家庭管家

基于 **FastAPI + Vue 3** 的智能家居语音管家。接入 Home Assistant，用「本地快路径 + 大模型慢推理」的双进程架构，让家庭成员通过**飞书聊天**直接对家里下达指令、接收主动推送。

## 特性

- **主动式管家**：不只是被动执行指令，会基于 Home Assistant 事件主动推理，形成提案并推送审批。
- **快 / 慢双路径**：
  - **快路径（Fast Path）**——本地规则引擎，指令明确、延迟敏感的场景秒回，不依赖大模型。
  - **慢路径**——大模型深度推理，处理意图不明确、需要上下文的任务，主 + 备用模型自动切换。
- **飞书消息渠道**：长连接（WebSocket）接收事件，**无需公网 IP**；支持图文消息与语音转文字下发指令。
- **成员与权限**：家庭成员账号、角色与细粒度设备权限，普通成员默认无权访问，需显式放行。
- **安全控制**：实体/服务/参数白名单、语音调试防护、审计日志、提案审批。
- **中文语音词库**：自然说法 → 设备功能的映射，支持导入汇总词表、设备能力覆盖、群控编排。

## 技术栈

| 端 | 技术 |
| --- | --- |
| 后端 | FastAPI · SQLite（aiosqlite）· Alembic · lark_oapi（飞书）· Home Assistant WebSocket/REST |
| 前端 | Vue 3 · Vite · naive-ui |
| 部署 | Docker Compose · Nginx 静态托管 + `/api` 反代 · systemd 原生进程（备选） |

## 目录结构

```text
byteAgent/
  backend/                # FastAPI 后端源码与迁移
    app/
      agent/              # 慢路径：推理 / 提案 / 上下文
      fastpath/           # 快路径：规则引擎 / 语音词库 / 实体
      ha/                 # Home Assistant 适配（WS + REST）
      channels/           # 消息渠道（飞书/语音转写）
      api/                # REST 路由
      safety/             # 白名单与安全防护
      db/ · core/ · services/  # 数据模型 / 配置 / 业务服务
    tests/
    pyproject.toml
  frontend/               # Vue 3 管理后台
  docs/                   # 部署教程 / 设置说明书 / 运维手册
  tools/                  # 开发调试与压测脚本
  docker-compose.yml      # 后端容器编排
  .env.example            # 环境变量模板（复制为 .env 使用）
```

## 快速开始

### 方式一：Docker Compose（推荐）

前置：已装 Docker 与 Compose 插件、一台可访问的 Home Assistant、一个大模型 API Key 或 Ollama。

```bash
cp .env.example .env
# 编辑 .env，填入 HA 地址/令牌、APP_SECRET_KEY、初始管理员账号
docker compose up -d --build
```

后端容器健康检查通过后，按需追加 Nginx `web` 服务托管前端 `dist` 并反代 `/api`。完整编排、Nginx 配置、升级与排障见 [docs/Docker部署教程.md](docs/Docker部署教程.md)。

> 后端必须**单进程**运行（`--workers 1`）：会话、SSE 推送、快路径缓存、限流计数均保存在进程内。

### 方式二：原生进程

```bash
# 后端
cd backend
python -m venv .venv
.\.venv\Scripts\activate          # Windows
# . .venv/bin/activate            # Linux / macOS
pip install -e .
alembic upgrade head
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8233 --workers 1

# 前端（另开终端）
cd frontend
npm install
npm run dev                       # 开发：访问 http://127.0.0.1:5173
# npm run build                   # 生产：产物写入 frontend/dist
```

## 环境变量

核心项（完整见 `.env.example`）：

| 变量 | 说明 |
| --- | --- |
| `HA_BASE_URL` / `HA_TOKEN` | Home Assistant 地址与长期访问令牌 |
| `APP_SECRET_KEY` | JWT 签名密钥，生产必须替换为随机长字符串 |
| `APP_ENCRYPTION_KEY` | 用于加密保存的 LLM API Key；留空则从 `APP_SECRET_KEY` 派生 |
| `INITIAL_ADMIN_USERNAME` / `INITIAL_ADMIN_PASSWORD` | 首个管理员账号 |
| `DATABASE_URL` | SQLite 连接串（默认 `sqlite+aiosqlite:///./data/agent.db`） |
| `AUTO_CREATE_SCHEMA` | 是否自动建表；生产建议 `false` 并手动 `alembic upgrade head` |

LLM 的 provider、模型、密钥不在 `.env` 中，登录后台后在「配置 → LLM」页填写并加密保存。

## 使用

登录后台后按顺序配置（详见 [docs/设置说明书.md](docs/设置说明书.md)）：

1. **Home Assistant** —— 填入地址与令牌，保存并重连。
2. **LLM** —— 配置主 / 备用模型，保存后测试连通。
3. **快捷规则 / 语音词库 / 白名单** —— 按需启用快路径能力与安全边界。
4. **成员** —— 添加家庭成员账号并授予设备权限。
5. **消息渠道** —— 接入飞书自建应用，绑定渠道账号到成员。

> **语音词库不会随克隆自动就绪。** 仓库只内置少量种子词条（见 `backend/app/fastpath/voice_seed.json`），完整的中文语音词库需手动导入：后台「语音技术 → 语音词库」页点「下载模板」获取导入格式（与本仓库 `backend/app/fastpath/voice_template.xlsx` 相同），按列填写后点「上传 xlsx」即可；官方汇总版系统词库示例见 `智能家居语音技能/`。未导入词库时，快路径只能按实体名称做基础匹配，其余指令会自然回退到慢思考（大模型）处理 —— 功能不受影响，只是少了一层本地命中。
>
> 相同文件重复导入会被自动去重；批量导入官方汇总版（约 13 万条）时，若部署在 Nginx 反向代理之后，需确认 `/api/` 的 `client_max_body_size` ≥ 20m 且读超时 ≥ 300s，否则可能 413 / 504（504 不一定是失败，后端常已成功入库）。

## 文档

- [Docker 部署教程](docs/Docker部署教程.md) —— Docker 与原生进程两种部署方式、升级与排障。
- [设置说明书](docs/设置说明书.md) —— 8 大配置标签图文说明。
- [部署与运维](docs/部署与运维.md) —— 备份、日志、健康检查与安全要求。

## 开发者

- `tools/` 下提供若干开发调试与性能压测脚本（快路径延迟、语音词库探测、LLM 连通性、持续监控等），配合后端 venv 运行。
- 数据库变更：`backend/alembic` 管理迁移，生产机 `AUTO_CREATE_SCHEMA=false` 时须执行 `alembic upgrade head`。

## 许可

内部 / 私有项目，未另作开源许可声明。