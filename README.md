# 企业设备售后 Agent

一个面向售后人员的、以证据为门禁的故障排查作品集项目。系统先确认设备型号和故障码，再读取可追溯资料；证据不足时明确转人工，不把猜测写成维修结论。

中文大白话：它不是“问一句就让模型随便答”的聊天 Demo（演示程序），而是把“资料依据、外部业务信息、回答步骤和审计记录”拆开验证的小型企业闭环。
## 新手快速开始

1. 安装并启动 Docker Desktop。
2. 克隆仓库并进入项目目录。
3. 执行 `docker compose --env-file .env.docker up --build -d`。
4. 打开 <http://localhost:8000/docs>，用浏览器操作接口。
5. 按照 [新手运行教程](BEGINNER_GUIDE_CN.md) 完成第一次请求和验证。

## 已验证范围与边界

| 能力 | 已验证事实 | 证据 | 不能据此声称什么 |
| --- | --- | --- | --- |
| API（应用接口） | FastAPI 公开会话入口会执行追问、夹具检索、证据门禁、模拟业务 API、引用绑定回答和 PostgreSQL 审计 | `tests/test_api_contract.py`、`tests/test_public_workflow.py`、`scripts/verify_docker_demo.py` | 默认 Docker 演示使用公开合成资料夹具，不是本地 RAGFlow 或生产入口 |
| 证据门禁 | 固定资料候选必须重新读取当前正文，并校验 SHA-256（文件指纹）与页码/章节，才能绑定回答引用 | `tests/test_evidence_registry.py`、`tests/test_ragflow_workflow_integration.py` | `Hit@5`（前 5 候选命中）不是最终答案语义正确率 |
| OCR（图片文字识别） | 合成控制面板图片通过 RapidOCR 提取 `E-200`、`E01`、`3.1.4` | `tests/test_ocr_pipeline.py` | 不代表真实企业扫描件或生产 OCR 准确率 |
| 模拟业务 API | 独立 FastAPI Mock（模拟接口）经过身份校验后才提供设备、故障码和库存上下文 | `tests/test_business_api.py` | 不代表真实企业业务系统已接入 |
| PostgreSQL（关系数据库） | Alembic（数据库迁移工具）定义 8 张审计相关表；事务、回滚和脱敏由隔离集成测试验证 | `tests/test_database_audit.py` | 固定评测不执行数据库持久化，不能报告数据库耗时 |
| 固定评测 | 版本化题集含 20 题，覆盖当前资料引用、追问、无证据、OCR、库存和系统状态；自动结果与人工语义复核分开记录 | `data/evaluation/c6_fixed_questions.v1.json`、`scripts/run_c6_evaluation.py` | 默认运行的人工语义复核状态为 `pending_human_review`（等待人工复核）；本地 `FakeSupportAnswerGenerator`（模拟回答生成器）不是 LLM（大模型） |
| 工作流编排 | 已实现严格状态模型、节点输入输出约束和分支处理，并由 `SupportWorkflowExecutor` 串联执行 | `app/agent/state.py`、`app/agent/nodes.py`、`app/agent/workflow.py` | 当前没有安装或调用 LangGraph 的 `StateGraph`（状态图）等真实图编排 API（接口） |
| Docker Compose（多容器编排） | 本项目提供 API、模拟业务 API、PostgreSQL、健康检查、迁移和持久化卷的可重复交付配置 | `compose.yaml`、`scripts/verify_docker_demo.py` | Docker 运行验证结果必须以本次实际命令输出为准 |

## 环境要求

- Docker Desktop（Docker 桌面容器运行环境）已启动，并支持 `docker compose`。
- 本地开发使用 Python 3.11+ 与 `uv`（Python 依赖锁定工具）。容器镜像固定使用 Python 3.12 和 `uv.lock`（锁文件）。
- 项目自带 `.env.docker`，其中仅含隔离本地卷的演示数据库密码；不含真实 RAGFlow、LLM 或企业系统密钥。

## 一条启动命令

在项目根目录执行：

```powershell
docker compose --env-file .env.docker up --build -d
```

这会构建同一应用镜像，启动 `db`（PostgreSQL）、`business-api`（模拟业务接口）和 `api`（主 API）。主 API 会在启动前执行 `alembic upgrade head`（把数据库结构升级到当前版本）；Compose 只会在数据库和模拟业务接口健康后启动主 API。

查看服务状态：

```powershell
docker compose --env-file .env.docker ps
```

主 API 健康检查地址：`http://localhost:8000/api/v1/health`。

## 一条验证命令

等待 `api` 显示为 `healthy`（健康）后执行：

```powershell
docker compose --env-file .env.docker --profile verify run --rm verifier
```

验证容器会检查：主 API 健康状态、模拟业务 API 健康状态和固定设备数据、主 API 返回的带来源引用回答，以及 PostgreSQL 中由迁移创建的审计表。成功输出包含 `Docker validation passed`（中文意思：Docker 验证通过）。

中文大白话：启动命令只说明容器被拉起来；验证命令再确认三个服务真的能互相访问，数据库也确实有迁移后的表。

停止并保留数据库卷：

```powershell
docker compose --env-file .env.docker down
```

仅在确认不再需要本地演示数据时，删除卷并回到全新数据库：

```powershell
docker compose --env-file .env.docker down -v
```

## 本地开发、测试与评测

安装锁定依赖：

```powershell
uv sync --locked --dev
```

运行全量测试：

```powershell
uv run --locked pytest -q
```

运行格式、静态检查和类型检查：

```powershell
uv run --locked ruff format --check app tests scripts
uv run --locked ruff check app tests scripts
uv run --locked mypy app tests scripts/run_c6_evaluation.py scripts/verify_docker_demo.py
uv lock --check
```

在全新目录运行 20 题固定评测：

```powershell
uv run --locked python scripts/run_c6_evaluation.py --output data/evaluation/results/c6_latest.json
```

该命令会把 `semantic_answer_review`（最终回答语义复核）标为 `pending_human_review`（等待人工复核），这是正确的：人工复核记录不能由程序伪造，也不应作为干净环境的隐藏输入。

如果你已经准备好独立的人工复核 JSON（结构化文本）记录，才额外运行：

```powershell
uv run --locked python scripts/run_c6_evaluation.py --review data/evaluation/results/c6_manual_semantic_review.json --output data/evaluation/results/c6_reviewed.json
```

结果文件位于被 Git 忽略的 `data/evaluation/results/`，因为它含运行时间与复核人本地记录；固定题集、夹具清单和复核模板可以被版本管理。质量指标必须分层阅读：候选覆盖、当前引用校验、人工语义复核不是同一个“准确率”。

## 配置

| 配置 | 用途 | Docker 默认值 |
| --- | --- | --- |
| `ESA_DATABASE_URL` | PostgreSQL 审计库连接地址 | 指向 Compose 中的 `db` 服务 |
| `ESA_BUSINESS_API_BASE_URL` | 模拟业务 API 地址 | `http://business-api:8001` |
| `ESA_RAGFLOW_ENABLED` | 是否启用真实 RAGFlow（知识库服务） | `false`（关闭） |
| `ESA_RAGFLOW_BASE_URL` / `ESA_RAGFLOW_API_KEY` / `ESA_RAGFLOW_DATASET_ID` | 真实 RAGFlow 的必填配置 | 不提供；启用时必须由运行环境显式注入 |
| `ESA_LLM_API_KEY` | 未来接入真实 LLM 的配置预留 | 不提供；当前代码路径不调用真实 LLM，固定评测使用模拟回答生成器 |

不要把真实 `.env`、API Key（接口密钥）、客户资料或运行结果提交到版本库。`.dockerignore`（Docker 构建忽略清单）也会阻止它们进入镜像构建上下文。

## 故障排查

| 现象 | 检查方式 | 处理 |
| --- | --- | --- |
| Docker 守护进程未启动 | `docker version` 报连接错误 | 启动 Docker Desktop，等 Linux 容器引擎就绪后重试 |
| `api` 不健康 | `docker compose --env-file .env.docker logs api` | 先确认 `db` 和 `business-api` 都是 `healthy`；再查看 Alembic 迁移错误 |
| PostgreSQL 端口或旧卷冲突 | `docker compose --env-file .env.docker ps` | 停止同名项目；只有确认数据可删除时才运行 `down -v` |
| 验证器提示缺少审计表 | `docker compose --env-file .env.docker logs api` | 主 API 迁移失败时不会健康；修复迁移后重新 `up --build -d` |
| 真实 RAGFlow 配置校验失败 | 启动日志会列出缺失的 `ESA_RAGFLOW_*` 名称 | 保持 `ESA_RAGFLOW_ENABLED=false`，或完整、安全地注入三项真实配置 |
| OCR 运行较慢 | 查看固定评测结果中的 `metrics.latency_ms.ocr` | RapidOCR 是本地真实 OCR；该耗时只代表本机固定合成样本，不是生产性能指标 |
