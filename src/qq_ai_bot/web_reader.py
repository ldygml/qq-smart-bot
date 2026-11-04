from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import trafilatura


class WebReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebArticle:
    url: str
    title: str
    text: str


class WebArticleReader:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        trust_env: bool = False,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            trust_env=trust_env,
        )

    async def read(self, url: str) -> WebArticle:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise WebReadError("网页地址必须以 http:// 或 https:// 开头。")
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise WebReadError("网页读取超时。") from exc
        except httpx.HTTPError as exc:
            raise WebReadError("网页读取失败。") from exc
        content_type = response.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type and "text" not in content_type:
            raise WebReadError("这个链接不是可解析的网页。")
        article = extract_article(response.text, url)
        if article is None:
            raise WebReadError("没有从网页中提取到正文。")
        return article

    async def close(self) -> None:
        await self._client.aclose()


def extract_article(html: str, url: str) -> WebArticle | None:
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if not text or not text.strip():
        return None
    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = (
        metadata.title.strip()
        if metadata and metadata.title
        else urlparse(url).netloc
    )
    return WebArticle(url=url, title=title, text=text.strip())
