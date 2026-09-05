# 新手运行教程

这份教程面向第一次拿到项目的人。目标是让你启动项目、打开可视化接口页面，并完成一次设备故障排查请求。

## 先理解项目是什么

这是一个企业设备售后问答后端。它会先确认设备型号和故障码，再从项目自带的公开合成资料中寻找依据；有依据才返回带引用的排障步骤，资料不足时会追问或转人工。

当前公开演示默认使用：

- Docker Compose 启动 API、PostgreSQL 和模拟业务 API。
- 项目自带的合成设备资料，不需要真实企业资料。
- 固定回答生成器，不调用真实大模型。
- 本地资料夹具，不默认启动本地 RAGFlow。

因此，这是一个可以重复运行的本地演示，不是已经接入真实企业系统的线上平台。

## 需要安装什么

推荐准备：

1. Git：下载项目代码。
2. Docker Desktop：运行多个容器。
3. Windows PowerShell：执行命令。

安装 Docker Desktop 后打开它，等待 Docker 引擎正常运行。

在 PowerShell 中检查：

```powershell
docker version
docker compose version
```

两个命令都能显示版本号，说明 Docker 基本可用。

## 下载项目

```powershell
git clone https://github.com/dashstar22/enterprise-support-agent.git
cd enterprise-support-agent
```

`git clone` 的意思是把 GitHub 上的项目下载到当前电脑。

后面的命令都要在项目根目录执行，也就是能看到 `compose.yaml` 和 `README.md` 的目录。

## 推荐方式：Docker 启动

### 1. 启动服务

```powershell
docker compose --env-file .env.docker up --build -d
```

这条命令会启动：

- `api`：主 FastAPI 接口服务。
- `db`：PostgreSQL 数据库。
- `business-api`：模拟第三方业务接口。

第一次运行需要下载镜像和依赖，可能需要几分钟。

### 2. 查看服务状态

```powershell
docker compose --env-file .env.docker ps
```

等待 `api`、`db` 和 `business-api` 的状态正常，主 API 最终应显示为 `healthy`。`healthy` 的意思是健康检查通过，可以接受请求。

也可以直接检查主 API：

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/health
```

正常时会返回服务状态、服务名和运行环境。

### 3. 运行完整验证

```powershell
docker compose --env-file .env.docker --profile verify run --rm verifier
```

验证器会检查主 API、模拟业务 API、固定设备资料、带引用回答和 PostgreSQL 数据库迁移。

成功输出是：

```text
Docker validation passed: API, mock business API, cited fixture workflow, and audit migration.
```

中文意思是：Docker 验证通过，主 API、模拟业务接口、带引用的资料流程和数据库迁移都正常。

## 用浏览器操作：最适合新手

项目没有单独开发聊天网页，但 FastAPI 会自动提供一个可视化接口调试页面。

打开：

<http://localhost:8000/docs>

这个页面叫 Swagger UI，是自动生成的接口操作页面。你可以展开接口、点击 `Try it out`（开始试用）、填写 JSON（结构化请求数据）并执行，不需要先写 PowerShell。

### 创建会话

找到：

```text
POST /api/v1/sessions
```

点击 `Try it out`，请求内容填写：

```json
{
  "device_model": "E-200"
}
```

点击 `Execute`（执行）。返回结果中会有一个 `session_id`，它是本次对话的编号。

### 提交故障问题

找到：

```text
POST /api/v1/sessions/{session_id}/messages
```

点击 `Try it out`，把返回的 `session_id` 填入路径参数，然后填写：

```json
{
  "message": "设备 E-200 报 E01，无法启动，应该怎么排查？",
  "fault_code": "E01"
}
```

点击 `Execute` 后，应该看到 `status` 为 `completed`，并看到 `answer`、`steps` 和 `citation`。

其中：

- `answer`：结构化回答。
- `steps`：排障步骤。
- `citation`：资料引用。
- `source_name`：引用的资料文件名。
- `page` 或 `section`：资料中的页码或章节。

## 用 PowerShell 操作：适合复现

浏览器方式更直观，PowerShell 方式更容易复制、记录和重复运行。

### 创建会话

```powershell
$session = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/v1/sessions `
  -ContentType "application/json" `
  -Body '{"device_model":"E-200"}'

$session
```

### 提交故障问题

```powershell
$messageBody = @{
  message = "设备 E-200 报 E01，无法启动，应该怎么排查？"
  fault_code = "E01"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/sessions/$($session.session_id)/messages" `
  -ContentType "application/json" `
  -Body $messageBody
```

### 体验缺少信息时追问

创建一个不带设备型号的会话：

```powershell
$session = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/api/v1/sessions `
  -ContentType "application/json" `
  -Body '{}'
```

提交只有故障码的问题：

```powershell
$messageBody = @{
  message = "设备报 E01，怎么处理？"
  fault_code = "E01"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/sessions/$($session.session_id)/messages" `
  -ContentType "application/json" `
  -Body $messageBody
```

这次应该返回：

```text
status: needs_clarification
missing_fields: device_model
```

意思是系统缺少设备型号，因此先追问，不直接猜测故障原因。

## 查看日志

```powershell
docker compose --env-file .env.docker logs -f api
```

查看模拟业务 API：

```powershell
docker compose --env-file .env.docker logs -f business-api
```

按 `Ctrl+C` 停止查看日志。

日志中可以看到请求编号、追踪编号、组件耗时和错误码。数据库中还会保存工作流审计记录。

## 运行自动化测试

如果已经安装 Python 3.11 以上版本和 `uv`（Python 依赖管理工具），可以在项目根目录执行：

```powershell
uv sync --locked --dev
uv run --locked pytest -q
```

测试会检查 API、状态分支、OCR、证据校验、模拟业务 API、数据库审计和固定评测。

## 停止项目

停止容器但保留数据库卷：

```powershell
docker compose --env-file .env.docker down
```

停止容器并删除本地演示数据库：

```powershell
docker compose --env-file .env.docker down -v
```

只有确认不再需要之前的演示数据时，才使用 `down -v`。

## 常见问题

### Docker 构建时无法访问 ghcr.io

如果错误中出现：

```text
ghcr.io/astral-sh/uv
```

通常是 Docker Desktop 无法访问 GitHub 容器镜像仓库，属于网络或代理问题，不一定是项目代码问题。可以重试，或检查 Docker Desktop 的代理设置。

### api 一直不是 healthy

查看日志：

```powershell
docker compose --env-file .env.docker logs api
```

同时确认 `db` 和 `business-api` 已经健康。主 API 启动时会先执行数据库迁移，如果数据库还没有准备好，启动脚本会有限次数重试。

### 为什么没有真实大模型回答

当前公开 Demo 使用固定回答生成器，重点是验证字段追问、证据门禁、引用绑定、业务接口和安全失败分支。它不需要真实模型密钥，也不会产生模型调用费用。

### 为什么没有直接启动 RAGFlow

真实 RAGFlow 是可选外部服务，需要服务地址、访问密钥和知识库编号。公开 Demo 默认使用项目自带的固定合成资料，保证新用户不需要先搭建外部知识库。

### 如何切换到真实大模型

真实 LLM（大语言模型）已经接入回答生成这一层，但默认开关仍是关闭的。复制 `.env.example` 到本地 `.env` 后，填写 `ESA_LLM_ENABLED=true`、`ESA_LLM_BASE_URL`、`ESA_LLM_MODEL` 和 `ESA_LLM_API_KEY`，再重启 API。这里的 `BASE_URL` 是兼容 OpenAI 的服务地址，程序会请求它的 `/chat/completions` 接口。

模型返回内容不会直接展示：程序先解析 JSON，再检查 `SupportAnswer`（回答结构）和引用是否来自当前证据。超时、鉴权失败、限流和格式错误都会变成受控错误。自动化测试继续使用 `FakeSupportAnswerGenerator`（固定模拟生成器），因此测试不需要联网或密钥。

## 当前边界

运行成功后，你看到的是一个可重复的本地后端演示。它当前不代表：

流程编排已经接入真实 LangGraph 图执行；默认回答仍使用固定生成器，真实 LLM 仅在显式配置后启用，当前仓库没有宣称已经完成某个云模型的线上调用评测。

- 已接入真实企业业务系统。
- 已调用真实大模型。
- 已默认启动本地 RAGFlow。
- 已完成云端部署或生产性能验证。
