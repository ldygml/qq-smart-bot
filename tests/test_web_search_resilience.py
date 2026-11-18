from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.grounded_qa import (
    GroundedAnswer,
    GroundedSource,
    _clean_search_query,
    _decode_html_response,
    _is_low_quality_search_domain,
    grounded_lookup,
    is_explicit_search_request,
)
from qq_ai_bot.app import _format_grounded_reply


class SourceChat:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.calls += 1
        return json.dumps(
            {
                "answer": "今天上海晴，6 到 14 摄氏度。",
                "source_ids": [1, 2],
            },
            ensure_ascii=False,
        )

    async def close(self) -> None:
        return None


class BrokenChat:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        self.calls += 1
        return "not-json"

    async def close(self) -> None:
        return None


class SingleSourceChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return json.dumps(
            {
                "answer": "今晚 23:00 有一场足球比赛。[来源1]",
                "source_ids": [1],
            },
            ensure_ascii=False,
        )

    async def close(self) -> None:
        return None


class StaleSourceChat:
    async def chat(self, messages, *, model=None, extra_payload=None) -> str:
        return json.dumps(
            {
                "answer": "今日（2026年11月17日）有一场足球比赛。[来源1]",
                "source_ids": [1],
            },
            ensure_ascii=False,
        )

    async def close(self) -> None:
        return None


def _search_transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.so.com":
            return httpx.Response(
                200,
                text=(
                    "<html><body>"
                    '<a data-mdurl="https://www.weather.com.cn/weather/a" '
                    'href="#">上海天气预报</a>'
                    '<a data-mdurl="https://tianqi.2345.com/today/a" '
                    'href="#">上海今日天气</a>'
                    "</body></html>"
                ),
            )
        return httpx.Response(
            200,
            text=(
                "<html><body>今天上海晴，气温 6 到 14 摄氏度，"
                "西北风 2 级，空气质量良，无降水。"
                + "天气页面正文补充信息。" * 30
                + "</body></html>"
            ),
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "question",
    [
        "联网搜索今天上海天气",
        "你上网帮我找找最近的转会消息",
        "给我查一下明天的航班",
        "去网上看看这个政策",
        "直接搜搜看连名带姓原唱",
        "用网络查一下这个新闻",
        "搜索今天的足球赛程",
    ],
)
def test_natural_explicit_search_requests_are_detected(question: str) -> None:
    assert is_explicit_search_request(question)


def test_normal_question_about_search_algorithm_is_not_explicit() -> None:
    assert not is_explicit_search_request("搜索算法的时间复杂度怎么分析")
    assert is_explicit_search_request("搜索今天的足球赛程")


def test_search_wrappers_are_removed_from_query() -> None:
    assert _clean_search_query("你联网帮我查一下今天的足球赛程，给我来源") == (
        "今天的足球赛程"
    )


def test_generated_sports_seo_domains_are_filtered() -> None:
    assert _is_low_quality_search_domain("zuqusaichengbiao.com.cn")
    assert _is_low_quality_search_domain(
        "jntanzuqusaisyilanba.com.cn"
    )
    assert _is_low_quality_search_domain(
        "jinrizuqiusaicbiachaxun1df.com.cn"
    )
    assert not _is_low_quality_search_domain("www.okooo.com")


def test_html_pages_without_charset_fall_back_to_gb18030() -> None:
    response = httpx.Response(
        200,
        content="<html><body>今日足球赛程</body></html>".encode("gb18030"),
    )
    assert "今日足球赛程" in _decode_html_response(response)


def test_unverified_search_text_is_not_discarded() -> None:
    reply = _format_grounded_reply(
        GroundedAnswer(
            answer="检索结果正文",
            sources=(
                GroundedSource(
                    title="天气来源",
                    url="https://example.com/weather",
                    quote="天气",
                ),
            ),
            verified=False,
            reason="未完成双源核验",
        )
    )
    assert "联网搜索已经执行" in reply
    assert "检索结果正文" in reply
    assert "https://example.com/weather" in reply


@pytest.mark.asyncio
async def test_general_search_does_not_require_government_sources() -> None:
    result = await grounded_lookup(
        SourceChat(),
        model="test",
        question="联网搜索今天上海天气",
        transport=_search_transport(),
    )
    assert result.verified is True
    assert result.verification_level == "live_search_pages"
    assert "上海" in result.answer
    assert {source.url for source in result.sources} == {
        "https://www.weather.com.cn/weather/a",
        "https://tianqi.2345.com/today/a",
    }


@pytest.mark.asyncio
async def test_search_results_are_returned_when_summary_json_fails() -> None:
    result = await grounded_lookup(
        BrokenChat(),
        model="test",
        question="你上网帮我查今天上海天气",
        transport=_search_transport(),
    )
    assert result.verified is False
    assert result.answer
    assert result.verification_level == "live_search_pages_unverified"
    assert len(result.sources) == 2


@pytest.mark.asyncio
async def test_live_pages_are_fetched_for_football_schedule_query() -> None:
    result = await grounded_lookup(
        BrokenChat(),
        model="test",
        question="搜索今天的足球赛程",
        transport=_search_transport(),
    )
    assert result.verification_level == "live_search_pages_unverified"
    assert len(result.sources) == 2


@pytest.mark.asyncio
async def test_real_time_football_can_use_one_known_data_source() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.so.com":
            return httpx.Response(
                200,
                text=(
                    '<a data-mdurl="https://www.okooo.com/jingcai/" '
                    'href="#">竞足赛程</a>'
                ),
            )
        return httpx.Response(
            200,
            text="<html><body>2026-11-18 今日足球赛程 " + "比赛数据 " * 80 + "</body></html>",
        )

    result = await grounded_lookup(
        SingleSourceChat(),
        model="test",
        question="搜索今天的足球赛程",
        transport=httpx.MockTransport(handler),
    )
    assert result.verified is True
    assert result.verification_level == "live_search_pages"
    assert result.sources[0].url == "https://www.okooo.com/jingcai/"


@pytest.mark.asyncio
async def test_stale_schedule_is_not_presented_as_today() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.so.com":
            return httpx.Response(
                200,
                text=(
                    '<a data-mdurl="https://www.okooo.com/jingcai/" '
                    'href="#">竞足赛程</a>'
                ),
            )
        return httpx.Response(
            200,
            text="<html><body>足球赛程 " + "比赛数据 " * 80 + "</body></html>",
        )

    result = await grounded_lookup(
        StaleSourceChat(),
        model="test",
        question="搜索今天的足球赛程",
        transport=httpx.MockTransport(handler),
        current_date="2026-11-18",
    )
    assert result.verified is False
    assert result.verification_level == "stale_live_source"
    assert "2026-11-17" in result.answer
    assert "2026-11-18" in result.answer
