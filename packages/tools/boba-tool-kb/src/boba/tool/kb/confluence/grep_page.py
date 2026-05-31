"""Tool `confluence_grep_page` + `ConfluenceGrepPageConfig`.

Скачивает одну Confluence-страницу в потоке (как `confluence_fetch_page` —
мимо attachment-fan-out'а, напрямую `request_source → http_transport →
ConfluenceJsonDecoder`) и применяет к её контенту grep-поиск с теми же
семантиками, что и file-tool `grep`: regex/fixed_string, регистр, контекст
до/после, limit, обрезка длинных строк по `max_text_chars`.

В отличие от `confluence_fetch_page`, возвращает не весь контент, а только
совпадения с номерами строк — экономит контекст LLM на больших страницах.

Config-секция: `[tool.kb.confluence.grep]`.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from itertools import islice
from typing import Annotated, Any, ClassVar

import httpx
import markdownify
from pydantic import Field

from boba.indexing import PipelineContext, PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.parsing import ConfluenceJsonDecoder
from boba.tool.kb.confluence.request_sources import ConfluencePagesRequestSource
from boba.tools import FromConfig, tool

__all__ = ["ConfluenceGrepPageConfig", "confluence_grep_page"]


class ConfluenceGrepPageConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_grep_page`."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.grep",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match.",
    )


class PageGrep:
    """Grep-движок поверх in-memory текста страницы (regex + контекст)."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("confluence.grep_page")

    @staticmethod
    def compile_pattern(
        pattern: str, *, fixed_string: bool, case_insensitive: bool
    ) -> re.Pattern[str]:
        """Компилирует pattern; fixed_string → литерал, иначе Python-regex."""
        raw = re.escape(pattern) if fixed_string else pattern
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            return re.compile(raw, flags)
        except re.error as e:
            raise RuntimeError(
                f"Некорректный regex {pattern!r}: {e.msg} в позиции {e.pos}; "
                f"экранируй спецсимволы или передай fixed_string=true.",
            ) from e

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
    def clip(s: str, limit: int) -> tuple[str, bool]:
        """Обрезает строку до `limit` символов; возвращает (строка, был_ли_обрезан)."""
        if len(s) <= limit:
            return s, False
        return s[:limit], True

    @staticmethod
    def clip_many(lines: list[str], limit: int) -> tuple[list[str], bool]:
        """Применяет `clip` к каждой строке списка."""
        out: list[str] = []
        cut = False
        for line in lines:
            clipped, c = PageGrep.clip(line, limit)
            out.append(clipped)
            cut = cut or c
        return out, cut


@tool
def confluence_grep_page(  # noqa: PLR0913 — независимые флаги grep'а
    cfg: Annotated[ConfluenceGrepPageConfig, FromConfig()],
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
) -> dict[str, Any]:
    """Скачивает Confluence-страницу и ищет в её контенте совпадения pattern.

    Формат результата — как у file-tool `grep`: список matches со
    `line`/`content`/`before`/`after`. При переполнении limit ответ обрезается
    с `truncated=true`. Длинные строки режутся по `max_text_chars`; на
    затронутом match ставится `truncated_lines=true`.
    """
    compiled = PageGrep.compile_pattern(
        pattern,
        fixed_string=fixed_string,
        case_insensitive=case_insensitive,
    )

    request_source = ConfluencePagesRequestSource(
        base_url=cfg.confluence.base_url,
        auth=cfg.confluence.make_auth(),
        page_ids=[page_id],
        body_format=cfg.confluence.body_format,
    )
    transport = cfg.confluence.make_transport()
    decoder = ConfluenceJsonDecoder(body_format=cfg.confluence.body_format)
    pctx = PipelineContext(pipeline_id=PageGrep.PIPELINE_ID)

    text: str | None = None
    try:
        for http_req in request_source.stream(pctx):
            for raw in transport.stream(pctx, [http_req]):
                decoded = decoder.convert(raw)
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

    return {
        "page_id": page_id,
        "matches": items,
        "count": len(items),
        "total": total,
        "truncated": truncated,
        "limit": limit,
        "max_text_chars": max_chars,
    }
