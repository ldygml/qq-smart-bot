from __future__ import annotations

import json
import re
from typing import Any, Protocol

from .llm import ChatClient


class StickerChoiceDecider(Protocol):
    async def choose(
        self,
        *,
        history: list[dict[str, str]],
        user_text: str,
        assistant_text: str,
        candidates: list[dict[str, object]],
        explicit_request: bool,
    ) -> int | None: ...


class LLMStickerChoiceDecider:
    def __init__(
        self,
        chat_client: ChatClient,
        *,
        model: str,
        min_confidence: float = 0.7,
    ) -> None:
        self.chat_client = chat_client
        self.model = model
        self.min_confidence = min_confidence

    async def choose(
        self,
        *,
        history: list[dict[str, str]],
        user_text: str,
        assistant_text: str,
        candidates: list[dict[str, object]],
        explicit_request: bool,
    ) -> int | None:
        if not candidates:
            return None
        catalog = "\n".join(
            f"id={item.get('id')}；名称={item.get('summary')}；"
            f"已使用={item.get('use_count', 0)}次"
            for item in candidates[:30]
        )
        recent = "\n".join(
            f"{item.get('role')}: {str(item.get('content', ''))[:180]}"
            for item in history[-8:]
        )
        prompt = (
            "判断是否发送一个 QQ 商城表情包。只能从给定表情库选择，不能编造 ID。"
            '返回 JSON：{"sticker_id":数字或null,"confidence":0到1}。\n'
            "只有在表情名称和当前语境明显匹配时才发送；没有合适表情就返回 null。"
            "用户明确要表情包时可以更积极，但不能选择语义冲突的表情。"
            "普通聊天不要为了活跃气氛强行发表情。\n\n"
            f"是否明确请求表情包：{'是' if explicit_request else '否'}\n"
            f"最近对话：\n{recent or '无'}\n"
            f"用户最新消息：{user_text}\n"
            f"机器人刚回复：{assistant_text}\n"
            f"可用表情库：\n{catalog}"
        )
        try:
            reply = await self.chat_client.chat(
                [
                    {
                        "role": "system",
                        "content": "严格输出 JSON，不要 Markdown，不要解释。",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.model,
                extra_payload={
                    "reasoning_effort": "none",
                    "response_format": {"type": "json_object"},
                },
            )
        except Exception:
            return None
        data = _parse_json(reply)
        sticker_id = data.get("sticker_id")
        try:
            sticker_id = int(sticker_id)
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            return None
        if confidence < self.min_confidence:
            return None
        valid_ids = {int(item["id"]) for item in candidates}
        return sticker_id if sticker_id in valid_ids else None


def _parse_json(reply: str) -> dict[str, Any]:
    value = reply.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}
