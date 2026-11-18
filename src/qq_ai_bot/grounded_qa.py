from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
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
    "chinanews.com",
    "chinagate.cn",
    "gov.cn",
    "edu.cn",
)
AUTHORITY_DOMAINS = (
    "baike.baidu.com",
    "wikipedia.org",
    "xinhuanet.com",
    "people.com.cn",
    "cctv.com",
    "chinanews.com",
    "chinagate.cn",
    "gov.cn",
    "edu.cn",
)


@dataclass(frozen=True)
class GroundedSource:
    title: str
    url: str
    quote: str
    page_text: str = ""


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
                f"{index}. {source.title}：{source.url}"
                for index, source in enumerate(self.sources[:4], start=1)
            )
        return "\n".join(lines)


async def grounded_lookup(
    chat_client: ChatClient,
    *,
    model: str,
    question: str,
    trust_env: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    force_web_fetch: bool = False,
    current_date: str | None = None,
) -> GroundedAnswer:
    local_date = current_date or datetime.now().astimezone().date().isoformat()
    music_result = await music_catalog_lookup(
        question,
        trust_env=trust_env,
        transport=transport,
    )
    if music_result is not None:
        return music_result
    # Once the caller has classified a request as needing live information,
    # fetch real result pages first instead of relying on a provider's optional
    # "search" flag, which can return no sources at all.
    search_pages: list[GroundedSource] = []
    if force_web_fetch or _explicit_search_request(question):
        search_pages = await _search_web_pages(
            question,
            trust_env=trust_env,
            transport=transport,
        )
    if len(search_pages) >= 2 or _allows_single_live_source(
        question,
        search_pages,
    ):
        searched = await _answer_from_search_pages(
            chat_client,
            model=model,
            question=question,
            sources=search_pages,
            current_date=local_date,
        )
        if searched is not None:
            return searched
    reason = "没有获得可核验的事实答案。"
    unverified_answer = ""
    unverified_sources: list[GroundedSource] = []
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
        if answer and candidates:
            unverified_answer = answer
            unverified_sources = candidates[:4]
        if len({urlparse(item.url).netloc.lower() for item in candidates}) < 2:
            reason = "检索结果缺少两个不同域名的可信来源，暂不下结论。"
            continue
        verified, allow_domain_fallback = await _verify_sources(
            candidates,
            answer=answer,
            trust_env=trust_env,
            transport=transport,
        )
        if (
            len({urlparse(item.url).netloc for item in verified}) < 2
            or (
                _requires_authority_sources(question)
                and not any(
                    _is_authority_domain(urlparse(item.url).netloc)
                    for item in verified
                )
            )
        ):
            accepted = (
                _cross_domain_fallback(
                    candidates,
                    answer=answer,
                    require_authority=_requires_authority_sources(question),
                )
                if allow_domain_fallback
                else []
            )
            if accepted:
                organized = await _organize_grounded_answer(
                    chat_client,
                    model=model,
                    question=question,
                    answer=answer,
                    sources=accepted,
                )
                return GroundedAnswer(
                    answer=organized or answer,
                    sources=tuple(accepted),
                    verified=True,
                    verification_level="domain_cross_checked",
                )
            reason = "来源页面无法访问或缺少权威交叉来源，暂不下结论。"
            continue
        organized = await _organize_grounded_answer(
            chat_client,
            model=model,
            question=question,
            answer=answer,
            sources=verified,
        )
        return GroundedAnswer(
            answer=organized or answer,
            sources=tuple(verified),
            verified=True,
            verification_level="page_verified",
        )
    if search_pages:
        return GroundedAnswer(
            answer=(
                "我联网查到了这些网页，但这次没整理出足够可靠的结论。"
                "先把实际检索到的来源发你，避免假装搜过。"
            ),
            sources=tuple(search_pages[:4]),
            verified=False,
            reason="联网检索已执行，但没有完成交叉核验。",
            verification_level="live_search_pages_unverified",
        )
    if unverified_answer:
        return GroundedAnswer(
            answer=unverified_answer,
            sources=tuple(unverified_sources),
            verified=False,
            reason=reason,
            verification_level="source_links_unverified",
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
    wants_summary = _wants_organized_answer(question)
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
        + (
            "用户本次明确要求整理、总结、汇总、列出几条或询问新闻。"
            "answer 必须给出 2 到 5 条可读的要点，说明时间范围和关键结论，"
            "不要只回一句话。"
            if wants_summary
            else "answer 保持一到三句，直接回答，不写空话。"
        )
        + f"{extra}\n"
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
            or _is_low_quality_search_domain(parsed.netloc)
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
    require_authority: bool = True,
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
    if require_authority and not any(
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
) -> tuple[list[GroundedSource], bool]:
    verified: list[GroundedSource] = []
    allow_domain_fallback = bool(candidates)
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
                text = _html_to_text(_decode_html_response(response))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {401, 403, 429}:
                    allow_domain_fallback = False
                continue
            except (httpx.HTTPError, ValueError):
                allow_domain_fallback = False
                continue
            if not text:
                allow_domain_fallback = False
                continue
            if answer_terms and not any(term in text for term in answer_terms):
                allow_domain_fallback = False
                continue
            quote_terms = _important_terms(source.quote)
            if quote_terms and not any(term in text for term in quote_terms):
                allow_domain_fallback = False
                continue
            verified.append(
                GroundedSource(
                    title=source.title,
                    url=source.url,
                    quote=source.quote,
                    page_text=text[:12000],
                )
            )
            if len({urlparse(item.url).netloc for item in verified}) >= 3:
                break
    return verified, allow_domain_fallback


def _explicit_search_request(question: str) -> bool:
    return is_explicit_search_request(question)


def is_explicit_search_request(question: str) -> bool:
    """Return true when the user clearly asks the bot to use the web."""
    value = re.sub(r"\s+", "", question.lower())
    direct_markers = (
        "联网搜索",
        "联网查",
        "联网找",
        "联网看看",
        "联网浏览",
        "联网检索",
        "上网查",
        "上网搜",
        "上网看看",
        "网上查",
        "网上搜",
        "网上看看",
        "帮我搜",
        "帮我查",
        "给我搜",
        "给我查",
        "替我搜",
        "替我查",
        "搜索一下",
        "搜索下",
        "搜索看看",
        "搜一下",
        "搜下看看",
        "搜一搜",
        "搜搜看",
        "检索一下",
        "查一下",
        "查下",
        "查一查",
        "查查",
        "现查",
        "去查",
        "去搜",
        "先搜索",
        "先联网",
        "用网络查",
        "用互联网查",
        "查资料",
        "查最新",
        "查新闻",
    )
    if any(marker in value for marker in direct_markers):
        return True
    if re.match(
        r"^(?:请|麻烦(?:你)?|你)?(?:帮我|给我|替我)?"
        r"(?:先|马上|直接)?搜索(?:一下|下|看看)?",
        value,
    ) and not re.match(
        r"^(?:搜索算法|搜索引擎|搜索功能|搜索技术|搜索框|"
        r"搜索记录|搜索历史|搜索排序|搜索建议)",
        value,
    ):
        return True
    return bool(
        re.search(
            r"(?:联网|上网|网上|互联网|网络)"
            r".{0,10}(?:查|搜|找|看|浏览|检索|核实|验证)",
            value,
        )
        or re.search(
            r"(?:帮我|给我|替我|你去|你来|直接|马上|现在|先)"
            r".{0,6}(?:查|搜|找|浏览|检索)",
            value,
        )
    )


async def _search_web_pages(
    question: str,
    *,
    trust_env: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[GroundedSource]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(
        follow_redirects=True,
        trust_env=trust_env,
        timeout=httpx.Timeout(15.0),
        transport=transport,
        headers=headers,
    ) as client:
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for search_query in _search_queries(question):
            try:
                response = await client.get(
                    "https://www.so.com/s",
                    params={"q": search_query},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            for match in re.finditer(
                r'<a[^>]+data-mdurl="([^"]+)"[^>]*>(.*?)</a>',
                response.text,
                re.S | re.I,
            ):
                url = html.unescape(match.group(1)).strip()
                title = re.sub(
                    r"\s+",
                    " ",
                    html.unescape(re.sub(r"<[^>]+>", " ", match.group(2))),
                ).strip()
                host = urlparse(url).netloc.lower()
                if (
                    not title
                    or not url.startswith(("http://", "https://"))
                    or host in {"www.so.com", "so.com"}
                    or host.endswith("360.com")
                    or host.endswith("360kan.com")
                    or host.endswith("bing.com")
                    or _is_low_quality_search_domain(host)
                    or url in seen
                ):
                    continue
                seen.add(url)
                candidates.append((title[:240], url))
        candidates.sort(
            key=lambda item: 0
            if _is_authority_domain(urlparse(item[1]).netloc)
            else 1
        )
        if _requires_authority_sources(question):
            authority_candidates = [
                item
                for item in candidates
                if _is_authority_domain(urlparse(item[1]).netloc)
            ]
            if len(authority_candidates) >= 2:
                candidates = authority_candidates

        semaphore = asyncio.Semaphore(4)

        async def load(item: tuple[str, str]) -> GroundedSource | None:
            title, url = item
            async with semaphore:
                try:
                    page = await client.get(url)
                    page.raise_for_status()
                    text = _html_to_text(_decode_html_response(page))
                except (httpx.HTTPError, ValueError):
                    return None
            if len(text) < 200:
                return None
            return GroundedSource(
                title=title,
                url=str(page.url),
                quote=title,
                page_text=text[:12000],
            )

        loaded = await asyncio.gather(
            *(load(item) for item in candidates[:10])
        )
        seen_hosts: set[str] = set()
        result: list[GroundedSource] = []
        for source in loaded:
            if source is None:
                continue
            host = urlparse(source.url).netloc.lower()
            if host in seen_hosts:
                continue
            seen_hosts.add(host)
            result.append(source)
            if len(result) >= 6:
                break
        return result


def _clean_search_query(question: str) -> str:
    value = question.strip()
    # Strip the complete request wrapper before replacing shorter markers below.
    # Otherwise "你联网查一下今天..." first becomes "你 今天...", leaving the
    # pronoun embedded in the actual search query.
    value = re.sub(
        r"^(?:请|麻烦|你|能不能|可以)?(?:先|马上|直接)?"
        r"(?:联网|上网|网上|用互联网|用网络)?"
        r"(?:帮我|给我|替我)?"
        r"(?:搜索|搜|查|找|浏览|检索)"
        r"(?:一下|下|看看|一查|查)?"
        r"[：:，,\s]*",
        "",
        value,
    )
    for marker in (
        "帮我联网搜索",
        "帮我上网查",
        "帮我网上查",
        "联网搜索",
        "联网查一下",
        "联网查",
        "联网找",
        "上网查一下",
        "上网查",
        "网上查一下",
        "网上查",
        "帮我搜一下",
        "搜索一下",
        "搜一下",
        "现查",
    ):
        value = value.replace(marker, " ")
    # Remove any remaining leading politeness/network wrappers without treating
    # query words such as "下载" as request particles.
    wrapper = re.compile(
        r"^(?:请|麻烦(?:你)?|你|能不能|可以|帮我|给我|替我|"
        r"联网|上网|网上|用互联网|用网络|先|马上|直接|"
        r"[：:，,\s]+)+"
    )
    previous = None
    while previous != value:
        previous = value
        value = wrapper.sub("", value, count=1)
    value = re.sub(
        r"[，,\s]*(?:再)?(?:给我|把)?(?:来源|出处|链接).*$",
        "",
        value,
    )
    value = re.sub(r"(?:然后|之后再|再).*$", "", value)
    return re.sub(r"\s+", " ", value).strip() or question


def _search_queries(question: str) -> list[str]:
    clean = _clean_search_query(question)
    queries = [clean]
    if _requires_authority_sources(clean) and "site:gov.cn" not in clean:
        queries.append(f"{clean} site:gov.cn")
    return queries


def _requires_authority_sources(question: str) -> bool:
    return any(
        marker in question
        for marker in (
            "政策",
            "法规",
            "政府",
            "通知",
            "新闻",
            "时事",
            "时政",
        )
    )


async def _answer_from_search_pages(
    chat_client: ChatClient,
    *,
    model: str,
    question: str,
    sources: list[GroundedSource],
    current_date: str,
) -> GroundedAnswer | None:
    single_source_allowed = _allows_single_live_source(question, sources)
    materials = "\n\n".join(
        (
            f"来源编号：{index}\n"
            f"标题：{source.title}\n"
            f"URL：{source.url}\n"
            f"网页正文：{source.page_text[:8000]}"
        )
        for index, source in enumerate(sources, start=1)
    )
    summary = _wants_organized_answer(question)
    answer_instruction = (
        "answer 用 2 到 5 条编号要点整理，每条一到两句，"
        "说明时间范围和关键结论。"
        if summary
        else "answer 用一到三句话直接回答。"
    )
    source_instruction = (
        "从下面唯一的可信实时数据源中提取答案，并在结论后标注 [来源1]。"
        if single_source_allowed
        else "从下面来源中选择至少两个能支撑答案的编号。"
    )
    try:
        reply = await chat_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是联网资料整理器。只根据用户提供的真实网页正文回答，"
                        "不得补充正文没有的事实，不得编造链接。"
                        "政策、法规、日期和数字必须由官方政府或官方媒体正文明确支持；"
                        "自媒体标题和观点不能当成政策事实。"
                        "每条结论末尾用 [来源编号] 标注依据。"
                        "只返回 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"用户问题：{question}\n"
                        f"当前日期：{current_date}\n"
                        f"要求：{answer_instruction}\n"
                        f"{source_instruction}"
                        '返回格式：{"answer":"整理后的答案",'
                        '"source_ids":[1,2]}。\n\n'
                        f"{materials}"
                    ),
                },
            ],
            model=model,
            extra_payload={
                "reasoning_effort": "none",
                "response_format": {"type": "json_object"},
            },
        )
    except Exception:
        return None
    data = _parse_json(reply)
    answer = str(data.get("answer") or "").strip()
    if not answer:
        return None
    selected: list[GroundedSource] = []
    raw_ids = data.get("source_ids")
    if isinstance(raw_ids, list):
        for value in raw_ids:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(sources):
                source = sources[index - 1]
                if source not in selected:
                    selected.append(source)
    if len(selected) < 2 and not single_source_allowed:
        answer_terms = _important_terms(answer)
        selected = [
            source
            for source in sources
            if any(term in source.page_text for term in answer_terms)
        ][:4]
    if single_source_allowed and not selected:
        selected = sources[:1]
    domains = {urlparse(item.url).netloc.lower() for item in selected}
    if len(domains) < 2 and not single_source_allowed:
        return None
    if (
        _requires_authority_sources(question)
        and not any(
            _is_authority_domain(urlparse(item.url).netloc)
            for item in selected
        )
    ):
        return None
    stale_date = _stale_today_date(question, answer, current_date)
    if stale_date:
        return GroundedAnswer(
            answer=(
                f"我联网查到的页面标注的是 {stale_date}，不是今天"
                f"（{current_date}）。这份页面可能还没更新，不能拿它冒充"
                "今天的赛程。"
            ),
            sources=tuple(selected[:4]),
            verified=False,
            reason="联网页面日期与今天不一致。",
            verification_level="stale_live_source",
        )
    if summary and not re.search(r"(?m)^\s*1[.、]", answer):
        organized = await _organize_grounded_answer(
            chat_client,
            model=model,
            question=question,
            answer=answer,
            sources=selected,
        )
        answer = organized or answer
    return GroundedAnswer(
        answer=answer,
        sources=tuple(selected[:4]),
        verified=True,
        verification_level="live_search_pages",
    )


def _allows_single_live_source(
    question: str,
    sources: list[GroundedSource],
) -> bool:
    if len(sources) != 1:
        return False
    host = urlparse(sources[0].url).netloc.lower()
    return host in {"okooo.com", "www.okooo.com"} and any(
        marker in question
        for marker in ("赛程", "比分", "比赛", "对阵")
    )


def _stale_today_date(
    question: str,
    answer: str,
    current_date: str,
) -> str:
    if not any(marker in question for marker in ("今天", "今日")):
        return ""
    normalized_current = current_date.replace("-", "")
    dates: list[str] = []
    for value in re.findall(
        r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})",
        answer,
    ):
        year, month, day = value
        dates.append(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
    dates = list(dict.fromkeys(dates))
    if not dates:
        return ""
    if any(value.replace("-", "") == normalized_current for value in dates):
        return ""
    return dates[0]


def _wants_organized_answer(question: str) -> bool:
    return any(
        marker in question
        for marker in (
            "整理",
            "总结",
            "汇总",
            "列出",
            "几条",
            "详细",
            "介绍",
            "新闻",
        )
    )


async def _organize_grounded_answer(
    chat_client: ChatClient,
    *,
    model: str,
    question: str,
    answer: str,
    sources: list[GroundedSource],
) -> str:
    if not _wants_organized_answer(question):
        return ""
    materials = "\n\n".join(
        (
            f"来源 {index}：{source.title}\n"
            f"URL：{source.url}\n"
            f"摘录：{source.quote}\n"
            f"页面正文：{(source.page_text or source.quote)[:12000]}"
        )
        for index, source in enumerate(sources[:4], start=1)
    )
    try:
        reply = await chat_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "根据已核验的网页资料整理答案。只使用提供的资料，"
                        "不补充资料外的事实，不编造链接。用简体中文输出，"
                        "用 1. 2. 3. 编号，每条一到两句；不要写来源列表，"
                        "保留原文中的 [来源编号] 标记并核对编号，"
                        "不要写 Markdown 标题或代码块。资料不足时明确说明。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"用户要求：{question}\n\n"
                        f"初步结论：{answer}\n\n"
                        f"可核验资料：\n{materials}"
                    ),
                },
            ],
            model=model,
            extra_payload={"reasoning_effort": "none"},
        )
    except Exception:
        return ""
    value = reply.strip()
    if not value:
        return ""
    return value


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


def _is_low_quality_search_domain(netloc: str) -> bool:
    """Filter generated SEO pages that imitate current sports/news data."""
    host = netloc.lower().split(":", 1)[0]
    label = host.split(".", 1)[0]
    keyword_tokens = (
        "zuqiu",
        "saicheng",
        "bisai",
        "jinri",
        "yilan",
        "chaxun",
        "zhibo",
        "bifen",
    )
    token_count = sum(token in label for token in keyword_tokens)
    return (
        token_count >= 2
        or (token_count >= 1 and len(label) >= 16)
        or bool(re.search(r"[a-z]{16,}\d+[a-z0-9]*", label))
        or label.startswith(("zuqiujinri", "jinrizuqi", "zuqusaicheng"))
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


def _decode_html_response(response: httpx.Response) -> str:
    raw = response.content
    declared = re.search(
        br"<meta[^>]+charset\s*=\s*[\"']?([a-zA-Z0-9_.-]+)",
        raw[:16384],
        re.IGNORECASE,
    )
    encodings = [
        declared.group(1).decode("ascii", "ignore") if declared else "",
        response.charset_encoding or "",
        "utf-8",
        "gb18030",
    ]
    best = ""
    best_score = None
    for encoding in dict.fromkeys(item for item in encodings if item):
        try:
            text = raw.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
        score = text.count("\ufffd")
        if best_score is None or score < best_score:
            best = text
            best_score = score
        if score == 0:
            break
    return best or raw.decode("utf-8", errors="replace")


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
