from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .audio import AudioLoader, AudioLoaderProtocol, AudioLoadError
from .config import Settings
from .files import (
    DocumentLoader,
    FileParseError,
    FileParserProtocol,
    ParsedDocument,
    summarize_documents,
)
from .images import ImageLoader, ImageLoaderProtocol, ImageLoadError
from .llm import ChatClient, CompatibleChatClient, LLMError
from .memory import ChatMemory
from .onebot import (
    OneBotClient,
    OneBotError,
    OneBotEvent,
    AudioSource,
    FileSource,
    ReplyTarget,
    extract_message_content,
    split_reply,
    strip_trigger_prefix,
)
from .productivity import (
    ProductivityStore,
    ReminderTimeError,
    format_utc_for_zone,
    parse_reminder_time,
    split_reminder_argument,
)
from .qq_format import format_qq_text
from .ratelimit import SlidingWindowRateLimiter
from .security import request_is_authorized

logger = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1_048_576
QQ_TRANSPORT_CHUNK_CHARS = 1500
AI_COMMAND_PROMPTS = {
    "/计划": "请把下面的学习或工作目标整理成可执行计划，包含优先级、时间安排和检查点：",
    "/总结": "请用清晰的要点总结下面的内容，并列出关键结论：",
    "/出题": "请根据下面的主题或材料出题，并附上答案；优先出能检查理解的题目：",
    "/翻译": "请把下面的内容翻译成表达自然的中文；如果原文是中文，则翻译成英文：",
    "/润色": "请在不改变事实和含义的前提下润色下面的文字，使其清晰、专业、自然：",
    "/解释": "请用容易理解的方式解释下面的概念，给出例子，并指出常见误区：",
}
TASK_HINTS = (
    "总结",
    "计划",
    "步骤",
    "翻译",
    "润色",
    "解释",
    "分析",
    "代码",
    "报错",
    "怎么",
    "如何",
    "为什么",
    "区别",
    "证明",
    "计算",
    "列出",
    "写一篇",
    "方案",
)


def create_app(
    *,
    settings: Settings | None = None,
    chat_client: ChatClient | None = None,
    onebot_client: OneBotClient | None = None,
    memory: ChatMemory | None = None,
    image_loader: ImageLoaderProtocol | None = None,
    productivity_store: ProductivityStore | None = None,
    document_loader: FileParserProtocol | None = None,
    audio_loader: AudioLoaderProtocol | None = None,
) -> FastAPI:
    config = settings or Settings()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    llm = chat_client or CompatibleChatClient(
        base_url=config.llm_base_url,
        model=config.llm_model,
        api_key=config.effective_llm_api_key,
        temperature=config.llm_temperature,
        timeout_seconds=config.llm_timeout_seconds,
    )
    sender = onebot_client or OneBotClient(
        base_url=config.onebot_api_base,
        token=config.onebot_token.get_secret_value(),
    )
    store = memory or ChatMemory(config.database_path)
    images = image_loader or ImageLoader(
        max_bytes=config.max_image_bytes,
        timeout_seconds=config.image_timeout_seconds,
    )
    productivity = productivity_store or ProductivityStore(config.database_path)
    documents = document_loader or DocumentLoader(
        timeout_seconds=config.document_timeout_seconds
    )
    audio = audio_loader or AudioLoader(
        timeout_seconds=config.audio_timeout_seconds
    )

    limiter = SlidingWindowRateLimiter(config.rate_limit_per_minute, 60.0)
    semaphore = asyncio.Semaphore(max(1, config.max_concurrent_requests))
    conversation_locks: dict[str, asyncio.Lock] = {}
    background_tasks: set[asyncio.Task[Any]] = set()
    seen_message_ids: OrderedDict[str, None] = OrderedDict()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        reminder_task = asyncio.create_task(_reminder_loop())
        try:
            yield
        finally:
            reminder_task.cancel()
            pending = [*background_tasks, reminder_task]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await sender.close()
            await llm.close()
            await images.close()
            await documents.close()
            await audio.close()

    app = FastAPI(
        title="QQ Smart Bot",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = config
    app.state.memory = store

    def get_conversation_lock(conversation_id: str) -> asyncio.Lock:
        lock = conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            conversation_locks[conversation_id] = lock
        return lock

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "model": config.llm_model,
            "casual_model": config.casual_model,
            "vision_enabled": config.vision_enabled,
            "vision_model": config.vision_model,
            "max_image_bytes": config.max_image_bytes,
            "max_files_per_message": config.max_files_per_message,
            "audio_enabled": config.audio_enabled,
            "audio_model": config.audio_model,
            "group_enabled": config.enable_group,
            "group_require_mention": config.group_require_mention,
            "quote_replies": config.quote_replies,
            "group_quote_counts_as_mention": config.group_quote_counts_as_mention,
            "history_messages": config.history_messages,
            "max_history_rows_per_chat": config.max_history_rows_per_chat,
            **store.stats(),
        }

    @app.post("/onebot")
    async def receive_onebot_event(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            return JSONResponse({"detail": "event too large"}, status_code=413)

        token = config.onebot_token.get_secret_value()
        if not request_is_authorized(
            token=token,
            headers=request.headers,
            body=body,
            client_host=request.client.host if request.client else None,
        ):
            logger.warning("Rejected an unauthorized OneBot webhook")
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        try:
            payload = await request.json()
            event = OneBotEvent.model_validate(payload)
        except (ValueError, ValidationError):
            return JSONResponse({"detail": "invalid event"}, status_code=400)

        if event.post_type != "message" or event.message_type not in {
            "private",
            "group",
        }:
            return Response(status_code=204)
        if event.user_id is None:
            return Response(status_code=204)
        if event.self_id is not None and event.user_id == event.self_id:
            return Response(status_code=204)

        dedupe_key = (
            (
                f"{event.message_type}:{event.group_id or 0}:"
                f"{event.user_id}:{event.message_id}"
            )
            if event.message_id is not None
            else ""
        )
        if dedupe_key:
            if dedupe_key in seen_message_ids:
                return Response(status_code=204)
            seen_message_ids[dedupe_key] = None
            while len(seen_message_ids) > 2048:
                seen_message_ids.popitem(last=False)

        task = asyncio.create_task(_run_event(event))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return Response(status_code=204)

    async def _run_event(event: OneBotEvent) -> None:
        try:
            await _process_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected message-processing failure")

    async def _reminder_loop() -> None:
        await asyncio.sleep(1)
        while True:
            try:
                for reminder in productivity.due_reminders():
                    target = ReplyTarget(
                        reminder.message_type,
                        reminder.user_id,
                        reminder.group_id,
                    )
                    if await _safe_send(target, f"提醒：{reminder.content}"):
                        productivity.mark_reminder_delivered(reminder.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reminder delivery loop failed")
            await asyncio.sleep(max(1.0, config.reminder_poll_seconds))

    async def _process_event(event: OneBotEvent) -> None:
        assert event.user_id is not None
        if event.message_type == "private":
            if config.private_user_ids and event.user_id not in config.private_user_ids:
                logger.info("Ignored private message outside allowlist")
                return
            target = ReplyTarget(
                "private",
                event.user_id,
            )
        elif event.message_type == "group":
            if not config.enable_group or event.group_id is None:
                return
            if config.group_ids and event.group_id not in config.group_ids:
                logger.info("Ignored group outside allowlist")
                return
            target = ReplyTarget(
                "group",
                event.user_id,
                event.group_id,
            )
        else:
            return

        content = extract_message_content(
            event.message,
            self_id=event.self_id,
            raw_message=event.raw_message,
        )
        text = re.sub(r"\[文件:[^\]]+\]", "", content.text)
        text = text.replace("[图片]", "").replace("[语音]", "").strip()
        image_sources = content.image_sources[: max(0, config.max_images_per_message)]
        file_sources = content.file_sources[: max(0, config.max_files_per_message)]
        audio_sources = content.audio_sources[:1]
        should_quote = (
            config.quote_replies
            and (
                bool(content.reply_id)
                or bool(image_sources)
                or bool(file_sources)
                or bool(audio_sources)
            )
        )
        if should_quote:
            target = replace(target, message_id=event.message_id)
        if target.message_type == "group":
            prefix_triggered = any(
                text.lower().startswith(prefix.lower())
                for prefix in config.trigger_prefixes
            )
            quoted_bot = False
            if (
                config.group_quote_counts_as_mention
                and content.reply_id
                and not content.mentioned
            ):
                quoted_bot = await _is_reply_to_bot(
                    content.reply_id,
                    event.self_id,
                )
            if (
                config.group_require_mention
                and not content.mentioned
                and not prefix_triggered
                and not quoted_bot
            ):
                return
            text = strip_trigger_prefix(text, config.trigger_prefixes)

        if not text and not image_sources and not file_sources and not audio_sources:
            text = "你好"
        if not text and image_sources:
            text = "请分析这张图片。如果是题目，请给出解题步骤和最终答案。"

        if text.strip().lower() in {"/戳", "/戳一戳", "戳一戳", "戳我"}:
            if not await _safe_poke(target):
                await _safe_send(target, "戳不动，NapCat 可能没有启用这个互动接口。")
            return

        command_reply = _handle_command(event, target, text)
        if command_reply is not None:
            await _safe_send(target, command_reply)
            return

        if audio_sources:
            if not limiter.allow(str(event.user_id)):
                logger.info("Rate limited one voice request")
                return
            await _process_audio_request(
                target=target,
                text=text,
                audio_source=audio_sources[0],
            )
            return

        if file_sources:
            if not limiter.allow(str(event.user_id)):
                logger.info("Rate limited one file request")
                return
            await _process_file_request(
                event=event,
                target=target,
                text=text,
                file_sources=file_sources,
            )
            return

        is_task_request = _looks_like_task_request(text)
        text, command_error = _expand_ai_command(text)
        if command_error is not None:
            await _safe_send(target, command_error)
            return
        if text.startswith("/"):
            await _safe_send(target, "这个命令我还不认识。发送“/帮助”查看支持的命令。")
            return

        if not limiter.allow(str(event.user_id)):
            logger.info("Rate limited one user")
            return

        conversation_id = target.conversation_id
        lock = get_conversation_lock(conversation_id)
        async with semaphore, lock:
            loaded_images: list[str] = []
            if image_sources:
                if not config.vision_enabled:
                    await _safe_send(target, "图片理解功能目前没有开启。")
                    return
                try:
                    for source in image_sources:
                        loaded_images.append(await images.load(source))
                except ImageLoadError as exc:
                    await _safe_send(target, str(exc))
                    return

            history = store.history(conversation_id, config.history_messages)
            history_text = (
                f"[用户发送了 {len(loaded_images)} 张图片] {text}"
                if loaded_images
                else text
            )
            if loaded_images:
                current_content: Any = [
                    {"type": "text", "text": text},
                    *(
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data},
                        }
                        for image_data in loaded_images
                    ),
                ]
            else:
                current_content = text
            messages = [
                {
                    "role": "system",
                    "content": (
                        config.effective_system_prompt
                        if is_task_request or loaded_images
                        else config.casual_system_prompt
                    ),
                },
                *history,
                {"role": "user", "content": current_content},
            ]
            try:
                reply = await llm.chat(
                    messages,
                    model=(
                        config.vision_model
                        if loaded_images
                        else (
                            config.llm_model
                            if is_task_request
                            else config.casual_model
                        )
                    ),
                )
            except LLMError as exc:
                await _safe_send(target, str(exc))
                return
            except Exception:
                logger.exception("Unexpected AI response failure")
                await _safe_send(target, "我这边刚刚出了点问题，请稍后再试。")
                return

            reply = reply.strip()
            if not reply:
                await _safe_send(target, "模型这次没有生成可用回复，请换个问法试试。")
                return
            if (
                not is_task_request
                and not loaded_images
                and _needs_casual_rewrite(text, reply)
            ):
                try:
                    rewritten = await _rewrite_casual_reply(
                        llm,
                        model=config.casual_model,
                        draft=reply,
                    )
                    if rewritten.strip():
                        reply = rewritten.strip()
                except LLMError:
                    logger.warning("Casual reply rewrite failed; using original reply.")
            reply = format_qq_text(reply)
            store.append_exchange(
                conversation_id,
                history_text,
                reply,
                max_rows=config.max_history_rows_per_chat,
            )

        await _send_long_reply(target, reply)

    async def _process_file_request(
        *,
        event: OneBotEvent,
        target: ReplyTarget,
        text: str,
        file_sources: Sequence[FileSource],
    ) -> None:
        conversation_id = target.conversation_id
        lock = get_conversation_lock(conversation_id)
        async with semaphore, lock:
            await _safe_send(
                target,
                f"已收到 {len(file_sources)} 个文件，正在解析并生成总结…",
            )
            parsed_documents: list[ParsedDocument] = []
            for file_source in file_sources:
                try:
                    parsed_documents.append(
                        await documents.load(file_source, sender)
                    )
                except FileParseError as exc:
                    await _safe_send(target, f"文件解析失败：{exc}")
                    return
                except Exception:
                    logger.exception("Unexpected document parsing failure")
                    await _safe_send(target, "文件解析时出现意外错误，请重新发送附件。")
                    return

            instruction = _file_instruction(text)
            try:
                reply = await summarize_documents(
                    llm,
                    model=config.llm_model,
                    documents=parsed_documents,
                    instruction=instruction,
                )
            except LLMError as exc:
                await _safe_send(target, str(exc))
                return
            except Exception:
                logger.exception("Unexpected document summary failure")
                await _safe_send(target, "文档总结时出现问题，请稍后再试。")
                return

            reply = reply.strip()
            if not reply:
                await _safe_send(target, "模型没有生成可用的文档总结。")
                return
            reply = format_qq_text(reply)
            file_names = "、".join(document.name for document in parsed_documents)
            store.append_exchange(
                conversation_id,
                f"[文件：{file_names}] {instruction}",
                reply,
                max_rows=config.max_history_rows_per_chat,
            )
        await _send_long_reply(target, reply)

    async def _process_audio_request(
        *,
        target: ReplyTarget,
        text: str,
        audio_source: AudioSource,
    ) -> None:
        if not config.audio_enabled:
            await _safe_send(target, "语音识别功能目前没有开启。")
            return

        conversation_id = target.conversation_id
        lock = get_conversation_lock(conversation_id)
        async with semaphore, lock:
            await _safe_send(target, "收到语音，正在识别…")
            try:
                loaded = await audio.load(audio_source, sender)
            except AudioLoadError as exc:
                await _safe_send(target, str(exc))
                return
            except Exception:
                logger.exception("Unexpected voice-loading failure")
                await _safe_send(target, "语音下载时出现意外错误，请重新发送。")
                return

            instruction = _audio_instruction(text)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是语音识别与理解助手。忠实转写，不要编造听不清的内容；"
                        "如果用户要求回答语音里的问题，先理解问题再回答。"
                        "输出纯文本，不使用 Markdown、emoji 或颜文字。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": loaded.data,
                                "format": loaded.format,
                            },
                        },
                    ],
                },
            ]
            try:
                reply = await llm.chat(
                    messages,
                    model=config.audio_model,
                    extra_payload={
                        "modalities": ["text"],
                        "reasoning_effort": "none",
                    },
                )
            except LLMError as exc:
                await _safe_send(target, str(exc))
                return
            except Exception:
                logger.exception("Unexpected voice-understanding failure")
                await _safe_send(target, "语音识别时出现问题，请稍后再试。")
                return

            reply = format_qq_text(reply.strip())
            if not reply:
                await _safe_send(target, "没有识别出可用内容，请重新发送语音。")
                return
            store.append_exchange(
                conversation_id,
                f"[语音] {instruction}",
                reply,
                max_rows=config.max_history_rows_per_chat,
            )
        await _send_long_reply(target, reply)

    async def _send_long_reply(target: ReplyTarget, reply: str) -> None:
        chunks = split_reply(reply, QQ_TRANSPORT_CHUNK_CHARS)
        current_target = target
        for index, chunk in enumerate(chunks):
            if not await _safe_send(current_target, chunk):
                break
            if len(chunks) > 1:
                await asyncio.sleep(0.25)
            if index == 0:
                current_target = replace(current_target, message_id=None)

    async def _is_reply_to_bot(
        message_id: str,
        self_id: int | None,
    ) -> bool:
        if self_id is None:
            return False
        try:
            sender_id = await sender.get_msg_sender_id(message_id)
        except Exception:
            logger.warning("Could not inspect quoted message", exc_info=True)
            return False
        return sender_id == self_id

    def _handle_command(
        event: OneBotEvent,
        target: ReplyTarget,
        text: str,
    ) -> str | None:
        command = text.split(maxsplit=1)[0].lower()
        if command in {"/help", "/帮助"}:
            group_hint = (
                "群里需要 @我 或使用 /ai 开头。"
                if target.message_type == "group"
                else "直接发消息就能聊天。"
            )
            return (
                f"{group_hint}\n"
                "图片：直接发送题目截图，可附带你的要求\n"
                "语音：直接发送语音，默认转写；也可以附上总结或回答要求\n"
                "笔记：/记 标题 | 内容；/笔记；/查 关键词；/删笔记 编号\n"
                "待办：/待办 内容；/待办；/完成 编号\n"
                "提醒：/提醒 2026-10-28 09:00 内容；/提醒；/取消提醒 编号\n"
                "学习工作：/计划、/总结、/出题、/翻译、/润色、/解释\n"
                "互动：/戳，或者私聊直接说“戳一戳”\n"
                "引用：机器人会引用你的原消息；引用机器人消息可以直接追问\n"
                "/重置：忘记当前聊天上下文\n"
                "/id：查看 QQ、群号和会话标识\n"
                "/ping：检查机器人是否在线\n"
                "/帮助：显示这段说明"
            )
        if command in {"/reset", "/重置"}:
            removed = store.reset(target.conversation_id)
            return f"已清空当前会话的 {removed} 条本地上下文。"
        if command == "/id":
            if target.message_type == "group":
                return (
                    f"你的 QQ：{target.user_id}\n"
                    f"群号：{target.group_id}\n"
                    f"会话标识：{target.conversation_id}"
                )
            return (
                f"你的 QQ：{target.user_id}\n"
                f"会话标识：{target.conversation_id}"
            )
        if command == "/ping":
            return "pong，我在。"
        if command == "/记":
            title, separator, content = text.partition(" ")
            note_body = content.strip()
            title, pipe, note_content = note_body.partition("|")
            if not pipe or not title.strip() or not note_content.strip():
                return "用法：/记 标题 | 要保存的内容"
            note_id = productivity.add_note(
                event.user_id or 0,
                title.strip(),
                note_content.strip(),
            )
            return f"已保存笔记 #{note_id}：{title.strip()}"
        if command == "/笔记":
            notes = productivity.list_notes(event.user_id or 0)
            if not notes:
                return "还没有保存的笔记。使用 /记 标题 | 内容 添加。"
            lines = ["最近的笔记："]
            for note in notes:
                updated = format_utc_for_zone(note.updated_at, config.timezone)
                lines.append(f"#{note.id} {note.title}（{updated}）")
            return "\n".join(lines)
        if command == "/查":
            query = text.partition(" ")[2].strip()
            if not query:
                return "用法：/查 关键词"
            notes = productivity.search_notes(event.user_id or 0, query)
            if not notes:
                return f"没有找到包含“{query}”的笔记。"
            lines = [f"找到 {len(notes)} 条笔记："]
            for note in notes:
                preview = note.content.replace("\n", " ")
                if len(preview) > 160:
                    preview = preview[:160] + "…"
                lines.append(f"#{note.id} {note.title}\n{preview}")
            return "\n\n".join(lines)
        if command == "/删笔记":
            note_id = _parse_id(text.partition(" ")[2])
            if note_id is None:
                return "用法：/删笔记 编号"
            if productivity.delete_note(event.user_id or 0, note_id):
                return f"已删除笔记 #{note_id}。"
            return f"没有找到你的笔记 #{note_id}。"
        if command == "/待办":
            todo_text = text.partition(" ")[2].strip()
            if todo_text:
                todo_id = productivity.add_todo(event.user_id or 0, todo_text)
                return f"已添加待办 #{todo_id}：{todo_text}"
            todos = productivity.list_todos(event.user_id or 0)
            if not todos:
                return "当前没有待办。使用 /待办 内容 添加。"
            pending = [todo for todo in todos if not todo.done]
            completed = [todo for todo in todos if todo.done]
            lines = ["待办："]
            lines.extend(f"#{todo.id} {todo.content}" for todo in pending)
            if completed:
                lines.append("\n最近完成：")
                lines.extend(
                    f"✓ #{todo.id} {todo.content}" for todo in completed[:5]
                )
            return "\n".join(lines)
        if command == "/完成":
            todo_id = _parse_id(text.partition(" ")[2])
            if todo_id is None:
                return "用法：/完成 编号"
            if productivity.complete_todo(event.user_id or 0, todo_id):
                return f"已完成待办 #{todo_id}。"
            return f"没有找到未完成的待办 #{todo_id}。"
        if command == "/提醒":
            reminder_argument = text.partition(" ")[2].strip()
            if reminder_argument:
                try:
                    raw_time, reminder_content = split_reminder_argument(
                        reminder_argument
                    )
                    due_at, display_time = parse_reminder_time(
                        raw_time,
                        config.timezone,
                    )
                except ReminderTimeError as exc:
                    return str(exc)
                reminder_id = productivity.add_reminder(
                    user_id=event.user_id or 0,
                    message_type=target.message_type,
                    group_id=target.group_id,
                    content=reminder_content,
                    due_at=due_at,
                )
                return f"已设置提醒 #{reminder_id}：{display_time} {reminder_content}"
            reminders = productivity.list_reminders(event.user_id or 0)
            if not reminders:
                return "当前没有待发送的提醒。"
            lines = ["待发送提醒："]
            for reminder in reminders:
                display_time = format_utc_for_zone(
                    reminder.due_at,
                    config.timezone,
                )
                lines.append(f"#{reminder.id} {display_time} {reminder.content}")
            return "\n".join(lines)
        if command == "/取消提醒":
            reminder_id = _parse_id(text.partition(" ")[2])
            if reminder_id is None:
                return "用法：/取消提醒 编号"
            if productivity.cancel_reminder(event.user_id or 0, reminder_id):
                return f"已取消提醒 #{reminder_id}。"
            return f"没有找到待发送的提醒 #{reminder_id}。"
        if command in {"/status", "/状态"}:
            if not config.owner_qq or event.user_id != config.owner_qq:
                return "这个命令仅限管理员使用。"
            return (
                f"模型：{config.llm_model}\n"
                f"当前会话记录：{store.count(target.conversation_id)} 条\n"
                f"群聊：{'开启' if config.enable_group else '关闭'}\n"
                f"限流：{config.rate_limit_per_minute} 次/分钟\n"
                f"视觉模型：{config.vision_model}"
            )
        return None

    async def _safe_send(target: ReplyTarget, text: str) -> bool:
        try:
            await sender.send_text(target, text)
            return True
        except OneBotError as exc:
            logger.warning("Failed to send a QQ message: %s", exc)
            return False
        except Exception:
            logger.exception("Unexpected message-send failure")
            return False

    async def _safe_poke(target: ReplyTarget) -> bool:
        try:
            await sender.poke(target)
            return True
        except OneBotError as exc:
            logger.warning("Failed to poke a QQ user: %s", exc)
            return False
        except Exception:
            logger.exception("Unexpected poke failure")
            return False

    return app


def _parse_id(raw: str) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _expand_ai_command(text: str) -> tuple[str, str | None]:
    command, _, argument = text.partition(" ")
    prompt = AI_COMMAND_PROMPTS.get(command)
    if prompt is None:
        return text, None
    argument = argument.strip()
    if argument:
        return f"{prompt}\n\n{argument}", None
    if command in {"/计划", "/总结", "/出题", "/解释"}:
        return f"{prompt}\n\n请基于当前对话内容完成。", None
    return text, f"用法：{command} 要处理的内容"


def _file_instruction(text: str) -> str:
    value = text.strip()
    if not value:
        return "请全面总结这些文件，提炼关键信息、结论和行动项。"
    command, _, argument = value.partition(" ")
    argument = argument.strip()
    if command == "/总结":
        return argument or "请全面总结这些文件，提炼关键信息、结论和行动项。"
    prompt = AI_COMMAND_PROMPTS.get(command)
    if prompt:
        return f"{prompt}\n{argument}".strip()
    return value


def _audio_instruction(text: str) -> str:
    value = text.strip()
    if not value:
        return "请把这段语音完整转写成简体中文，保留原意，不要补充没听到的内容。"
    command, _, argument = value.partition(" ")
    argument = argument.strip()
    if command in {"/转写", "/听写"}:
        return argument or "请把这段语音完整转写成简体中文。"
    if command == "/总结":
        return "请先准确理解语音，再用简短要点总结主要内容。"
    if command in {"/回答", "/问"}:
        return f"请先听懂语音中的问题，再直接回答。{argument}"
    return (
        "请根据下面的要求处理这段语音；如果要求不明确，就先完整转写。"
        f"\n要求：{value}"
    )


def _looks_like_task_request(text: str) -> bool:
    value = text.strip()
    if value.startswith("/"):
        return True
    return any(hint in value for hint in TASK_HINTS)


def _needs_casual_rewrite(user_text: str, reply: str) -> bool:
    lowered = user_text.lower()
    if "ai" in lowered or "机器人" in user_text:
        return False
    markers = ("\n", "**", "```", "# ", "🔹", "✅", "1.", "2.", "- ")
    return len(reply) > 100 or any(marker in reply for marker in markers)


async def _rewrite_casual_reply(
    chat_client: ChatClient,
    *,
    model: str,
    draft: str,
) -> str:
    return await chat_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你负责把一段过度完整的回复改成 QQ 熟人闲聊口吻。"
                    "像关系熟的普通朋友随口回消息，判断直接，可以轻吐槽，"
                    "但不羞辱或攻击人格。只保留核心意思。"
                    "写成一句或两句，通常不超过四十个字。"
                    "不要列点、标题、加粗、菜单、鸡汤、额外建议或继续追问；"
                    "不要用“太懂了”“我陪你”“你值得”“真朋友都懂”这类话，"
                    "也不要堆 emoji、波浪号和感叹号。"
                    "不要解释你改了什么，只输出最终回复。"
                ),
            },
            {"role": "user", "content": draft},
        ],
        model=model,
    )

