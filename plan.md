# 基于 LangGraph 的人机协同邮件智能助手 · 实施计划与设计文档

版本：v0.1
日期：2026-09-22
依据：`requirements.md` v0.1

## 0. 与需求文档的对齐说明

1. “可回溯历史未读”的语义：仅未读邮件可进入处理流程；每轮只拉取当前前 `MAIL_FETCH_LIMIT`（默认 10 封）封，人工确认已读后，下一轮自动补充后续未读邮件。
2. 需求文档第 6 章“历史邮件全量回溯与清洗”不在范围内，指的是**已读**邮件不回溯、不清洗；未读邮件的回溯处理属于 v1 范围。
3. 同一封邮件以 `Message-ID`（缺失时用内容哈希回退）做唯一标识，已处理过的不重复进入流程。

## 1. 需求优先级拆分

### 1.1 P0 / 核心任务（不完成无法通过）

| 编号 | 任务 | 内容 | 验收要点 |
|------|------|------|----------|
| P0-1 | 项目骨架与配置 | Python 项目结构、依赖清单、`.env.example`、`Settings` 配置校验 | 服务可启动；缺关键配置时给出明确错误；`MAIL_FETCH_LIMIT` 默认 10 |
| P0-2 | SQLite 存储与状态机 | `emails` / `drafts` / `audit_logs` 三张表、仓储层、状态流转实现 | 状态只能按定义转移；重启后数据不丢；`Message-ID` 唯一约束生效 |
| P0-3 | IMAP 拉取与解析 | QQ 邮箱授权码登录、轮询收件箱未读、MIME 解析、纯文本提取、正文截断 | 能拉取当前前 10 封未读并维持队列；重复邮件去重；提取发件人/主题/正文/关键邮件头 |
| P0-4 | 确定性分类规则 | `Auto-Submitted`、`Precedence`、系统发件人、“请勿回复”等规则引擎 | 规则命中的邮件不调用 LLM；典型系统邮件判为 `ignore` |
| P0-5 | LLM 结构化分类与 Key 回退 | DeepSeek 优先、OpenAI 回退；Pydantic 校验；解析失败重试 | 解析失败重试后默认 `notify`；DeepSeek 超时可切 OpenAI；鉴权失败页面告警 |
| P0-6 | LangGraph 工作流与 HITL 中断 | 状态图、checkpointer、草稿生成、发送前强制中断 | `respond` 邮件停在 `DRAFT_PENDING`；重启后可恢复；模型节点无发送工具 |
| P0-7 | 本地审核页 | 待处理队列、已发送列表、处理历史（每页 10 条）、邮件详情、草稿编辑、四类操作（批准发送/修改后发送/忽略/反馈重写） | 四类操作全部可用并落库；`new` 不作为常驻筛选；历史分页与返回页码正确；仅绑定 127.0.0.1 |
| P0-8 | SMTP 人工触发发送 | 仅由审核动作触发、幂等防重、失败标记 | 无人工操作绝不发送；重复点击不重复发送；失败进入 `SEND_FAILED` 仅人工重试 |
| P0-9 | 端到端主流程与安全验收 | 轮询调度串联全流程；安全用例（不发送、防注入、防重复） | 从收信到审核发送全链路走通；安全用例全部通过 |

### 1.2 P1 / 重要任务（影响主要功能完整性）

| 编号 | 任务 | 内容 | 验收要点 |
|------|------|------|----------|
| P1-1 | notify 展示与人工升级 | `notify` 邮件高亮；可一键升级为 `respond` 生成草稿 | 升级后进入 `DRAFT_PENDING`，升级动作写入审核日志 |
| P1-2 | 重写反馈闭环 | 用户反馈驱动草稿重写，保留草稿历史，最多 5 轮 | 每轮草稿可追溯；第 5 轮后提示上限，用户只能编辑或忽略 |
| P1-3 | 错误分级与回退完善 | 区分超时/限流/鉴权错误；不同错误不同策略；页面告警 | 超时/限流切换 provider 重试一次；鉴权失败不静默循环 |
| P1-4 | 重启恢复一致性 | checkpoint 与业务状态对齐，异常退出后自愈 | 模拟崩溃重启：草稿、轮次、待审队列一致，不重复发送 |
| P1-5 | 审核记录展示 | 详情页展示操作历史、分类理由、草稿版本 | 每次操作可追溯（谁、何时、做了什么） |
| P1-6 | 文档完备 | README：安装、QQ 授权码获取、启动步骤、隐私数据流说明 | 新用户按文档可独立跑通 |

### 1.3 P2 / 优化任务（边界增强、代码质量、性能与体验）

| 编号 | 任务 | 内容 |
|------|------|------|
| P2-1 | 审核 UI/UX 优化 | 按分类/状态筛选、搜索、状态徽标、键盘快捷键、未读数提示 |
| P2-2 | 结构化日志与统计 | 统一日志格式、LLM 调用耗时与 token 统计、收发统计 |
| P2-3 | SQLite 调优 | WAL 模式、索引、`busy_timeout`、数据清理策略 |
| P2-4 | 并发与轮询锁 | 防止轮询重入；发送动作加锁，杜绝并发重复发送 |
| P2-5 | 分类与草稿质量调优 | 提示词迭代、误判样本回归集、置信度阈值调参 |
| P2-6 | 安全加固补充 | prompt injection 用例集扩充、本机绑定复核、敏感信息脱敏选项 |
| P2-7 | 测试覆盖率与 CI | 覆盖率目标、一键跑测脚本、基础 lint |

## 2. 总体设计

### 2.1 技术选型

| 类别 | 选择 | 说明 |
|------|------|------|
| 语言 | Python 3.11+ | 与 LangGraph 生态匹配 |
| 工作流 | LangGraph + `langgraph-checkpoint-sqlite` | 状态图与 HITL 中断 |
| LLM 访问 | `openai` SDK | DeepSeek 使用兼容 `base_url`；OpenAI 直连 |
| 校验 | Pydantic v2 / `pydantic-settings` | 结构化输出 schema 与配置 |
| Web | FastAPI + Uvicorn + Jinja2 | 本地单人审核页，无需前端构建链 |
| 存储 | SQLite（SQLAlchemy 2.0） | 业务数据与 checkpoint 同库分表 |
| 邮件 | `imaplib` + `email` 标准库 / `smtplib` | 满足单邮箱场景 |
| 测试 | pytest、pytest-asyncio、FastAPI TestClient | 单元/集成/端到端分层 |

### 2.2 模块拆分与依赖关系

```text
┌────────────┐     ┌──────────────┐     ┌───────────────┐
│ scheduler  │────▶│ mailbox.imap │────▶│ mailbox.parser│
└─────┬──────┘     └──────────────┘     └───────┬───────┘
      │                                        │ EmailContent
      │                                        ▼
      │            ┌────────────┐        ┌──────────┐
      └───────────▶│ agent.graph│◀──────▶│  store   │
                   └─────┬──────┘        └────▲─────┘
              ┌──────────┼──────────┐         │
              ▼          ▼          ▼         │
        agent.router agent.drafter llm.provider
              │                          │
              └── 规则优先，LLM 兜底 ────┘

┌────────────┐  人工动作   ┌──────────────┐
│ web.review │───────────▶│ mailbox.smtp │
└─────┬──────┘            └──────────────┘
      │ resume / 查询
      ▼
  agent.graph + store
```

依赖方向约定：

1. `config` 被所有模块依赖，不依赖业务模块。
2. `store` 只被业务模块读写，不依赖 IMAP/LLM。
3. `agent.graph` 依赖 `router` / `drafter` / `llm` / `store`，是流程唯一编排者。
4. `mailbox.smtp` 只被 `web` 的人工发送动作调用；`agent` 永远不 import `smtp`。
5. `web` 通过 resume graph 与查询 store 实现审核，不直接改分类结果。

### 2.3 目录结构

```text
demo7/
├── requirements.md          # 需求文档
├── plan.md                  # 本文档
├── README.md
├── .env.example
├── pyproject.toml           # 依赖与工具配置
├── app/
│   ├── __init__.py
│   ├── config.py            # Settings：.env 加载与校验
│   ├── main.py              # FastAPI 入口：lifespan 启动轮询
│   ├── scheduler.py         # 后台轮询任务
│   ├── mailbox/
│   │   ├── __init__.py
│   │   ├── imap_client.py   # 连接、拉取未读（分批）
│   │   ├── parser.py        # MIME → EmailContent
│   │   └── smtp_client.py   # 发送回复（仅人工触发）
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── state.py         # LangGraph AgentState
│   │   ├── graph.py         # 状态图 + checkpointer + interrupt
│   │   ├── router.py        # 规则路由 + LLM 兜底
│   │   ├── drafter.py       # 草稿生成与重写
│   │   └── prompts.py       # 提示词模板（正文按数据处理）
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── provider.py      # provider 注册、回退链、错误分级
│   │   └── schemas.py       # RouteDecision 等 Pydantic 模型
│   ├── store/
│   │   ├── __init__.py
│   │   ├── db.py            # 引擎、会话、建表
│   │   ├── models.py        # EmailRecord / DraftRecord / AuditLog
│   │   └── repo.py          # 仓储函数与状态转移守卫
│   └── web/
│       ├── __init__.py
│       ├── routes.py        # 页面与 API
│       └── templates/
│           ├── base.html
│           ├── index.html   # 待审列表
│           └── detail.html  # 详情 + 草稿操作
├── tests/
│   ├── conftest.py          # 临时库、FakeLLM、FakeMailbox
│   ├── test_router_rules.py
│   ├── test_llm_provider.py
│   ├── test_state_machine.py
│   ├── test_graph_hitl.py
│   ├── test_web_actions.py
│   └── test_e2e_flow.py
└── data/                    # 运行时 SQLite（加入 .gitignore）
```

## 3. 数据结构与状态设计

### 3.1 存储模型

`emails`（一封已进入流程的邮件一条记录）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 内部主键 |
| `message_id` | TEXT UNIQUE | 邮件 `Message-ID`；缺失时用 `sha256(from+subject+date+body)` 回退 |
| `sender` / `recipient` / `reply_to` | TEXT | 解析后的地址 |
| `subject` | TEXT | 主题（解码后） |
| `received_at` | DATETIME | 接收时间 |
| `headers_json` | TEXT | `Auto-Submitted`、`Precedence` 等关键头 |
| `body_text` | TEXT | 纯文本正文（截断后） |
| `body_truncated` | BOOL | 是否截断 |
| `category` | TEXT | `ignore` / `notify` / `respond` |
| `route_reason` | TEXT | 判定理由（规则名或 LLM 理由） |
| `route_source` | TEXT | `rule` / `llm` / `fallback` |
| `status` | TEXT | 业务状态（见 3.3） |
| `sent_kind` | TEXT NULL | `approved` / `edited` |
| `rewrite_rounds` | INTEGER | 当前重写轮次，默认 0 |
| `created_at` / `updated_at` | DATETIME | 审计时间 |

`drafts`（草稿版本，一封邮件多轮多条）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 主键 |
| `email_id` | FK → emails | 所属邮件 |
| `content` | TEXT | 草稿全文 |
| `round_number` | INTEGER | 第几版（0 为初稿） |
| `feedback` | TEXT NULL | 触发该版的重写反馈 |
| `source` | TEXT | `llm` / `user_edit` |
| `created_at` | DATETIME | 生成时间 |

`audit_logs`（操作留痕）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 主键 |
| `email_id` | FK → emails | 关联邮件 |
| `action` | TEXT | `approve_send` / `edit_send` / `ignore` / `rewrite` / `upgrade` / `retry_send` 等 |
| `detail_json` | TEXT | 操作附注（如反馈内容、失败原因） |
| `created_at` | DATETIME | 操作时间 |

### 3.2 LangGraph AgentState

```python
class AgentState(TypedDict):
    email_id: int
    email: dict              # EmailContent 快照（不含附件）
    category: str            # ignore / notify / respond
    route_reason: str
    route_source: str        # rule / llm / fallback
    draft: str               # 当前草稿
    feedback: str            # 用户重写反馈
    rewrite_rounds: int
    pending_action: str      # approve / edit_send / ignore / rewrite / upgrade
    error: str | None
```

节点划分：

```text
START → parse_email → route(rule 优先) ─┬─ ignore → persist_ignore → END
                                        ├─ notify → persist_notify → END
                                        └─ respond → generate_draft
                                             → persist_draft_pending
                                             → interrupt()   # HITL 中断点
                                             → apply_action(approve/edit/ignore/rewrite)
                                             → END
```

安全要点：`apply_action` 中只有 `approve` / `edit_send` 分支会由 Web 层在 graph 执行完成后调用 `smtp_client` 发送；graph 节点本身不注册发送工具，保证“模型只写草稿”。

### 3.3 状态流转

```text
NEW ──route──▶ IGNORED            （终态，留痕）
   └─route──▶ NOTIFIED ──人工升级──▶ DRAFT_PENDING
   └─route──▶ DRAFT_PENDING

DRAFT_PENDING ──批准──▶ SENT_DONE(sent_kind=approved)
DRAFT_PENDING ──修改发送──▶ SENT_DONE(sent_kind=edited)
DRAFT_PENDING ──人工忽略──▶ IGNORED
DRAFT_PENDING ──反馈重写(≤5轮)──▶ DRAFT_PENDING（rounds+1，保留旧草稿）
DRAFT_PENDING / SEND_FAILED ──发送失败──▶ SEND_FAILED
SEND_FAILED ──人工重试成功──▶ SENT_DONE
```

状态转移守卫在 `store.repo` 统一实现：非法转移直接抛错并记录，例如 `NOTIFIED → SENT_DONE` 不允许（必须先升级生成草稿）。

### 3.4 异常处理矩阵

| 异常场景 | 处理策略 | 状态/表现 |
|----------|----------|-----------|
| IMAP 授权失败 | 启动时预检 + 页面顶部告警 | 轮询继续按间隔重试；已有审核不受影响 |
| IMAP 网络失败 | 记日志，下轮重试 | 邮件状态不变 |
| 邮件解析失败（编码/格式异常） | 尽力提取字段，正文标记 `解析受限` | 归入 `notify`，不丢弃 |
| LLM 超时/限流 | 切换下一个 provider 重试一次 | 仍失败则默认 `notify`（`route_source=fallback`） |
| LLM 结构化输出非法 | 同 provider 重试 1–2 次（带错误反馈） | 仍失败默认 `notify` |
| LLM 鉴权失败（401/403） | 不静默循环，页面配置告警 | 该批规则可判的正常处理，其余默认 `notify` |
| SMTP 发送失败 | 标记 `SEND_FAILED`，记录原因 | 仅允许人工重试；不自动重发 |
| 重复点击发送 | 以 email 为粒度加发送锁 + 状态检查 | 第二次请求直接拒绝并提示 |
| 服务重启 | SQLite + checkpointer 恢复 | 待审队列、草稿、轮次一致；不重复发送 |
| 数据库并发写 | WAL + `busy_timeout` + 短事务 | 写冲突重试一次后报错 |

## 4. 开发顺序与验收标准

| 里程碑 | 内容 | 对应任务 | 验收标准 |
--------|------|----------|----------|
| M0 | 骨架与配置：目录、依赖、`Settings`、`.env.example`、启动入口 | P0-1 | `pip install` 后服务可启动；缺配置时报错清晰；配置项与需求 9 一致 |
| M1 | 存储与状态机：建表、仓储、转移守卫 | P0-2 | 状态机单元测试通过；`Message-ID` 重复插入被拒；重启后数据完好 |
| M2 | IMAP 拉取与解析（先 FakeMailbox，后真机联调） | P0-3 | 拉取默认 10 封/批；确认已读后可补充后续未读；去重生效；正文截断有标记 |
| M3 | 确定性规则路由 | P0-4 | “请勿回复”、`Auto-Submitted`、`Precedence` 等用例全部命中且不调 LLM |
| M4 | LLM 适配层：结构化输出、重试、provider 回退 | P0-5 | FakeLLM 注入非法 JSON/超时/401 分别走对应分支；兜底为 `notify` |
| M5 | LangGraph 工作流：路由 → 草稿 → 中断 → 持久化 | P0-6 | `respond` 停在 `DRAFT_PENDING`；中断点重启可恢复；`ignore/notify` 正确落库 |
| M6 | 审核网页：队列、历史、详情、四类操作 | P0-7 | 页面操作全部落库；发送成功自动已读且保留在已发送/历史；无 JS 报错；仅监听 127.0.0.1 |
| M7 | SMTP 发送与幂等 | P0-8 | 只有批准/修改发送触发 SMTP；重复请求不重复发；失败进入 `SEND_FAILED` |
| M8 | 轮询调度与端到端串联 | P0-9 | FakeMailbox → 分类 → 草稿 → 审核 → FakeSMTP 全链路通过；安全用例通过 |
| M9 | 真机验收 + P1 完善 | P1-1～P1-6 | QQ 邮箱真实收发一封测试邮件；notify 升级、重写 5 轮、重启恢复、文档齐备 |
| M10 | P2 择优实施 | P2-1～P2-7 | 按时间盒选择：优先 UI 筛选、日志统计、并发锁 |

建议提交节奏：每个里程碑一组提交，M0–M8 全部使用 Fake 组件驱动，M9 才接真实邮箱，避免开发期误发邮件。

## 5. 测试策略

### 5.1 测试分层

1. **单元测试**
   - 规则路由：每条规则正反用例。
   - 解析器：中文编码、HTML 正文转纯文本、缺头、超大正文截断。
   - LLM schema：合法/非法 JSON、字段缺失、置信度边界。
   - 状态机：全部合法转移与代表性非法转移。
2. **集成测试**
   - graph + FakeLLM：路由、草稿、中断、恢复。
   - repo + 临时 SQLite：唯一约束、重启恢复。
   - FastAPI TestClient：四类审核操作 API 与页面渲染。
3. **端到端测试**
   - FakeIMAP + FakeLLM + FakeSMTP + TestClient 串全流程。
   - 真机冒烟（M9）：QQ 邮箱发送一封明确标注“测试”的邮件人工走完审核。
4. **安全专项（必须通过）**
   - 无人工操作时 SMTP 调用次数必须为 0。
   - 注入邮件（正文含伪造指令）不影响分类与草稿安全边界。
   - 并发/重复发送请求只产生一封外发邮件。

### 5.2 关键测试场景

| # | 场景 | 预期 |
|---|------|------|
| 1 | 正文含“系统自动邮件，请勿回复”且无重要行动信息 | 规则判 `ignore`，不调 LLM |
| 2 | `Auto-Submitted: auto` / `Precedence: bulk` 且无重要行动信息 | 规则判 `ignore` |
| 2a | “请勿直接回复”但含测评邀请、截止/失效时间 | 规则判 `notify`，不生成回复草稿 |
| 3 | 明确提问“请问方案什么时候能给到我？” | `respond`，生成含称呼/正文/署名的草稿 |
| 4 | 内容模糊（如纯通知但含少量疑问词） | LLM 低置信 → `notify` |
| 5 | LLM 返回非法 JSON | 重试 1–2 次后默认 `notify` |
| 6 | DeepSeek 超时 | 自动切 OpenAI 成功返回 |
| 7 | 双方 Key 均鉴权失败 | 页面告警，本批默认 `notify` |
| 8 | 用户修改草稿后发送 | 新草稿入 `drafts(source=user_edit)`，状态 `SENT_DONE(edited)` |
| 9 | 连续 6 次反馈重写 | 前 5 轮重写，第 6 次被拒并提示上限 |
| 10 | 全流程无任何人工操作 | SMTP 发送次数为 0 |
| 11 | 服务在 `DRAFT_PENDING` 时重启 | 队列、草稿、轮次恢复一致 |
| 12 | 收件箱有 23 封未读且当前队列已确认已读 | 下一轮拉取 10 封；再次确认后继续补充，无重复 |
| 13 | SMTP 网络失败 | 状态 `SEND_FAILED`，人工重试成功后 `SENT_DONE` 并自动标记已读 |
| 14 | 双击/并发发送 | 仅一封外发，第二次请求返回明确提示 |
| 15 | 正文含 prompt injection 指令 | 指令不生效，分类与草稿不越界 |
| 16 | 超大正文（> `MAX_EMAIL_BODY_CHARS`） | 截断送 LLM，页面标注“正文已截断” |
| 17 | 当前 10 封未读入库后尚未人工确认已读 | 重复轮询不扩展队列、不重复执行工作流；QQ 未读标志等待批量/单封确认后清除 |
| 18 | 人工发送成功但 IMAP 标记已读失败 | 状态保持 SENT_DONE，不重复发送；页面告警并保留在已发送列表等待补标记 |

## 6. 风险与应对（实施期）

| 风险 | 应对 |
|------|------|
| 开发期误发真实邮件 | M0–M8 全部 Fake；SMTP 真实凭据只在 M9 配置；测试收件人固定为自己 |
| QQ 邮箱风控 | 轮询间隔 ≥60s；真机联调集中在短时间窗口完成 |
| DeepSeek/OpenAI 输出差异引发解析问题 | schema 校验 + 错误反馈重试 + 规则兜底三层防御 |
| LangGraph checkpoint 与业务状态不一致 | 以业务表为准，checkpoint 仅恢复流程；重启时做状态对账 |
| 范围蔓延 | 严格按 P0→P1→P2 顺序，P2 进时间盒 |
