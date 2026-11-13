from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.grounded_qa import (
    _grounding_prompt,
    grounded_lookup,
    music_catalog_lookup,
)


class GroundingChat:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return json.dumps(self.payload, ensure_ascii=False)

    async def close(self) -> None:
        return None


class SequenceChat:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls = 0

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return json.dumps(payload, ensure_ascii=False)

    async def close(self) -> None:
        return None


class SynthesisChat:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "answer": "政策A发布，产品B更新。",
                    "sources": [
                        {
                            "title": "政府公告",
                            "url": "https://www.gov.cn/news/a",
                            "quote": "政策A发布",
                        },
                        {
                            "title": "百科资料",
                            "url": "https://baike.baidu.com/item/b",
                            "quote": "产品B更新",
                        },
                    ],
                },
                ensure_ascii=False,
            )
        return "1. 政策A正式发布。\n2. 产品B完成更新。"

    async def close(self) -> None:
        return None


class LiveSearchChat:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.calls += 1
        return json.dumps(
            {
                "answer": "1. 政策A正式发布。\n2. 产品B完成更新。",
                "source_ids": [1, 2],
            },
            ensure_ascii=False,
        )

    async def close(self) -> None:
        return None


def test_grounding_prompt_requests_an_organized_answer() -> None:
    prompt = _grounding_prompt(
        "联网搜索最近的人工智能新闻，整理三条",
        strict=False,
    )
    assert "2 到 5 条" in prompt
    assert "不要只回一句话" in prompt


@pytest.mark.asyncio
async def test_explicit_search_uses_real_search_result_urls() -> None:
    chat = LiveSearchChat()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.so.com":
            return httpx.Response(
                200,
                text=(
                    '<html><body>'
                    '<a data-mdurl="https://www.gov.cn/policy" '
                    'href="#">政策A正式发布</a>'
                    '<a data-mdurl="https://www.chinanews.com/tech" '
                    'href="#">产品B完成更新</a>'
                    '<a data-mdurl="https://example.com/untrusted" '
                    'href="#">自媒体猜测</a>'
                    '</body></html>'
                ),
            )
        return httpx.Response(
            200,
            text=(
                "<html><body>人工智能政策A正式发布，"
                "人工智能产品B完成更新，相关措施已开始实施。"
                + "政策细则覆盖研发、应用和安全评估。" * 12
                + "</body></html>"
            ),
        )

    result = await grounded_lookup(
        chat,
        model="test",
        question="联网搜索人工智能最新政策，整理两条",
        transport=httpx.MockTransport(handler),
    )
    assert result.verified is True
    assert result.verification_level == "live_search_pages"
    assert {source.url for source in result.sources} == {
        "https://www.gov.cn/policy",
        "https://www.chinanews.com/tech",
    }
    assert chat.calls == 1


@pytest.mark.asyncio
async def test_grounded_search_organizes_verified_page_content() -> None:
    chat = SynthesisChat()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>政策A发布，产品B更新。</body></html>",
        )

    result = await grounded_lookup(
        chat,
        model="test",
        question="联网搜索最近AI政策，整理两条",
        transport=httpx.MockTransport(handler),
    )
    assert chat.calls == 2
    assert result.verified is True
    assert result.answer.startswith("1. 政策A")
    assert "2. 产品B" in result.answer
    assert "来源：" in result.formatted()


@pytest.mark.asyncio
async def test_grounded_lookup_verifies_two_trusted_domains() -> None:
    chat = GroundingChat(
        {
            "answer": "《连名带姓》由张惠妹演唱。",
            "sources": [
                {
                    "title": "百度百科",
                    "url": "https://baike.baidu.com/item/test",
                    "quote": "张惠妹演唱",
                },
                {
                    "title": "维基百科",
                    "url": "https://zh.wikipedia.org/wiki/test",
                    "quote": "张惠妹",
                },
            ],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>张惠妹演唱《连名带姓》</body></html>",
        )

    result = await grounded_lookup(
        chat,
        model="test",
        question="连名带姓谁唱的",
        transport=httpx.MockTransport(handler),
    )
    assert result.verified is True
    assert len(result.sources) == 2
    assert "baike.baidu.com" in result.formatted()


@pytest.mark.asyncio
async def test_grounded_lookup_rejects_single_domain() -> None:
    chat = GroundingChat(
        {
            "answer": "答案",
            "sources": [
                {
                    "title": "A",
                    "url": "https://baike.baidu.com/a",
                    "quote": "答案",
                },
                {
                    "title": "B",
                    "url": "https://baike.baidu.com/b",
                    "quote": "答案",
                },
            ],
        }
    )
    result = await grounded_lookup(
        chat,
        model="test",
        question="问题",
    )
    assert result.verified is False
    assert "两个不同域名" in result.reason


@pytest.mark.asyncio
async def test_grounded_lookup_retries_cover_version_and_requires_authority() -> None:
    chat = SequenceChat(
        [
            {
                "answer": "《连名带姓》由薛之谦演唱。",
                "sources": [
                    {
                        "title": "翻唱版",
                        "url": "https://y.qq.com/cover",
                        "quote": "薛之谦翻唱",
                    },
                    {
                        "title": "翻唱页面",
                        "url": "https://music.163.com/cover",
                        "quote": "翻唱版本",
                    },
                ],
            },
            {
                "answer": "《连名带姓》由张惠妹演唱。",
                "sources": [
                    {
                        "title": "百度百科",
                        "url": "https://baike.baidu.com/item/test",
                        "quote": "张惠妹演唱",
                    },
                    {
                        "title": "维基百科",
                        "url": "https://zh.wikipedia.org/wiki/test",
                        "quote": "张惠妹",
                    },
                ],
            },
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>张惠妹演唱《连名带姓》</body></html>",
        )

    result = await grounded_lookup(
        chat,
        model="test",
        question="连名带姓原唱是谁",
        transport=httpx.MockTransport(handler),
    )
    assert chat.calls == 2
    assert result.verified is True
    assert "张惠妹" in result.answer


@pytest.mark.asyncio
async def test_grounded_lookup_falls_back_to_domain_cross_check() -> None:
    chat = GroundingChat(
        {
            "answer": "《连名带姓》由张惠妹演唱。",
            "sources": [
                {
                    "title": "百度百科：连名带姓",
                    "url": "https://baike.baidu.com/item/test",
                    "quote": "张惠妹演唱",
                },
                {
                    "title": "维基百科：连名带姓",
                    "url": "https://zh.wikipedia.org/wiki/test",
                    "quote": "张惠妹",
                },
            ],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    result = await grounded_lookup(
        chat,
        model="test",
        question="连名带姓原唱是谁",
        transport=httpx.MockTransport(handler),
    )
    assert result.verified is True
    assert result.verification_level == "domain_cross_checked"


@pytest.mark.asyncio
async def test_grounded_lookup_rejects_missing_invented_urls() -> None:
    chat = GroundingChat(
        {
            "answer": "政策正式发布。",
            "sources": [
                {
                    "title": "政府公告",
                    "url": "https://www.gov.cn/fake-a",
                    "quote": "政策正式发布",
                },
                {
                    "title": "百科资料",
                    "url": "https://baike.baidu.com/fake-b",
                    "quote": "政策正式发布",
                },
            ],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    result = await grounded_lookup(
        chat,
        model="test",
        question="联网搜索这个政策",
        transport=httpx.MockTransport(handler),
    )
    assert result.verified is False
    assert "无法访问" in result.reason


@pytest.mark.asyncio
async def test_music_catalog_cross_checks_original_singer() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "c.y.qq.com":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "song": {
                            "list": [
                                {
                                    "songname": "连名带姓",
                                    "songmid": "qq-mid",
                                    "albumname": "偷故事的人",
                                    "singer": [{"name": "张惠妹"}],
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "songs": [
                        {
                            "id": 123,
                            "name": "连名带姓",
                            "artists": [{"name": "翻唱歌手"}],
                            "album": {
                                "name": "连名带姓（翻自 张惠妹）"
                            },
                        }
                    ]
                }
            },
        )

    result = await music_catalog_lookup(
        "连名带姓这首歌是谁唱的",
        transport=httpx.MockTransport(handler),
    )
    assert result is not None
    assert result.verified is True
    assert "张惠妹" in result.answer
    assert result.verification_level == "music_catalog_cross_checked"
    assert len(result.sources) == 2
