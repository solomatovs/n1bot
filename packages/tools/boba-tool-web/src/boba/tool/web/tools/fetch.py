"""
Tool `web_fetch`.

Скачивает одну web-страницу и возвращает строки [line_offset : line_offset+line_count]

Возвращаемый dict:

    {
      "content":        str,   # окно строк, склеенное через "\\n"
      "path":           str,   # URL источника (canonical id запроса)
      "total_lines":    int,   # сколько всего строк в материализованном контенте
      "returned_lines": int,   # len(window)
    }
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, ClassVar

import httpx
import markdownify
from pydantic import Field

from boba.indexing import BinaryStream, PipelineContext, PipelineId
from boba.tool.web.connection import WebConnection
from boba.tool.web.request_source import WebUrlsRequestSource
from boba.tools import FromConfig, tool
from boba.transport.http import HttpTransport

__all__ = ["web_fetch"]


class _WebFetcher:
    """Скачать → построчно итерировать → вырезать окно; без записи на ФС."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("web.fetch")
    CHUNK_SIZE: ClassVar[int] = 8192
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, *, connection: WebConnection) -> None:
        self._connection = connection

    def run(
        self,
        *,
        url: str,
        as_markdown: bool,
        line_offset: int,
        line_count: int,
    ) -> dict[str, Any]:
        source = WebUrlsRequestSource(urls=(url,), connection=self._connection)
        transport = HttpTransport(self._connection.resolve_profile(url))
        pctx = PipelineContext(pipeline_id=self.PIPELINE_ID)
        try:
            for raw in transport.stream(pctx, source.stream(pctx)):
                lines_iter = (
                    self._iterate_markdown_lines(raw.handle)
                    if as_markdown
                    else self._iterate_html_lines(raw.handle)
                )
                window, total = self._collect_window(
                    lines_iter,
                    offset=line_offset,
                    count=line_count,
                )
                return {
                    "content": "\n".join(window),
                    "path": url,
                    "total_lines": total,
                    "returned_lines": len(window),
                }
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"web_fetch failed: {type(e).__name__}: {e}",
            ) from e
        msg = f"web_fetch: пустой ответ для URL {url!r}"
        raise RuntimeError(msg)

    @staticmethod
    def _iterate_html_lines(handle: BinaryStream) -> Iterator[str]:
        """Построчно из byte-stream без загрузки всего тела в RAM."""
        buf = b""
        while True:
            chunk = handle.read(_WebFetcher.CHUNK_SIZE)
            if not chunk:
                if buf:
                    yield buf.decode(_WebFetcher.ENCODING, errors="replace")
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode(_WebFetcher.ENCODING, errors="replace")

    @staticmethod
    def _iterate_markdown_lines(handle: BinaryStream) -> Iterator[str]:
        """HTML → markdownify (требует всю страницу) → splitlines."""
        html = handle.read(-1).decode(_WebFetcher.ENCODING, errors="replace")
        md = markdownify.markdownify(html, heading_style="ATX")
        return iter(md.splitlines())

    @staticmethod
    def _collect_window(
        lines: Iterator[str],
        *,
        offset: int,
        count: int,
    ) -> tuple[list[str], int]:
        """Пройти весь iter (для total), собрать только окно [offset:offset+count]."""
        end = offset + count
        window: list[str] = []
        total = 0
        for line in lines:
            if offset <= total < end:
                window.append(line)
            total += 1
        return window, total


@tool
def web_fetch(
    cfg: Annotated[WebConnection, FromConfig()],
    url: Annotated[
        str,
        Field(
            min_length=1,
            description=("URL для скачивания"),
        ),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=("true — конвертирует HTML→Markdown"),
        ),
    ],
    line_offset: Annotated[
        int,
        Field(
            ge=0,
            description=("Вернуть контент начиная со строки `line_offset`"),
        ),
    ],
    line_count: Annotated[
        int,
        Field(
            ge=1,
            description=("Сколько строк вернуть начиная с `line_offset`"),
        ),
    ],
) -> dict[str, Any]:
    """
    Скачивает URL и возвращает окно строк как dict

    Формат: `{content, path, total_lines, returned_lines}`.
    `path` — URL источника (canonical id запроса).
    `total_lines` позволяет выбрать корректный следующий `line_offset`.
    """
    fetcher = _WebFetcher(connection=cfg)
    return fetcher.run(
        url=url,
        as_markdown=as_markdown,
        line_offset=line_offset,
        line_count=line_count,
    )
