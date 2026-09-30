# 基于 LangGraph 的人机协同邮件智能助手

通过 IMAP 收取 QQ 邮箱未读邮件，经 `ignore / notify / respond` 三分类路由后生成回复草稿；所有外发邮件必须经本地审核页人工批准。

## 参考说明

本项目的“邮件 Agent + 人工审核”基础功能形态参考了 LangChain 官方示例 [agents-from-scratch](https://github.com/langchain-ai/agents-from-scratch)。本仓库未复制该项目代码；LangGraph 工作流、分类规则、存储状态机、审核网页、测试与文档均为独立实现。

## 文档

- 需求文档：[requirements.md](requirements.md)
- 实施计划与设计：[plan.md](plan.md)

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env
# 编辑 .env，填入 QQ 邮箱授权码与至少一个 LLM API Key
.venv\Scripts\python -m app.main
```

服务仅监听 `127.0.0.1:8000`，健康检查：`GET /api/health`。

应用启动后会立即执行一轮收信，之后按 `POLL_INTERVAL_SECONDS` 轮询。每轮会先恢复业务库中仍为 `new` 的邮件，再拉取邮箱当前前 `MAIL_FETCH_LIMIT` 封未读邮件；在人工确认已读前，重复轮询会得到同一批未读邮件，已入库记录不会重复进入工作流。

审核列表顶部的“立即收取邮件”按钮会人工触发同一套收信流程，适合刚发出测试邮件后不想等待下一轮轮询时使用。页面分为两类视图：“待处理”及其状态筛选只显示尚未确认已读的工作队列；“已发送”是已回复邮件的专用检索列表；“处理历史”显示全部已读邮件用于审计追溯，按接收时间倒序分页展示，每页 10 条。`ignored` 列表提供“批量已读”，`notify` 邮件在详情页提供单封“标记已读”；回复经人工批准或修改发送成功后，系统会自动写入 QQ 邮箱 `\Seen` 并保留在“已发送 / 处理历史”。若发送成功但 IMAP 标记已读失败，页面会告警，邮件保留在“已发送”中等待人工补标记。

## 真机模式预检

1. 在 QQ 邮箱网页版开启 IMAP/SMTP 服务，生成授权码；授权码不是 QQ 登录密码。
2. 复制 `.env.example` 为 `.env`，填入邮箱地址、授权码和 DeepSeek API Key。
3. 如需 OpenAI 回退，将 `LLM_PROVIDER_ORDER` 改为 `deepseek,openai`，并同时补齐两个 Key。
4. 执行无发信预检：

```powershell
.venv\Scripts\python scripts\preflight.py
```

预检只做配置校验、IMAP 登录并读取一封未读样例、SMTP 登录与 NOOP、一次 LLM 结构化分类；不会发送邮件。

## 审核与数据流

- `ignore` / `notify` / `respond` 分类结果、草稿版本、人工操作和发送结果都写入本地 SQLite。
- `notify` 邮件不会外发；在详情页点击“升级为待回复”后，先生成草稿，成功才进入 `draft_pending`，失败则保持 `notified`。
- `respond` 草稿必须停在 `draft_pending`，由人在详情页批准、修改后发送、忽略、反馈重写或失败后人工重试。
- 模型节点没有 SMTP 工具；轮询和草稿生成阶段不会外发。真实 SMTP 只由网页的人工发送动作触发。
- 服务只监听 `127.0.0.1`；`.env` 与 `data/` 中的数据库都只保存在本机，不要提交或分享。
- 数据库内部时间统一使用 UTC，审核页按 `DISPLAY_TIMEZONE`（默认 `Asia/Shanghai`）显示。

## 离线演示数据

不配置真实邮箱和 LLM 时，可以启动本地演示模式：

```powershell
$env:DEMO_MODE = "true"
.venv\Scripts\python scripts\seed_demo.py --database data\mail.db
.venv\Scripts\python -m app.main
```

演示数据包含五类真实状态：

| 场景 | 主题 | 状态 |
|------|------|------|
| 营销群发邮件 | 开发者技术周刊：本周文章精选 | `ignored` |
| 模糊通知 | 关于下周值班安排的通知 | `notified` |
| 待人工审核 | 请问方案初稿什么时候能给到我？ | `draft_pending` |
| 重写后修改发送 | 需要确认部署验收时间 | `sent_done` |
| 发送失败 | 发票信息需要回复确认 | `send_failed` |

演示模式不会连接 IMAP、LLM 或 SMTP；批准/修改发送只调用本地 Fake 发送记录。

## 当前进度

- [x] M0 项目骨架与配置
- [x] M1 SQLite 存储与状态机
- [x] M2 IMAP 拉取与解析
- [x] M3 确定性规则路由
- [x] M4 LLM 适配层
- [x] M5 LangGraph 工作流与 HITL
- [x] M6 审核网页
- [x] M7 SMTP 发送与幂等
- [x] M8 端到端串联
- [ ] M9 真机验收与 P1
