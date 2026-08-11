"""Tool web_fetch: окно строк веб-страницы."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.web._grep import WebGrepConfig
from boba.toolkit.launcher import TextCollector
from boba.web.caller import WebCaller
from boba.web.protocol import WebFetchArgs

__all__ = ["WebFetchAnswer", "web_fetch"]


class WebFetchAnswer(BaseModel):
    """Ответ модели: голова окна строк и счётчики для пагинации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    source_url: str
    total_lines: int
    returned_lines: int


def web_fetch(  # noqa: PLR0913
    cfg: WebGrepConfig,
    caller: WebCaller,
    url: Annotated[
        str,
        Field(min_length=1, description="URL для скачивания"),
    ],
    as_markdown: Annotated[
        bool,
        Field(description="true — конвертирует HTML->Markdown"),
    ],
    line_offset: Annotated[
        int,
        Field(ge=0, description="Вернуть контент начиная со строки `line_offset`"),
    ],
    line_count: Annotated[
        int,
        Field(ge=1, description="Сколько строк вернуть начиная с `line_offset`"),
    ],
) -> WebFetchAnswer:
    """Скачать URL и вернуть окно строк его содержимого."""
    args = WebFetchArgs(
        url=url,
        as_markdown=as_markdown,
        line_offset=line_offset,
        line_count=line_count,
    )

    collector = TextCollector(
        max_chars=cfg.max_result_chars,
        limit_rows=None,
        header_lines=0,
    )

    trailer = caller.fetch(args, collector)
    collector.close()

    return WebFetchAnswer(
        content=collector.text(),
        source_url=trailer.source_url,
        total_lines=trailer.total_lines,
        returned_lines=trailer.returned_lines,
    )
