"""Tool confluence_grep_page + ConfluenceGrepPageConfig.

Скачивает одну Confluence-страницу в потоке (как confluence_fetch_page —
мимо attachment-fan-out'а, напрямую request_source -> ConfluenceHttpTransport ->
ConfluenceJsonDecoder) и применяет к её контенту grep-поиск с теми же
семантиками, что и file-tool grep: regex/fixed_string, регистр, контекст
до/после, limit, обрезка длинных строк по max_text_chars.

В отличие от confluence_fetch_page, возвращает не весь контент, а только
совпадения с номерами строк — экономит контекст LLM на больших страницах.

Config-секция: [tool.kb.confluence.grep].
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from itertools import islice
from typing import Annotated, Any, Literal

import httpx
import markdownify
from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.agent.tools.confluence.connection import ConfluenceConnection
from boba.chainlit2.agent.tools.confluence.parsing import ConfluenceJsonDecoder
from boba.chainlit2.agent.tools.confluence.pipeline import ConfluenceHttpTransport
from boba.chainlit2.agent.tools.confluence.request_sources import (
    ConfluencePagesRequestSource,
)
from boba.chainlit2.agent.tools.http import CancellableHttpTransport
from boba.chainlit2.rendering.tool_result import TableResult
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceGrepPageConfig", "confluence_grep_page"]


class ConfluenceGrepPageConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_grep_page."""

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match.",
    )


class PageGrep:
    """Grep-движок поверх in-memory текста страницы (regex + контекст)."""

    @staticmethod
    def compile_pattern(
        pattern: str, *, fixed_string: bool, case_insensitive: bool
    ) -> re.Pattern[str]:
        """
        Компилирует pattern; fixed_string -> литерал,
        иначе Python-regex с literal-fallback при некорректном regex
        """
        flags = re.IGNORECASE if case_insensitive else 0
        if fixed_string:
            return re.compile(re.escape(pattern), flags)
        try:
            return re.compile(pattern, flags)
        except re.error:
            # Некорректный regex (часто LLM шлёт голый '?', '(', '+') -> ищем литерально
            return re.compile(re.escape(pattern), flags)

    @staticmethod
    def iter_matches(
        text: str, compiled: re.Pattern[str], *, context: int
    ) -> Iterator[dict[str, Any]]:
        """Построчный поиск; yield в порядке строк, с before/after-контекстом."""
        before: deque[str] = deque(maxlen=context if context > 0 else 0)
        pending: list[dict[str, Any]] = []
        for i, line in enumerate(text.splitlines(), start=1):
            still_pending: list[dict[str, Any]] = []
            for p in pending:
                p["after"].append(line)
                if len(p["after"]) >= context:
                    yield p
                else:
                    still_pending.append(p)
            pending = still_pending
            if compiled.search(line):
                if context == 0:
                    yield {"line": i, "content": line, "before": [], "after": []}
                else:
                    pending.append(
                        {
                            "line": i,
                            "content": line,
                            "before": list(before),
                            "after": [],
                        }
                    )
            before.append(line)
        yield from pending

    @staticmethod
    def note(
        page_id: str, items: list[dict[str, Any]], *, truncated: bool, max_chars: int
    ) -> str:
        """Footer-контекст под таблицей: page_id, число совпадений, усечения."""
        if not items:
            return f"page_id={page_id}: совпадений не найдено"
        parts = [f"page_id={page_id}", f"совпадений: {len(items)}"]
        if truncated:
            parts.append(f"показаны первые {len(items)} (найдено больше)")
        if any(it.get("truncated_lines") for it in items):
            parts.append(f"строки усечены до {max_chars} символов")
        return "; ".join(parts)

    @staticmethod
    def clip(s: str, limit: int) -> tuple[str, bool]:
        """Обрезает строку до limit символов; возвращает (строка, был_ли_обрезан)."""
        if len(s) <= limit:
            return s, False
        return s[:limit], True

    @staticmethod
    def clip_many(lines: list[str], limit: int) -> tuple[list[str], bool]:
        """Применяет clip к каждой строке списка."""
        out: list[str] = []
        cut = False
        for line in lines:
            clipped, c = PageGrep.clip(line, limit)
            out.append(clipped)
            cut = cut or c
        return out, cut


def confluence_grep_page(  # noqa: PLR0913 — независимые флаги grep'а
    cfg: ConfluenceGrepPageConfig,
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "ID страницы Confluence (из URL `viewpage.action?pageId=<id>`). "
                "Attachment'ы не скачиваются."
            ),
        ),
    ],
    pattern: Annotated[
        str,
        Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Искать по Markdown-конверсии (`markdownify`, ATX-заголовки) "
                "вместо исходного Confluence-HTML. По умолчанию true."
            ),
        ),
    ] = True,
    case_insensitive: Annotated[
        bool,
        Field(description="Игнорировать регистр. По умолчанию false."),
    ] = False,
    context: Annotated[
        int,
        Field(ge=0, description="Строк контекста до и после каждого совпадения."),
    ] = 0,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум совпадений в ответе. По умолчанию 100."),
    ] = 100,
    fixed_string: Annotated[
        bool,
        Field(description="Литеральный поиск без regex. По умолчанию false."),
    ] = False,
) -> TableResult:
    """Скачивает Confluence-страницу и ищет в её контенте совпадения pattern.

    Возвращает TableResult — таблицу matches с колонками line/content/
    before/after (+truncated_lines на усечённых). Контекст усечения,
    page_id и переполнение limit — в note/metadata. Длинные строки режутся
    по max_text_chars.
    """
    compiled = PageGrep.compile_pattern(
        pattern,
        fixed_string=fixed_string,
        case_insensitive=case_insensitive,
    )

    conn = ConfluenceConnection(profile=cfg.confluence, body_format=cfg.body_format)
    request_source = ConfluencePagesRequestSource(
        base_url=conn.base_url,
        page_ids=[page_id],
        body_format=conn.body_format,
    )
    decoder = ConfluenceJsonDecoder(body_format=conn.body_format)

    text: str | None = None
    try:
        with CancellableHttpTransport(conn.profile) as http:
            transport = ConfluenceHttpTransport(http)
            for req in request_source.requests():
                for raw in transport.fetch(req):
                    decoded = decoder.decode(raw)
                    html = decoded.handle.read().decode("utf-8", errors="replace")
                    text = (
                        markdownify.markdownify(html, heading_style="ATX")
                        if as_markdown
                        else html
                    )
                    break
                if text is not None:
                    break
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence fetch failed: {type(e).__name__}: {e}",
        ) from e

    if text is None:
        raise RuntimeError(
            f"Confluence grep returned no page for page_id={page_id!r}",
        )

    found = PageGrep.iter_matches(text, compiled, context=context)
    matches = list(islice(found, limit + 1))
    total = len(matches)
    truncated = total > limit
    if truncated:
        matches = matches[:limit]

    max_chars = cfg.max_text_chars
    items: list[dict[str, Any]] = []
    for m in matches:
        content, cut_c = PageGrep.clip(m["content"], max_chars)
        before, cut_b = PageGrep.clip_many(m["before"], max_chars)
        after, cut_a = PageGrep.clip_many(m["after"], max_chars)
        item: dict[str, Any] = {
            "line": m["line"],
            "content": content,
            "before": before,
            "after": after,
        }
        if cut_c or cut_b or cut_a:
            item["truncated_lines"] = True
        items.append(item)

    note = PageGrep.note(page_id, items, truncated=truncated, max_chars=max_chars)
    return TableResult(rows=items, note=note, metadata={"page_id": page_id})
