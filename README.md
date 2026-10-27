# QQ 小号智能助手

这是一个把 QQ 个人号接成 AI 聊天机器人的本地项目。NapCat 负责 QQ 登录与
OneBot v11 协议，Python 服务负责上下文、限流、管理命令和大模型请求。

> 风险提示：用个人 QQ 号自动收发消息不属于 QQ 官方机器人接入方式，可能触发
> 风控。建议只在备用小号上使用，不要批量加人、主动营销或高频群发。需要长期稳定
> 运营时，应改用 [QQ 开放平台](https://q.qq.com/) 的官方机器人。

## 架构

```text
QQ 小号 <-> NapCat
             | HTTP Client 推送消息 -> http://127.0.0.1:8080/onebot
             | HTTP Server 接收回复 <- http://127.0.0.1:3000
                         |
                    Python Bot
                 /      |      \
           SQLite上下文 限流  AI接口
```

模型接口使用 OpenAI 兼容格式，默认配置为阿里云百炼的 `qwen-plus`。也可以改为
DeepSeek、OpenAI、硅基流动，或本地 Ollama 的 `http://127.0.0.1:11434/v1`。

## 开始使用

### 1. 安装并登录 NapCat

从 [NapCat Releases](https://github.com/NapNeko/NapCatQQ/releases/latest) 下载
Windows Shell 或 OneKey 版本，按官方启动流程登录你的 QQ 小号。登录时需要用手机
扫码；不要把账号密码交给项目或配置文件。

如果电脑上已经登录了主 QQ，使用 Shell 版前先退出普通 QQ 客户端，避免两个客户端
争用同一份账号数据。无法退出时优先使用 OneKey 的独立安装目录。

打开 NapCat WebUI，通常地址是 `http://127.0.0.1:6099/webui`。登录密码在首次启动
日志中。

### 2. 配置两个 OneBot 网络

在 NapCat WebUI 的“网络配置”中新增：

1. **HTTP 服务端**：监听 `127.0.0.1:3000`，消息格式设为 `array`，设置 Token。
2. **HTTP 客户端**：上报地址 `http://127.0.0.1:8080/onebot`，消息格式设为
   `array`，关闭自身消息上报，并填写同一个 Token。

两处都要勾选“保存时启用”或保存后手动启用。也可以参考
[`config/onebot11.example.json`](config/onebot11.example.json)，但 WebUI 配置更直观。

### 3. 配置机器人

以后每次打开电脑，双击项目目录中的 [`启动机器人.cmd`](启动机器人.cmd) 即可。
它会自动检查配置、启动 NapCat、等待小号上线并启动机器人。启动成功后命令窗口可以
关闭，机器人在后台运行。

需要关闭时双击 [`停止机器人.cmd`](停止机器人.cmd)。它只停止本项目启动的机器人
和便携 NapCat，不会关闭你日常使用的主 QQ：

也可以在 PowerShell 中运行：

```powershell
cd E:\Vibe\qq-smart-bot
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

登录态有效时会直接后台启动。若 QQ 要求重新确认登录，脚本会打开 NapCat WebUI，
用小号手机扫码后会自动继续。首次运行会创建 `.env`、随机 `ONEBOT_TOKEN` 并安装
Python 依赖。

如果之后忘记了 Token，可以在项目目录查看：

```powershell
(Get-Content .env -Encoding UTF8 | Select-String "^ONEBOT_TOKEN=").Line
```

千问 API Key 可以在本地隐藏输入并写入 `.env`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\set-qwen-key.ps1
```

也可以直接编辑 `.env`：

```dotenv
LLM_API_KEY=你的模型服务密钥
```

默认千问配置为：

```dotenv
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
```

或者使用本地 Ollama：

```dotenv
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=
```

### 4. 测试

用你的主号私聊小号：

```text
/ping
/id
/帮助
你好，介绍一下你自己
```

`/id` 会显示 QQ 号。想限制私聊访问时，把主号与自己的 QQ 号填进 `.env`：

```dotenv
PRIVATE_ALLOWLIST=123456789,987654321
```

空值表示允许任何私聊。群聊默认关闭；需要开启时设置：

```dotenv
ENABLE_GROUP=true
GROUP_ALLOWLIST=群号1,群号2
GROUP_REQUIRE_MENTION=true
```

群里必须 @机器人，或以 `/ai`、`/问` 开头才会回复。群聊上下文按“群 + 用户”隔离，
不同人的对话不会混在一起。

### 图片和题目

直接发送题目截图即可。纯文字消息使用 `qwen-plus`，带图片时自动切换为
`qwen3-vl-plus`，可以读取题干、公式、图表和选项，并给出解题步骤。

```text
[发送一道数学题图片]
请给出详细步骤
```

默认最多处理 3 张图片，不限制单张图片大小：

```dotenv
VISION_ENABLED=true
VISION_MODEL=qwen3-vl-plus
MAX_IMAGES_PER_MESSAGE=3
MAX_IMAGE_BYTES=0
```

`MAX_IMAGE_BYTES=0` 表示不设机器人侧的大小限制。阿里云接口、QQ 传输和本机内存
仍存在客观上限，超大图片可能由上游拒绝。

图片中的文字必须足够清晰；几何图、手写公式或复杂表格如果无法辨认，机器人会说明
信息不足，而不是凭空猜测。

### 文件解析与总结

私聊发送文件，并附上一句要求，例如：

```text
[发送 PDF] 请总结这份资料
[发送 Word 文档] /总结 提炼行动项和风险
[发送 Excel] 请分析表格中的任务完成情况
[发送 PPT] 帮我整理汇报要点
```

支持文字型 PDF、`.docx`、`.pptx`、`.xlsx`、CSV、TXT、Markdown 和常见代码文本
文件。长文档会自动分段提炼，再做最终汇总，不会只读取开头。一次默认最多处理
3 个文件：

```dotenv
MAX_FILES_PER_MESSAGE=3
```

扫描版 PDF 目前无法直接提取文字，可以改发页面截图。旧格式 `.doc`、`.ppt`、
`.xls` 建议先另存为新版格式。文件内容会发送给千问用于总结；机器人本地只保存
总结和文件名占位，不长期保存原始文件内容。

### 学习与工作工具

日常内容保存在同一个本地数据库中，私聊使用最方便：

```text
/记 线代复习 | 行列式交换两行会变号
/笔记
/查 行列式
/待办 周五前完成实验报告
/待办
/完成 1
/提醒 2026-10-28 09:00 复习第三章
/提醒
/取消提醒 1
```

还提供直接交给千问处理的固定入口：

```text
/计划 两周内复习完线性代数
/总结 [粘贴会议记录或学习材料]
/出题 线性代数第三章，出 5 道题
/翻译 [需要翻译的内容]
/润色 [需要修改的文字]
/解释 特征值和特征向量
```

笔记、待办和提醒按 QQ 用户隔离。机器人运行时每 15 秒检查一次提醒；如果到点时
机器人离线，重启后仍会补发尚未发送的提醒。修改时区可在 `.env` 设置：

```dotenv
TIMEZONE=Asia/Shanghai
REMINDER_POLL_SECONDS=15
```

## 常用配置

| 配置 | 默认值 | 作用 |
|---|---|---|
| `BOT_NAME` | 小号助手 | 机器人身份名 |
| `BOT_PERSONA` | 友好、自然、简洁 | 说话风格 |
| `HISTORY_MESSAGES` | 40 | 每次带给模型的最近消息数 |
| `VISION_MODEL` | qwen3-vl-plus | 图片理解与题目识别模型 |
| `MAX_FILES_PER_MESSAGE` | 3 | 单条消息最多处理的文件数 |
| `TIMEZONE` | Asia/Shanghai | 提醒使用的时区 |
| `REMINDER_POLL_SECONDS` | 15 | 检查到期提醒的间隔 |
| `RATE_LIMIT_PER_MINUTE` | 8 | 每个 QQ 用户的 AI 请求上限 |
| `OWNER_QQ` | 空 | 允许使用 `/状态` 的管理员 QQ |
| `DATABASE_PATH` | `data/bot.db` | 本地聊天记录位置 |

修改 `.env` 后重启机器人。模型密钥不会发送到 QQ 协议端，聊天内容会发送给你选择
的模型服务。聊天记录只保存在本机 SQLite 文件中，`/重置` 可清空当前会话。

机器人不限制回复总长度，也不会主动截断模型输出。长回复会按 QQ 的传输需要自动拆成
多段发送；千问模型自身的最大输出 Token 数和 QQ 平台限制仍然适用。

## 验证

```powershell
uv sync --dev
uv run pytest
uv run python scripts\check_llm.py
```

检查 Python 服务是否在线：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/healthz
```

## 常见问题

- **Webhook 返回 401**：`.env` 里的 `ONEBOT_TOKEN` 与 NapCat 两个网络配置里的
  Token 不一致，或 HTTP 客户端没有填写 Token。
- **能收到消息但不回复**：检查 NapCat HTTP 服务端是否在 `127.0.0.1:3000`；
  群里还要确认已经 @机器人。
- **模型鉴权失败**：运行 `uv run python scripts\check_llm.py`，检查 Base URL 是否
  包含正确的 `/v1`，以及密钥是否有效。
- **QQ 掉线或无法登录**：这是 NapCat/NTQQ 与账号环境相关的问题，先按 NapCat
  启动日志和官方文档排查。
