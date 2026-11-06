from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .llm import ChatClient


TRUSTED_DOMAINS = (
    "baike.baidu.com",
    "wikipedia.org",
    "zhihu.com",
    "douban.com",
    "music.163.com",
    "y.qq.com",
    "kugou.com",
    "xinhuanet.com",
    "people.com.cn",
    "cctv.com",
    "thepaper.cn",
    "sina.com.cn",
    "sohu.com",
    "bilibili.com",
    "gov.cn",
    "edu.cn",
)
AUTHORITY_DOMAINS = (
    "baike.baidu.com",
    "wikipedia.org",
    "xinhuanet.com",
    "people.com.cn",
    "cctv.com",
    "gov.cn",
    "edu.cn",
)


@dataclass(frozen=True)
class GroundedSource:
    title: str
    url: str
    quote: str


@dataclass(frozen=True)
class GroundedAnswer:
    answer: str
    sources: tuple[GroundedSource, ...] = ()
    verified: bool = False
    reason: str = ""
    verification_level: str = "none"

    def formatted(self) -> str:
        if not self.answer:
            return self.reason
        lines = [self.answer]
        if self.sources:
            lines.append("来源：")
            lines.extend(
                f"- {source.title}：{source.url}"
                for source in self.sources[:4]
            )
        return "\n".join(lines)


async def grounded_lookup(
    chat_client: ChatClient,
    *,
    model: str,
    question: str,
    trust_env: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GroundedAnswer:
    music_result = await music_catalog_lookup(
        question,
        trust_env=trust_env,
        transport=transport,
    )
    if music_result is not None:
        return music_result
    reason = "没有获得可核验的事实答案。"
    for attempt in range(2):
        prompt = _grounding_prompt(question, strict=attempt > 0)
        reply = await chat_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "必须使用联网搜索。严格输出 JSON，不要 Markdown。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            model=model,
            extra_payload={
                "enable_search": True,
                "search_options": {
                    "forced_search": True,
                    "enable_source": True,
                },
                "reasoning_effort": "none",
                "response_format": {"type": "json_object"},
            },
        )
        data = _parse_json(reply)
        answer = str(data.get("answer") or "").strip()
        raw_sources = data.get("sources")
        if not answer or not isinstance(raw_sources, list):
            continue
        candidates = _candidate_sources(
            raw_sources,
            question=question,
            answer=answer,
        )
        if len({urlparse(item.url).netloc.lower() for item in candidates}) < 2:
            reason = "检索结果缺少两个不同域名的可信来源，暂不下结论。"
            continue
        verified = await _verify_sources(
            candidates,
            answer=answer,
            trust_env=trust_env,
            transport=transport,
        )
        if (
            len({urlparse(item.url).netloc for item in verified}) < 2
            or not any(
                _is_authority_domain(urlparse(item.url).netloc)
                for item in verified
            )
        ):
            accepted = _cross_domain_fallback(
                candidates,
                answer=answer,
            )
            if accepted:
                return GroundedAnswer(
                    answer=answer,
                    sources=tuple(accepted),
                    verified=True,
                    verification_level="domain_cross_checked",
                )
            reason = "来源页面无法访问或缺少权威交叉来源，暂不下结论。"
            continue
        return GroundedAnswer(
            answer=answer,
            sources=tuple(verified),
            verified=True,
            verification_level="page_verified",
        )
    return GroundedAnswer("", reason=reason)


async def music_catalog_lookup(
    question: str,
    *,
    trust_env: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GroundedAnswer | None:
    if not any(
        marker in question
        for marker in ("谁唱的", "是谁唱的", "原唱", "这首歌")
    ):
        return None
    title = _extract_song_title(question)
    if not title:
        return None
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"}
    async with httpx.AsyncClient(
        trust_env=trust_env,
        timeout=httpx.Timeout(15.0),
        transport=transport,
        headers=headers,
    ) as client:
        try:
            qq_response = await client.get(
                "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                params={"w": title, "format": "json", "n": 20, "p": 1},
            )
            qq_response.raise_for_status()
            qq_data = qq_response.json()
            netease_response = await client.get(
                "https://music.163.com/api/search/get/web",
                params={
                    "s": title,
                    "type": 1,
                    "offset": 0,
                    "total": "true",
                    "limit": 30,
                },
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://music.163.com/",
                },
            )
            netease_response.raise_for_status()
            netease_data = netease_response.json()
        except (httpx.HTTPError, ValueError):
            return None
    qq_candidates: list[tuple[str, str, str, str]] = []
    if isinstance(qq_data, dict):
        for item in (
            qq_data.get("data", {})
            .get("song", {})
            .get("list", [])
        ):
            if not isinstance(item, dict):
                continue
            name = str(item.get("songname") or "")
            artists = [
                str(artist.get("name") or "")
                for artist in item.get("singer", [])
                if isinstance(artist, dict)
            ]
            album = str(item.get("albumname") or "")
            mid = str(item.get("songmid") or "")
            if (
                mid
                and artists
                and _same_song_title(name, title)
                and not _is_cover_or_live(name, album)
            ):
                qq_candidates.append(
                    (name, "、".join(artists), album, mid)
                )
    if not qq_candidates:
        return None
    qq_name, qq_singer, qq_album, qq_mid = qq_candidates[0]
    netease_source: GroundedSource | None = None
    if isinstance(netease_data, dict):
        for item in netease_data.get("result", {}).get("songs", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            artists = [
                str(artist.get("name") or "")
                for artist in item.get("artists", [])
                if isinstance(artist, dict)
            ]
            album = str(item.get("album", {}).get("name") or "")
            song_id = item.get("id")
            if not song_id or not _same_song_title(name, title):
                continue
            confirms_original = (
                " ".join(artists) == qq_singer
                or f"翻自 {qq_singer}" in album
                or f"翻自{qq_singer}" in album
            )
            if not confirms_original:
                continue
            netease_source = GroundedSource(
                title=(
                    f"网易云音乐：{name}"
                    + (f" - {'、'.join(artists)}" if artists else "")
                ),
                url=f"https://music.163.com/#/song?id={song_id}",
                quote=album or "、".join(artists),
            )
            break
    if netease_source is None:
        return None
    answer = f"《{qq_name}》原唱是{qq_singer}"
    if qq_album:
        answer += f"，收录于专辑《{qq_album}》"
    answer += "。"
    return GroundedAnswer(
        answer=answer,
        sources=(
            GroundedSource(
                title=f"QQ 音乐：{qq_name} - {qq_singer}",
                url=f"https://y.qq.com/n/ryqq/songDetail/{qq_mid}",
                quote=qq_album,
            ),
            netease_source,
        ),
        verified=True,
        verification_level="music_catalog_cross_checked",
    )


def _extract_song_title(question: str) -> str:
    quoted = re.search(r"《(.+?)》", question)
    if quoted:
        return quoted.group(1).strip()[:120]
    value = question.strip()
    for marker in (
        "这首歌是谁唱的",
        "这首歌谁唱的",
        "的这首歌是谁唱的",
        "原唱是谁",
        "是谁唱的",
        "谁唱的",
        "原唱",
        "这首歌",
        "歌曲",
        "？",
        "?",
        "。",
    ):
        value = value.replace(marker, "")
    value = re.sub(r"^(?:请问|问一下|你知道)", "", value)
    return value.strip(" ：:，,")[:120]


def _same_song_title(candidate: str, wanted: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(
            r"[^\u4e00-\u9fffA-Za-z0-9]",
            "",
            value,
        ).lower()

    left = normalize(candidate)
    right = normalize(wanted)
    return left == right or (
        left.startswith(right)
        and any(
            marker in candidate.lower()
            for marker in ("live", "伴奏", "remix", "dj")
        )
    )


def _is_cover_or_live(name: str, album: str) -> bool:
    value = f"{name} {album}".lower()
    return any(
        marker in value
        for marker in (
            "live",
            "翻唱",
            "翻自",
            "cover",
            "伴奏",
            "remix",
            "dj版",
        )
    )


def _grounding_prompt(question: str, *, strict: bool) -> str:
    extra = (
        "上一次结果把翻唱、同名作品或二手页面误当成原事实。请重新搜索，"
        "必须至少有一个百科、官方媒体、政府、学校或正式机构页面。"
        if strict
        else ""
    )
    return (
        "联网核实这个事实问题。必须给出简洁答案和 2 到 4 个不同域名的来源。"
        "每个来源必须包含真实可访问的标题、完整 URL 和支撑答案的原文短句。"
        "至少一个来源必须来自百科、官方媒体或权威机构。"
        "问歌曲时严格区分原唱、翻唱和同名歌曲，音乐平台上的翻唱页不能证明原唱。"
        "不能编造 URL；无法找到两个独立可信来源时，answer 留空。"
        f"{extra}\n"
        '只返回 JSON：{"answer":"一句话答案",'
        '"sources":[{"title":"来源标题","url":"https://...",'
        '"quote":"支撑短句"}]}\n\n'
        f"问题：{question}"
    )


def _candidate_sources(
    raw_sources: list,
    *,
    question: str,
    answer: str,
) -> list[GroundedSource]:
    asks_original = any(
        marker in question
        for marker in ("原唱", "谁唱的", "是谁唱的")
    )
    candidates: list[GroundedSource] = []
    seen_urls: set[str] = set()
    answer_terms = _important_terms(answer)
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:200]
        url = str(item.get("url") or "").strip()
        quote = str(item.get("quote") or "").strip()[:500]
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not _is_trusted_domain(parsed.netloc)
            or url in seen_urls
        ):
            continue
        if asks_original and any(
            marker in f"{title} {quote}"
            for marker in ("翻唱", "翻自", "cover", "改编")
        ):
            continue
        source_text = f"{title} {quote}"
        if (
            answer_terms
            and not any(term in source_text for term in answer_terms)
        ):
            continue
        seen_urls.add(url)
        candidates.append(GroundedSource(title or parsed.netloc, url, quote))
    return candidates


def _cross_domain_fallback(
    candidates: list[GroundedSource],
    *,
    answer: str,
) -> list[GroundedSource]:
    selected: list[GroundedSource] = []
    domains: set[str] = set()
    answer_terms = _important_terms(answer)
    for source in candidates:
        host = urlparse(source.url).netloc.lower()
        if host in domains:
            continue
        source_text = f"{source.title} {source.quote}"
        if (
            answer_terms
            and not any(term in source_text for term in answer_terms)
        ):
            continue
        selected.append(source)
        domains.add(host)
    if len(domains) < 2:
        return []
    if not any(
        _is_authority_domain(urlparse(item.url).netloc)
        for item in selected
    ):
        return []
    return selected[:4]


async def _verify_sources(
    candidates: list[GroundedSource],
    *,
    answer: str,
    trust_env: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[GroundedSource]:
    verified: list[GroundedSource] = []
    answer_terms = _important_terms(answer)
    async with httpx.AsyncClient(
        follow_redirects=True,
        trust_env=trust_env,
        timeout=httpx.Timeout(12.0),
        transport=transport,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        },
    ) as client:
        for source in candidates[:6]:
            try:
                response = await client.get(source.url)
                response.raise_for_status()
                text = _html_to_text(response.text)
            except httpx.HTTPError:
                continue
            if not text:
                continue
            if answer_terms and not any(term in text for term in answer_terms):
                continue
            quote_terms = _important_terms(source.quote)
            if quote_terms and not any(term in text for term in quote_terms):
                continue
            verified.append(source)
            if len({urlparse(item.url).netloc for item in verified}) >= 3:
                break
    return verified


def _is_trusted_domain(netloc: str) -> bool:
    host = netloc.lower().split(":", 1)[0]
    return any(
        host == domain or host.endswith("." + domain)
        for domain in TRUSTED_DOMAINS
    )


def _is_authority_domain(netloc: str) -> bool:
    host = netloc.lower().split(":", 1)[0]
    return any(
        host == domain or host.endswith("." + domain)
        for domain in AUTHORITY_DOMAINS
    )


def _important_terms(answer: str) -> set[str]:
    compact = re.sub(
        r"[^\u4e00-\u9fffA-Za-z0-9]",
        "",
        answer,
    )
    result: set[str] = set()
    result.update(
        compact[index : index + 2]
        for index in range(max(0, len(compact) - 1))
        if len(compact[index : index + 2]) == 2
    )
    result.update(
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]{3,}", answer)
    )
    return result


def _html_to_text(value: str) -> str:
    value = re.sub(
        r"(?is)<(script|style|noscript).*?>.*?</\1>",
        " ",
        value,
    )
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


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
