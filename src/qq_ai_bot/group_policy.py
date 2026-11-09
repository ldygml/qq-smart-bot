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
        min_confidence: float = 0.58,
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
        prompt = _group_participation_prompt(
            history=history,
            current_sender=current_sender,
            current_text=current_text,
            extra_instructions=self.extra_instructions,
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
            return _local_should_reply(current_text)
        if not decision:
            return _local_should_reply(current_text)
        model_accepts = (
            decision.get("reply") is True
            and float(decision.get("confidence", 0)) >= self.min_confidence
        )
        return model_accepts or _local_should_reply(current_text)


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


def _group_participation_prompt(
    *,
    history: list[GroupMessage],
    current_sender: str,
    current_text: str,
    extra_instructions: str,
) -> str:
    recent = "\n".join(
        f"{item.sender}（{item.role}）：{item.content[:300]}"
        for item in history[-12:]
    )
    extra = (
        f"\n本群额外规则：{extra_instructions[:2000]}\n"
        if extra_instructions.strip()
        else ""
    )
    return (
        "判断 QQ 群里的机器人现在是否适合自然加入聊天。"
        "目标是像正常群友一样偶尔接话，不是客服自动答疑，"
        "也不必为了活跃而每条都回。\n"
        '只返回 JSON：{"reply":true/false,"confidence":0到1,'
        '"reason":"简短原因"}。\n'
        "适合回复：提出大家都能讨论的问题；需要补充事实、纠错或建议；"
        "话题有趣，机器人能用一两句自然接梗；有人继续追问机器人刚说过的话题；"
        "有人邀请大家参与或征求意见。普通闲聊只要接话自然，也可以回复。\n"
        "不必回复：纯寒暄、单字确认、连续复读、别人已经完整回答；"
        "明显是两个人之间的私密连续对话；机器人插话会打断对话；"
        "机器人刚说完且没有新的有效信息；话题完全超出把握。\n"
        "不要为了显得活跃而强行发表情、总结、讲道理或把话题扯到足球。\n"
        "真正适合参与时 confidence 给 0.7 以上；"
        "只有非常明确时才给 0.85 以上；拿不准时返回 false。\n"
        f"{extra}\n"
        f"群里最近消息：\n{recent or '无'}\n\n"
        f"最新消息来自：{current_sender}\n"
        f"最新内容：{current_text}"
    )


def _local_should_reply(text: str) -> bool:
    value = re.sub(r"\s+", "", text)
    if obvious_ignore(value):
        return False
    question_markers = (
        "?",
        "？",
        "怎么",
        "为什么",
        "咋",
        "如何",
        "能不能",
        "可不可以",
        "有没有",
        "是不是",
        "谁",
        "什么",
        "哪",
        "多少",
        "请问",
        "求",
        "帮",
        "看看",
        "解释",
        "推荐",
        "建议",
    )
    return any(marker in value for marker in question_markers)


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
