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

### 跨会话共享记忆

机器人还会从对话中提取长期记忆，让不同联系人之间保留“共同认识的人”这一层关系。
例如 B 告诉机器人自己下周三去杭州，之后 C 问“小明最近要去哪”，机器人可以自然提到
这件事。

记忆分为两类：

- `shareable`：偏好、公开计划、普通关系等可与其他联系人使用的信息
- `private`：健康、财务、秘密、私人冲突等敏感信息

默认开启隐私保护，`private` 记忆只允许当事人自己查询。当前项目配置中的
`MEMORY_PRIVACY_PROTECTION=false` 表示已关闭该保护，完整共享模式会允许其他联系人
使用私密记忆建立的关联。该模式只应部署在所有参与者都知情并同意的小圈子。可以自行
管理记忆：

```text
/记忆
/删记忆 编号
/忘记我
```

共享记忆保存在本机 SQLite，默认最多取 14 条相关记忆加入提示词。它不同于每个会话
最近 40 条短期上下文：短期上下文用于连续聊天，长期记忆用于跨联系人和跨会话回忆。

除提取后的事实外，系统还保存最近其他会话的简短对话片段，包括用户原话和机器人当时
的回复。因此在另一个群提到前一个群聊过的内容时，机器人可以回忆上下文。默认最多
加入 8 个跨会话片段，不会把完整聊天记录一次性塞进提示词。

```dotenv
SHARED_MEMORY_ENABLED=true
MEMORY_MODEL=qwen3.8-flash
MEMORY_CONTEXT_LIMIT=14
MEMORY_PRIVACY_PROTECTION=false
```

### 反思与成长

开启 `REFLECTION_ENABLED` 后，机器人会在后台定期检查新记忆，寻找人物、主题、计划和
事件之间的关联。它只保存整理后的观察和成长笔记，不保存或展示隐藏思维过程。

默认情况下，每 30 分钟检查一次；积累至少 5 条新记忆后才进行反思。反思结果带有
置信度。关闭隐私保护时，私密材料也可以形成其他联系人可见的关联和成长笔记。

```dotenv
REFLECTION_ENABLED=true
REFLECTION_MODEL=qwen-plus
REFLECTION_INTERVAL_SECONDS=1800
REFLECTION_MIN_NEW_MEMORIES=5
```

可以使用 `/成长` 查看机器人整理出的可共享成长笔记。这是对连续记忆和关系理解的
模拟，不代表机器人具有意识；价值观、隐私边界和基本人格不会被自动改写。

需要临时关闭“必须 @”限制时，双击 [`群聊免艾特.cmd`](群聊免艾特.cmd)。脚本会自动
修改配置并重启 Python 机器人，不会关闭 NapCat 或主 QQ。开启后机器人会对所有群消息
进行回复，Token 消耗和刷屏风险会明显增加。

需要恢复“只有 @ 才回复”时，双击 [`群聊必须艾特.cmd`](群聊必须艾特.cmd)。该脚本
会清空无 @ 前缀触发规则，只响应 @ 机器人的群消息。

机器人只按需引用消息，不会每次回复都引用。普通文字聊天和命令回复保持无引用；
用户主动引用消息、发送图片、文件或语音时，机器人回复才会引用对应原消息。在
“必须 @”模式下，直接引用机器人的回复继续追问，不需要再次 @；引用其他群成员的
消息仍不会被当成对机器人说话。

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

QQ 商城表情和表情包不会当作题目图片。带 `emoji_id`、商城表情 key 或 `mface`
标记的消息按聊天表情处理，普通截图才进入图片理解模型。

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

### 语音识别

直接发送 QQ 语音，机器人会通过 NapCat 转为 WAV，再交给
`qwen3.8-omni-flash` 识别。默认只输出转写结果，也可以附加要求：

```text
[发送语音] 请完整转写
[发送语音] 总结这段录音
[发送语音] 回答语音里提出的问题
```

也支持 `/转写`、`/听写`、`/总结` 和 `/回答` 命令。语音识别只返回文字，不会合成
语音回复。

### 联网搜索与时事

机器人会在当前日期时间基础上处理时效性问题。检测到新闻、天气、比赛、比分、赛程、
转会、伤病、价格、政策、热搜或“最新/实时”等需求时，会自动启用千问联网搜索。
也可以明确要求：

```text
/搜 今天有哪些重要足球新闻
/搜 杭州明天天气
```

搜索结果需要说明信息日期和来源，不能把旧知识伪装成实时消息。普通闲聊不会触发搜索。

```dotenv
WEB_SEARCH_ENABLED=true
```

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
/戳
```

`/戳` 会戳当前聊天对象；在群里会戳发送指令的人。私聊也可以直接说“戳一戳”。

笔记、待办和提醒按 QQ 用户隔离。机器人运行时每 15 秒检查一次提醒；如果到点时
机器人离线，重启后仍会补发尚未发送的提醒。修改时区可在 `.env` 设置：

```dotenv
TIMEZONE=Asia/Shanghai
REMINDER_POLL_SECONDS=15
```

## 常用配置

| 配置 | 默认值 | 作用 |
|---|---|---|
| `BOT_NAME` | 给她爱 | 机器人身份名 |
| `PERSONA_PROMPT_PATH` | prompts/gexiai.md | 完整人格提示词文件 |
| `BOT_PERSONA` | 足球迷、嘴毒但不脏 | 身份、偏好和说话风格 |
| `CASUAL_MODEL` | qwen3.8-flash | 普通闲聊使用的轻量模型 |
| `WEB_SEARCH_ENABLED` | true | 为时事问题启用千问联网搜索 |
| `LLM_TRUST_ENV` | false | 是否让模型请求使用系统代理 |
| `HISTORY_MESSAGES` | 40 | 每次带给模型的最近消息数 |
| `VISION_MODEL` | qwen3-vl-plus | 图片理解与题目识别模型 |
| `MAX_FILES_PER_MESSAGE` | 3 | 单条消息最多处理的文件数 |
| `AUDIO_MODEL` | qwen3.8-omni-flash | 语音转写与理解模型 |
| `SHARED_MEMORY_ENABLED` | true | 启用跨会话长期记忆 |
| `MEMORY_MODEL` | qwen3.8-flash | 提取长期记忆使用的模型 |
| `MEMORY_CONTEXT_LIMIT` | 14 | 每次最多加入多少条共享记忆 |
| `MEMORY_PRIVACY_PROTECTION` | false | 是否阻止私密记忆跨联系人使用 |
| `REFLECTION_MODEL` | qwen-plus | 后台关联与反思使用的模型 |
| `REFLECTION_INTERVAL_SECONDS` | 1800 | 后台反思间隔 |
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
