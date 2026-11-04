from __future__ import annotations

from qq_ai_bot.web_reader import extract_article


def test_extract_article_from_html() -> None:
    html = """
    <html>
      <head><title>测试文章</title></head>
      <body>
        <article>
          <h1>测试文章</h1>
          <p>这是网页正文，用来验证正文提取功能。</p>
          <p>第二段包含一个重要结论。</p>
        </article>
      </body>
    </html>
    """
    article = extract_article(html, "https://example.com/a")
    assert article is not None
    assert article.title == "测试文章"
    assert "正文提取功能" in article.text
