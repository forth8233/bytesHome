# 主动式家庭管家 Agent 方案说明书（FastAPI 自研版 V1\.1）

文档版本：V1\.1　｜　文档状态：正式评审稿，开发基线　｜　选型结论：从零自研 FastAPI，不基于 OpenClaw

变更记录：V1\.1 新增 LLM 多模型支持（Ollama 本地模型 \+ DeepSeek 等第三方在线 API）。

# 项目概述

## 项目背景

Home Assistant 原生自动化为静态硬规则，难以实现结合设备时序状态、用户历史偏好、家庭常识的柔性智能判断；HA Assist 仅支持被动问答，无法主动感知环境变化、主动发起提醒与建议。Miloco 等同类方案强绑定单一生态（米家），无法适配已混合接入 Home Assistant 的多品牌智能家居设备。

面向 HA 深度玩家、自托管爱好者，需要一套零侵入 HA、内网可闭环、隐私可控、推理过程可观测、行为严格可控的主动管家 Agent。

## 产品定位

主动式家庭管家 Agent 是独立后台服务，依托 Home Assistant 开放 API 构建，使用 **HA WebSocket API（事件订阅）\+ HA REST API（状态查询、设备控制）\+ HA 长期令牌** 完成对接。Agent 以家庭设备事件作为核心驱动源，7×24 小时常驻后台运行，同时兼容用户主动对话指令。

核心特点：主动值守为主、被动对话为辅；AI 仅生成提案建议，高危操作强制人工确认；默认保守安全策略。LLM 同时支持 Ollama 本地大模型（数据内网闭环）与 DeepSeek 等在线 API。

## 设计原则

- **零侵入 HA：**不安装 HA 插件、不修改 HA 内部配置，全部交互走官方开放 API 与长期令牌。

- **最小权限：**HA 侧使用独立专用账号 \+ 长期令牌；Agent 内部叠加实体/服务白名单双重校验。

- **可解释优先：**所有推理链路、Agent 思考内容、用户反馈、设备动作全部落审计日志，拒绝黑盒。

- **解耦分层：**外部依赖（HA、LLM、数据库）统一通过适配器封装，便于替换。

- **本地优先、模型可选：**支持 Ollama 本地模型，数据不出内网；也支持 DeepSeek 等在线模型，在线模式增加明确风险提示。

- **故障可降级：**LLM 不可用时主动推理暂停；HA 断连后自动重试，不阻塞进程；支持一键关闭全部主动推理，退化为纯对话助手。

## 目标用户

Home Assistant 深度玩家、自托管爱好者；拥有小主机（N100 等），熟悉 Docker 部署，接受配置令牌、查看日志调优；传感器、智能家居设备已接入 HA。本期 MVP 不面向零基础小白用户。

# 需求范围（MoSCoW）

## Must｜MVP 必须实现（内测基线）

1. **HA 对接能力：**配置 HA 地址与长期访问令牌、连通性检测；WebSocket 长连接订阅 state\_changed 事件流，断线指数退避自动重连；本地内存状态缓存（首次全量拉取白名单快照，后续增量更新）；REST 客户端查询状态、调用服务；实体与服务白名单，不在白名单内的实体 Agent 不可见、不可操作。

2. **事件预处理流水线：**过滤无状态变更、无关属性抖动、非白名单实体事件；滑动窗口防抖聚合抑制传感器高频抖动；事件标准化、类型打标、预风险标记（如水浸直接标记高风险）。

3. **事件总线与主动推理链路：**进程内异步事件总线，解耦生产者、推理消费、日志消费；上下文按需组装，仅召回相关设备状态、历史反馈、成员偏好，避免 Prompt 膨胀；LLM 统一适配器同时支持 Ollama 本地与 DeepSeek 在线，支持故障自动降级；Pydantic Schema 强校验输出，强制结构化提案 JSON。

4. **提案审批状态机（核心业务）：**状态为 pending（待确认）/ approved（同意）/ rejected（拒绝）/ timeout（超时）；内置硬编码风险分级，LLM 无权修改；前端 SSE 实时推送；超时自动关闭（默认 15 分钟）；审批通过后风控二次校验再调用 HA，同意/拒绝结果写入反馈记忆。

5. **被动对话能力（对标 HA Assist）：**Web 端自然语言查询状态、控制设备；一句话生成 HA 自动化 YAML，预览确认后再写入；会话短期上下文，对话历史持久化。

6. **结构化记忆管理：**家庭成员账号、角色、独立通知渠道与设备权限；成员偏好 key\-value 存储；用户反馈记录表；Web 记忆管理页面（查看、编辑、单条删除、一键清空）。

7. **LLM 配置管理：**提供商选择（ollama / deepseek）、模型名、接口地址、密钥；连通性测试、降级备选模型；在线模式隐私风险提示；密钥加密存储，禁止明文输出日志。

8. **配置、审计与可观测：**HA 连接、白名单、LLM 参数、防抖窗口、提案超时、主动推理总开关；全链路审计日志；JWT 账号鉴权，区分管理员与普通成员。

9. **部署：**Docker Compose 一键部署，单进程 FastAPI 服务。

## Should｜V2 版本（MVP 稳定后迭代）

- RAG 向量常识库：自然语言录入家庭自定义规则，向量检索辅助推理。

- 细粒度自动策略面板：可视化配置【事件类型 \+ 动作】维度自动放行策略。

- TTS 语音播报通知，复用 HA TTS 能力。

- 多通知渠道扩展：企业微信、Telegram 推送。

- 时序聚合判断：基于一段时间连续状态推理（如窗户持续打开 1 小时）。

## Could｜V3 可选增值（插件化）

- 独立 CV 视觉插件：外部视觉服务输出结构化事件（跌倒、区域有人）推送给 Agent，Agent 不直接处理图片。

- 定时全局巡检任务（离家模式周期性安全检查）。

- 长期任务跟踪（服药提醒、晾晒提醒等）。

- 插件扩展机制，支持自定义 Skill。

## Won't｜本期明确不做

- 自研 Zigbee/BLE/Matter 网关、设备协议栈；所有设备统一接入 HA。

- 自研图像识别、CV 模型。

- 公网多租户商用、付费系统。

- 超长链式复杂任务规划。

# 系统架构

## 分层架构（依赖向下）

整体分为接入层、FastAPI 核心服务、外部依赖底座三层，依赖自上而下单向流动，外部依赖全部通过适配器封装。

## 两条核心数据流

系统同时承载主动事件流（核心）与被动对话流（兼容），两者复用同一套 LLM 适配器与工具能力。

**主动事件流：**HA 设备状态变更 → WS Client → 预处理（过滤、防抖、打标）→ 事件总线 → 推理调度 → 生成结构化提案 → 入库 pending → SSE 推送 → 用户同意/拒绝/超时 → 同意分支风控校验后执行 HA 动作，同时写入反馈记忆。闭环流程与提案状态机如下图。

**被动对话流：**Web 前端发送用户消息 → 对话会话服务 → 推理调度（复用 LLM 适配器、工具能力）→ 回答 / 调用 HA 工具 / 生成自动化 YAML → 返回前端，对话记录入库。

# 核心模块详细说明

## HA Adapter（ha\_client）

隔离所有 HA 交互逻辑，上层业务不感知 HA 原始协议。

- **WebSocket 常驻协程：**独立后台 Task，捕获异常、日志告警，单个连接异常不导致整个服务崩溃。

- **StateCache：**内存字典维护白名单实体最新状态；初始化全量拉取快照，后续增量更新；对外提供只读查询接口。

- **异步 REST Client：**封装服务调用、状态拉取，内置超时、重试。

- **白名单前置拦截：**请求实体/服务不在白名单直接拒绝，不转发 HA。

## 事件预处理流水线

降噪层，控制 LLM 调用量，避免频繁轰炸用户。输出标准化 ProcessedEvent 模型，包含 entity\_id、事件类型、新旧状态、时间戳、预标记风险等级。

## 内部事件总线

MVP 不引入消息队列，基于 asyncio\.Queue 实现轻量进程内订阅；生产者与消费者解耦，后续新增消费分支（指标、第三方告警）无需改动推理主链路。

## 推理调度核心

- **上下文按需召回：**禁止全量设备状态灌入 Prompt，只筛选与当前事件相关实体、同类型历史反馈、成员偏好。

- **LiteLLM 适配器：**统一封装 ollama、deepseek，抹平接口差异，统一 function call 解析、异常捕获、JSON Schema 约束，支持故障自动切换备选降级模型。

- **强制结构化输出：**定义固定 AgentProposal Pydantic 模型；输出不符合 Schema 直接丢弃，记录 trace，支持重试 \+ 降级。

```python
class AgentProposal(BaseModel):
    thought: str            # 思考过程，用于面板展示，解决黑盒
    risk_level: Literal["low", "medium", "high"]
    suggestion: str
    actions: list[HAServiceAction]
    auto_allow: bool        # 是否允许自动执行；high 风险强制为 False
```

LLM 配置模型如下，支持本地与在线两套形态，并可配置降级备选：

```python
class LLMConfig(BaseModel):
    provider: Literal["ollama", "deepseek"]
    model_name: str
    api_base: Optional[str]
    api_key: Optional[str]
    temperature: float = 0.1   # 低随机性，优先稳定输出 JSON
    timeout: int = 60
    fallback_provider: Optional[Literal["ollama", "deepseek"]]
    fallback_model: Optional[str]
```

隐私说明：Ollama 本地模式所有 Prompt、设备数据不出内网；DeepSeek 在线模式家庭设备、事件、偏好信息会上传服务商，前端配置页强制展示风险提示。

## 提案审批状态机

区别于普通对话 Agent 的核心模块，为异步状态机。独立后台定时扫描任务自动处理超时 pending 提案；风控校验与 LLM 解耦，风险等级、黑名单服务写死在代码配置，AI 只给出建议，没有执行决策权。

## 记忆服务

两类记忆物理隔离：结构化记忆（关系数据库，MVP 核心）适合偏好、统计类反馈、策略，查询快、可精确匹配；向量 RAG 记忆（V2）适合非结构化自然语言家庭规则，语义检索，MVP 关闭以降低复杂度。

## 对话会话服务

负责用户主动问答，支持设备查询、设备控制、一句话创建 HA 自动化；自动化 YAML 必须经过用户预览确认才调用 HA API 写入，禁止静默修改 HA 配置。

# 数据模型（数据库表）

基于 SQLAlchemy，MVP 最小表集合如下：

|表名|用途与关键字段|
|---|---|
|**member**|成员：id、名称、密码哈希、角色、创建时间|
|**member\_preference**|member\_id \+ key \+ value，用户结构化偏好（如空调目标温度）|
|**processed\_event\_log**|清洗后的事件日志，用于回溯|
|**proposal**|提案主表（状态机核心）：risk\_level、actions\_json、status、expire\_at、operated\_at|
|**user\_feedback**|历史反馈：事件类型、动作签名、是否同意、计数、最后触发时间|
|**auto\_policy**|用户自定义自动放行策略（事件类型 \+ 动作签名 \+ 开关）|
|**reasoning\_trace**|推理审计追踪：输入摘要、LLM 输出、耗时、错误|
|**system\_config**|全局配置：HA 参数、LLM 配置、开关参数（LLM 密钥加密存储）|

# API 接口总览（REST \+ SSE）

所有接口携带 JWT 鉴权，区分管理员与普通成员权限。

|分组|接口|说明|
|---|---|---|
|HA 配置|GET/POST /api/config/ha|HA 连接配置、连通性测试|
|白名单|GET/POST /api/config/whitelist|管理可见可控实体白名单|
|全局配置|GET/POST /api/config/global|主动推理开关、防抖、超时参数|
|LLM 配置|GET/POST /api/config/llm|提供商、模型、密钥、降级配置、连通性测试|
|LLM 配置|GET /api/config/llm/providers|获取支持的模型提供商列表|
|记忆管理|GET/POST/PUT/DELETE /api/memories/preference|成员偏好管理|
|记忆管理|GET /api/memories/feedback|查看历史反馈记忆|
|提案审批|GET /api/proposal|分页查询提案历史|
|提案审批|POST /api/proposal/\{pid\}/approve|同意提案|
|提案审批|POST /api/proposal/\{pid\}/reject|拒绝提案|
|对话|POST /api/chat|用户发送对话消息（被动入口）|
|对话|GET /api/chat/history|会话历史|
|实时推送|GET /api/sse/notify|前端订阅 SSE，接收新提案、告警|
|审计追踪|GET /api/trace/reasoning|推理日志回溯，查看 Agent 思考过程|

# 技术栈清单

|**Web 框架**|FastAPI（async）|
|---|---|
|**ORM / 校验**|SQLAlchemy 2\.0 Async、Pydantic v2|
|**HA 客户端**|websockets（WS）\+ httpx\.AsyncClient（REST）|
|**LLM 适配**|litellm（统一 ollama、deepseek，function call、JSON Schema）|
|**数据库**|SQLite（MVP），可平滑迁移 Postgres；向量库 Qdrant（V2）|
|**前端 / 推送**|Vue3 \+ NaiveUI；SSE（Server\-Sent Event）|
|**部署 / 日志**|Docker \+ Docker Compose，uvicorn 运行；structlog 结构化日志|

# 并发模型与后台任务

服务运行在同一个 asyncio 事件循环，包含两类任务：HTTP 请求任务由 uvicorn 自动管理；后台常驻异步 Task 包括 HA WebSocket 监听任务、提案超时扫描任务。

- 所有后台 Task 增加全局异常捕获、日志告警，单个 Task 崩溃不带走整个 event loop。

- 共享内存状态缓存使用 asyncio\.Lock 保护并发读写。

- 禁止在 async 代码中使用同步阻塞 IO。

# 安全设计

1. **HA 侧最小权限：**单独新建 Agent 专用账号生成长期令牌，配合 HA 权限集成进一步限制可见实体。

2. **Agent 侧二次校验：**白名单校验，高危 domain（lock、valve、alarm\_control\_panel）硬编码拦截。

3. **密钥管理：**HA token、LLM 密钥优先从环境变量/\.env 注入；数据库中加密存储，禁止明文写入日志、trace、前端页面。

4. **网络：**HA 8123 端口不直接暴露公网，Agent 与 HA 在内网通信。

5. **鉴权：**Web 端 JWT 登录，管理员账号单独权限。

6. **执行边界：**AI 生成动作仅作为提案，高危动作强制人工确认。

7. **在线模型风险提示：**DeepSeek 等在线 API 模式在配置页强制提示隐私风险，不可关闭。

# 迭代计划

|里程碑|交付内容|
|---|---|
|**M1 底座搭建（1\~2 周）**|HA Adapter、WS 订阅、状态缓存、REST 调用、白名单、原始事件落库|
|**M2 事件流水线（1 周）**|事件过滤、滑动窗口防抖、标准化事件、内部事件总线|
|**M3 推理与提案闭环（2 周，核心）**|LiteLLM 适配器、ollama \+ deepseek 双模型、上下文组装、结构化提案、状态机、SSE 推送、审批 API、风控校验、降级重试|
|**M4 记忆、对话、Web 基础页（2 周）**|结构化记忆表、反馈回写、对话接口、基础 WebUI（聊天、审批、日志、LLM 配置页）|
|**M5 内测打磨（2 周）**|断线重连、超时处理、边界 case 测试、限流、错误兜底，完成 MVP 全部 Must 需求|
|**V2 迭代**|RAG 向量库、自动策略面板、多通知渠道、TTS 播报|
|**V3 迭代**|插件化扩展、视觉事件接入、定时巡检|

# 风险说明

- **HA 断连：**重启、网络抖动导致 WS 断开，内置指数退避重连，状态缓存可从快照恢复。

- **LLM 输出不稳定：**依靠 Pydantic 强校验 \+ 重试兜底，记录失败 trace；本地小参数模型更易出现 JSON 崩坏，依靠 fallback 降级。

- **传感器事件风暴：**预处理防抖、过滤、限流控制 LLM 调用频次。

- **在线模型限流计费：**增加 token 统计、速率限制，日志记录消耗。

- **权限泄露：**严格最小权限，双重白名单隔离。

# 选型对比：FastAPI 自研 vs OpenClaw Skill 扩展

|对比项|FastAPI 自研方案|OpenClaw Skill 扩展方案|
|---|---|---|
|数据流模型|原生事件流优先，适配主动值守|原生消息/请求驱动，WS 常驻任务寄生在 Skill，范式冲突|
|自由度|全链路可控，调度、状态机、记忆完全定制|受框架原有消息模型、Skill 生命周期约束|
|开发成本|前期脚手架工作量大|复用聊天、LLM、工具调用脚手架，前期上手快|
|长期维护|无上游版本合并冲突|上游更新易冲突，常驻后台任务属非标准用法|
|适合场景|以主动事件驱动为核心、长期持续迭代|快速验证、以对话为主，主动能力作为附加功能|

选型结论：本项目选择 FastAPI 自研方案，优先保证主动事件链路架构干净、可控。

> （注：部分内容由豆包工作 AI 生成）
