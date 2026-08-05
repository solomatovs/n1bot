"""Tool web_grep: поиск по содержимому веб-страницы."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.web.connection import WebConnection
from boba.toolkit.launcher import RowCollector
from boba.toolkit.result import TableResult
from boba.web.caller import WebCaller
from boba.web.protocol import WebGrepRequest, WebGrepRow

__all__ = ["WebGrepConfig", "web_grep"]


class WebGrepConfig(WebConnection):
    """Config web-инструментов: профили, лимиты выдачи и песочница запроса."""

    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match.",
    )
    max_result_chars: int = Field(
        default=1_000_000,
        ge=1,
        description="Потолок суммарного объёма потока результата (символов).",
    )


class WebGrepNote:
    """Человекочитаемая сводка поиска для LLM."""

    @staticmethod
    def build(url: str, rows: list[dict[str, Any]], *, limit: int) -> str:
        if not rows:
            return f"url={url}: совпадений не найдено"
        parts = [f"url={url}", f"совпадений: {len(rows)}"]
        if len(rows) >= limit:
            parts.append(f"показаны первые {len(rows)} (найдено больше)")
        return "; ".join(parts)


def web_grep(  # noqa: PLR0913
    cfg: WebGrepConfig,
    caller: WebCaller,
    url: Annotated[
        str,
        Field(min_length=1, description="URL для скачивания."),
    ],
    pattern: Annotated[
        str,
        Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
    ],
    as_markdown: Annotated[
        bool,
        Field(description="true — искать по HTML→Markdown-конверсии, иначе по HTML."),
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
    """Найти совпадения pattern в содержимом страницы."""
    profile = cfg.resolve_profile(url)
    request = WebGrepRequest(
        op=WebGrepRequest.OP,
        url=url,
        profile=WebCaller.transport_of(profile),
        as_markdown=as_markdown,
        pattern=pattern,
        case_insensitive=case_insensitive,
        context=context,
        limit=limit,
        fixed_string=fixed_string,
        max_text_chars=cfg.max_text_chars,
    )
    collector = RowCollector(max_chars=cfg.max_result_chars, limit_rows=limit)
    caller.grep(request, collector)
    rows: list[dict[str, Any]] = []
    for raw in collector.rows():
        rows.append(WebGrepRow.model_validate(raw).model_dump())
    note = WebGrepNote.build(url, rows, limit=limit)
    return TableResult(rows=rows, note=note, metadata={"url": url})
