from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .llm import ChatClient


@dataclass(frozen=True)
class GroupMessage:
    role: str
    sender: str
    content: str


class GroupReplyDecider(Protocol):
    async def should_reply(
        self,
        *,
        history: list[GroupMessage],
        current_sender: str,
        current_text: str,
    ) -> bool: ...


class LLMGroupReplyDecider:
    def __init__(
        self,
        chat_client: ChatClient,
        *,
        model: str,
        min_confidence: float = 0.65,
        extra_instructions: str = "",
    ) -> None:
        self.chat_client = chat_client
        self.model = model
        self.min_confidence = min_confidence
        self.extra_instructions = extra_instructions.strip()

    async def should_reply(
        self,
        *,
        history: list[GroupMessage],
        current_sender: str,
        current_text: str,
    ) -> bool:
        recent = "\n".join(
            f"{item.sender}（{item.role}）：{item.content[:300]}"
            for item in history[-12:]
        )
        prompt = (
            "判断 QQ 群里的机器人是否应该回复最新消息。要保守，不要抢话。\n"
            "返回 JSON：{\"reply\":true/false,\"confidence\":0到1,"
            "\"reason\":\"简短原因\"}。\n"
            "应该回复：明确向机器人提问；请求解答、处理或决策；"
            "话题需要机器人补充关键事实；有人直接叫机器人名字。\n"
            "不应回复：群友之间聊天；寒暄、道谢、晚安、哈哈大笑、"
            "短确认、表情回应；只是在自言自语；别人已经回答了；"
            "机器人刚说完后没有新问题；没有把握或不相关的话题。\n"
            "如果消息只是群友互相接话、玩梗、复读或已经形成完整对话，"
            "机器人插进去只会像硬找存在感，应返回 false。"
            "被 @ 或被叫名字但如果只是一句无内容的招呼，可以回一句很短的话，"
            "不要顺势长篇输出观点、总结或功能菜单。\n"
            "不要因为机器人是足球迷就把无关话题硬扯到足球。\n\n"
            + (
                f"本群额外规则：{self.extra_instructions[:2000]}\n\n"
                if self.extra_instructions
                else ""
            )
            + f"群里最近消息：\n{recent or '无'}\n\n"
            f"最新消息来自：{current_sender}\n"
            f"最新内容：{current_text}"
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
            decision = _parse_decision(reply)
        except Exception:
            return False
        return (
            decision.get("reply") is True
            and float(decision.get("confidence", 0)) >= self.min_confidence
        )


def obvious_ignore(text: str) -> bool:
    value = re.sub(r"[\s，。！？!?,.~～]+", "", text)
    if not value:
        return True
    if len(value) <= 2 and value not in {"在吗", "求助", "问题"}:
        return True
    return value.lower() in {
        "哈",
        "哈哈",
        "哈哈哈",
        "笑死",
        "好的",
        "好",
        "行",
        "可以",
        "收到",
        "谢谢",
        "谢了",
        "晚安",
        "早安",
        "在吗",
        "牛",
        "6",
        "666",
    }


def _parse_decision(reply: str) -> dict[str, Any]:
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
