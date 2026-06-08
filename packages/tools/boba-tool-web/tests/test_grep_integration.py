"""web_grep на реальных данных из config (pytest -m integration).

Ходит в whitelist-хосты [tool.web].profiles (raw.githubusercontent.com,
cwiki.apache.org). URL'ы запинованы по тегу/коммиту, чтобы контент не плавал.
Default-режим (-m "not integration") эти тесты исключает.
"""

from __future__ import annotations

import pytest

from boba.tool.web.tools.grep import WebGrepConfig, web_grep
from boba.tools.domain import TableResult

pytestmark = pytest.mark.integration

# Запинованный по тегу README cpython — immutable, всегда содержит "Python".
RAW_README = "https://raw.githubusercontent.com/python/cpython/v3.12.0/README.rst"


def test_returns_table_with_matches(web_grep_cfg: WebGrepConfig) -> None:
    """Реальный grep по raw-тексту -> TableResult с непустыми matches."""
    res = web_grep(
        cfg=web_grep_cfg,
        url=RAW_README,
        pattern="Python",
        as_markdown=False,
        case_insensitive=False,
        context=0,
        limit=100,
        fixed_string=False,
    )
    assert isinstance(res, TableResult)
    assert len(res.rows) >= 1
    assert set(res.rows[0]) >= {"line", "content", "before", "after"}
    assert all("Python" in r["content"] for r in res.rows)
    assert res.metadata == {"url": RAW_README}


def test_line_numbers_strictly_increasing(web_grep_cfg: WebGrepConfig) -> None:
    """line — 1-based номер исходной строки, монотонно растёт по matches."""
    res = web_grep(
        cfg=web_grep_cfg,
        url=RAW_README,
        pattern="Python",
        as_markdown=False,
        case_insensitive=False,
        context=0,
        limit=100,
        fixed_string=False,
    )
    lines = [r["line"] for r in res.rows]
    assert lines[0] >= 1
    assert lines == sorted(lines)
    assert len(lines) == len(set(lines))
    # README начинается со строки "This is Python version ..."
    assert res.rows[0]["line"] == 1


def test_case_insensitive_widens_matches(web_grep_cfg: WebGrepConfig) -> None:
    """case_insensitive=true ловит больше (или столько же) строк, чем точный."""
    common = {
        "url": RAW_README,
        "pattern": "python",
        "as_markdown": False,
        "context": 0,
        "limit": 500,
        "fixed_string": False,
    }
    sensitive = web_grep(cfg=web_grep_cfg, case_insensitive=False, **common)
    insensitive = web_grep(cfg=web_grep_cfg, case_insensitive=True, **common)
    assert len(insensitive.rows) >= len(sensitive.rows)
    assert all("python" in r["content"].lower() for r in insensitive.rows)


def test_fixed_string_treats_pattern_literally(web_grep_cfg: WebGrepConfig) -> None:
    """fixed_string=true: точки в '3.12.0' — литералы, не 'любой символ'."""
    res = web_grep(
        cfg=web_grep_cfg,
        url=RAW_README,
        pattern="3.12.0",
        as_markdown=False,
        case_insensitive=False,
        context=0,
        limit=10,
        fixed_string=True,
    )
    assert len(res.rows) >= 1
    assert all("3.12.0" in r["content"] for r in res.rows)


def test_context_attaches_before_and_after(web_grep_cfg: WebGrepConfig) -> None:
    """context=2 навешивает до 2 строк до/после; не больше запрошенного."""
    res = web_grep(
        cfg=web_grep_cfg,
        url=RAW_README,
        pattern="Python",
        as_markdown=False,
        case_insensitive=False,
        context=2,
        limit=20,
        fixed_string=False,
    )
    assert any(r["before"] or r["after"] for r in res.rows)
    for r in res.rows:
        assert len(r["before"]) <= 2
        assert len(r["after"]) <= 2


def test_limit_caps_rows_and_marks_overflow(web_grep_cfg: WebGrepConfig) -> None:
    """limit=N обрезает до N строк и пишет про переполнение в note."""
    res = web_grep(
        cfg=web_grep_cfg,
        url=RAW_README,
        pattern="Python",
        as_markdown=False,
        case_insensitive=True,
        context=0,
        limit=2,
        fixed_string=False,
    )
    assert len(res.rows) == 2
    assert res.note is not None
    assert "найдено больше" in res.note


def test_no_match_returns_empty_table(web_grep_cfg: WebGrepConfig) -> None:
    """Несуществующий pattern -> пустые rows и note 'совпадений не найдено'."""
    res = web_grep(
        cfg=web_grep_cfg,
        url=RAW_README,
        pattern="zzz_nonexistent_token_qqq",
        as_markdown=False,
        case_insensitive=False,
        context=0,
        limit=100,
        fixed_string=True,
    )
    assert res.rows == []
    assert res.note is not None
    assert "не найдено" in res.note


def test_markdown_conversion_finds_heading(web_grep_cfg: WebGrepConfig) -> None:
    """as_markdown=True: HTML cwiki -> Markdown, grep по сконвертированному."""
    res = web_grep(
        cfg=web_grep_cfg,
        url="https://cwiki.apache.org/confluence/",
        pattern="Apache",
        as_markdown=True,
        case_insensitive=True,
        context=0,
        limit=5,
        fixed_string=False,
    )
    assert len(res.rows) >= 1
    assert all("apache" in r["content"].lower() for r in res.rows)


def test_unwhitelisted_host_rejected_before_http(
    web_grep_cfg: WebGrepConfig,
) -> None:
    """Хост вне [tool.web].profiles -> ValueError ещё до HTTP-запроса."""
    with pytest.raises(ValueError, match="whitelist") as exc_info:
        web_grep(
            cfg=web_grep_cfg,
            url="https://evil.example.com/x",
            pattern="anything",
            as_markdown=False,
            case_insensitive=False,
            context=0,
            limit=10,
            fixed_string=False,
        )
    assert "evil.example.com" in str(exc_info.value)
