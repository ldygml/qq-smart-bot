from __future__ import annotations

from qq_ai_bot.qq_format import format_qq_text


def test_markdown_is_converted_to_plain_qq_text() -> None:
    source = """# 解题过程

**答案：96**

- 第一步：展开
- 第二步：相加

| 项目 | 结果 |
| --- | --- |
| 计算 | 96 |

[资料](https://example.com)
"""
    result = format_qq_text(source)
    assert "【解题过程】" in result
    assert "答案：96" in result
    assert "**" not in result
    assert "• 第一步：展开" in result
    assert "项目  |  结果" in result
    assert "| --- |" not in result
    assert "资料（https://example.com）" in result


def test_multiplication_and_code_are_preserved() -> None:
    source = "12 * 8 = 96\n\n```python\nresult = 2 * 8\n```"
    result = format_qq_text(source)
    assert "12 * 8 = 96" in result
    assert "result = 2 * 8" in result
    assert "```" not in result
