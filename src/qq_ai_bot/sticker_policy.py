from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .llm import ChatClient


@dataclass(frozen=True)
class StickerClassification:
    category: str
    usage_hint: str
    tags: tuple[str, ...] = ()


class LLMStickerOrganizer:
    def __init__(
        self,
        chat_client: ChatClient,
        *,
        model: str,
        vision_model: str | None = None,
    ) -> None:
        self.chat_client = chat_client
        self.model = model
        self.vision_model = vision_model or model

    async def classify(
        self,
        *,
        summary: str,
        recent_context: str = "",
        image_url: str = "",
    ) -> StickerClassification:
        prompt = (
            "整理一个 QQ 表情包。根据名称和最近语境判断它的情绪与适用场景。"
            '只返回 JSON：{"category":"分类","usage_hint":"何时适合使用",'
            '"tags":["识别到的文字、梗或情绪标签"]}。'
            "分类优先从：开心、无语、震惊、生气、委屈、嘲讽、赞同、"
            "疑惑、安慰、加油、日常 中选择，也可以使用更准确的中文短分类。"
            "usage_hint 要具体，例如“对方说了离谱的事情时使用”。"
            "同时说明适合哪些熟悉程度、关系阶段或联系人，以及哪些严肃场景不应使用。"
            "不要在语境冲突时强行使用表情。\n\n"
            f"表情名称：{summary or '动画表情'}\n"
            f"表情图片：{'已提供，请直接识别画面和文字' if image_url else '无'}\n"
            f"最近聊天：{recent_context[:1200] or '无'}"
        )
        user_content: Any = prompt
        if image_url.startswith(("http://", "https://", "data:image/")):
            user_content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                },
            ]
        try:
            reply = await self.chat_client.chat(
                [
                    {
                        "role": "system",
                        "content": "严格输出 JSON，不要 Markdown，不要解释。",
                    },
                    {"role": "user", "content": user_content},
                ],
                model=self.vision_model if image_url else self.model,
                extra_payload={
                    "reasoning_effort": "none",
                    "response_format": {"type": "json_object"},
                },
            )
            data = _parse_json(reply)
            category = str(data.get("category") or "").strip()[:80]
            usage_hint = str(data.get("usage_hint") or "").strip()[:300]
            raw_tags = data.get("tags")
            tags = tuple(
                str(item).strip()[:40]
                for item in raw_tags[:12]
                if str(item).strip()
            ) if isinstance(raw_tags, list) else ()
            if category and usage_hint:
                return StickerClassification(category, usage_hint, tags)
        except Exception:
            pass
        return infer_sticker_classification(summary)


class StickerChoiceDecider(Protocol):
    async def choose(
        self,
        *,
        history: list[dict[str, str]],
        user_text: str,
        assistant_text: str,
        candidates: list[dict[str, object]],
        explicit_request: bool,
        contact_context: str = "",
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
        contact_context: str = "",
    ) -> int | None:
        if not candidates:
            return None
        catalog = "\n".join(
            f"id={item.get('id')}；名称={item.get('summary')}；"
            f"分类={item.get('category', '未分类')}；"
            f"适用场景={item.get('usage_hint') or '未设置'}；"
            f"标签={','.join(str(tag) for tag in item.get('tags', [])[:8]) or '无'}；"
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
            "还必须适应当前联系人的关系、称呼和禁忌；对方不想开玩笑或语境严肃时宁可不发。"
            "用户明确要表情包时可以更积极，但不能选择语义冲突的表情。"
            "普通聊天不要为了活跃气氛强行发表情。\n\n"
            f"是否明确请求表情包：{'是' if explicit_request else '否'}\n"
            f"联系人上下文：{contact_context or '无额外资料'}\n"
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


def infer_sticker_classification(summary: str) -> StickerClassification:
    value = summary.lower()
    rules = (
        (
            ("开心", "笑", "哈哈", "庆祝", "赞", "妙", "棒"),
            "开心",
            "气氛轻松、值得庆祝或表示赞同的时候使用",
        ),
        (
            ("无语", "汗", "省略号", "尴尬", "离谱", "服了"),
            "无语",
            "对方说了离谱、尴尬或让人接不下去的事情时使用",
        ),
        (
            ("震惊", "惊", "吓", "啊", "什么"),
            "震惊",
            "听到意外消息或需要表达惊讶时使用",
        ),
        (
            ("生气", "怒", "火", "打", "刀"),
            "生气",
            "表达不满或对明显不合理的事情作出反应时使用",
        ),
        (
            ("委屈", "哭", "难过", "泪", "可怜"),
            "委屈",
            "对方情绪低落或需要少量安慰时使用",
        ),
        (
            ("嘲讽", "白眼", "呵呵", "阴阳", "菜"),
            "嘲讽",
            "轻松互损或指出明显失误时使用，不能用于严肃求助",
        ),
        (
            ("疑惑", "问号", "懵", "不懂"),
            "疑惑",
            "没有理解对方意思或需要请求解释时使用",
        ),
        (
            ("加油", "努力", "冲", "鼓励", "抱抱"),
            "加油",
            "对方需要鼓励、准备行动或完成阶段目标时使用",
        ),
    )
    for keywords, category, hint in rules:
        if any(keyword in value for keyword in keywords):
            return StickerClassification(category, hint)
    return StickerClassification(
        "日常",
        "普通闲聊中语气和情绪匹配时使用，不要连续发送",
    )
