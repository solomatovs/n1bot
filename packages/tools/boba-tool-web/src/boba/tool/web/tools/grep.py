"""Tool `web_grep` + `WebGrepConfig`.

Скачивает одну web-страницу (как `web_fetch` — `WebUrlsRequestSource →
http_transport`) и применяет к её контенту grep-поиск с теми же семантиками,
что и file-tool `grep`: regex/fixed_string, регистр, контекст до/после, limit,
обрезка длинных строк по `max_text_chars`.

В отличие от `web_fetch`, возвращает не окно строк, а только совпадения с
номерами строк — экономит контекст LLM на больших страницах.

Config-секция: `[tool.web.grep]`.
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

from boba.indexing import BinaryStream, PipelineContext, PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.web.connection import WebConnection
from boba.tool.web.request_source import WebUrlsRequestSource
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["WebGrepConfig", "web_grep"]


class WebGrepConfig(BobaFlatSettings):
    """Config tool'а `web_grep` (`[tool.web.grep]`)."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.web.grep",
        defaults_from=("tool.web",),
    )

    connection: WebConnection
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match.",
    )


class _WebGrep:
    """Скачать страницу → grep поверх in-memory текста (regex + контекст)."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("web.grep")
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, *, connection: WebConnection) -> None:
        self._connection = connection

    def load_text(self, *, url: str, as_markdown: bool) -> str:
        """Скачать URL и вернуть его контент целиком (markdown или HTML)."""
        source = WebUrlsRequestSource(urls=(url,), connection=self._connection)
        transport = self._connection.make_transport()
        pctx = PipelineContext(pipeline_id=self.PIPELINE_ID)
        try:
            for raw in transport.stream(pctx, source.stream(pctx)):
                return self._decode(raw.handle, as_markdown=as_markdown)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"web_grep failed: {type(e).__name__}: {e}",
            ) from e
        raise RuntimeError(f"web_grep: пустой ответ для URL {url!r}")

    @classmethod
    def _decode(cls, handle: BinaryStream, *, as_markdown: bool) -> str:
        """Прочитать тело целиком; при as_markdown — HTML → markdownify (ATX)."""
        html = handle.read(-1).decode(cls.ENCODING, errors="replace")
        if as_markdown:
            return markdownify.markdownify(html, heading_style="ATX")
        return html

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
    def note(
        url: str, items: list[dict[str, Any]], *, truncated: bool, max_chars: int
    ) -> str:
        """Footer-контекст под таблицей: url, число совпадений, усечения."""
        if not items:
            return f"url={url}: совпадений не найдено"
        parts = [f"url={url}", f"совпадений: {len(items)}"]
        if truncated:
            parts.append(f"показаны первые {len(items)} (найдено больше)")
        if any(it.get("truncated_lines") for it in items):
            parts.append(f"строки усечены до {max_chars} символов")
        return "; ".join(parts)

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
            clipped, c = _WebGrep.clip(line, limit)
            out.append(clipped)
            cut = cut or c
        return out, cut


@tool
def web_grep(  # noqa: PLR0913 — независимые флаги grep'а
    cfg: Annotated[WebGrepConfig, FromConfig()],
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
    """Скачивает URL и ищет в его контенте совпадения pattern.

    Возвращает `TableResult` — таблицу matches с колонками `line`/`content`/
    `before`/`after` (+`truncated_lines` на усечённых). Url и переполнение
    limit — в `note`/`metadata`. Длинные строки режутся по `max_text_chars`.
    """
    engine = _WebGrep(connection=cfg.connection)
    compiled = _WebGrep.compile_pattern(
        pattern,
        fixed_string=fixed_string,
        case_insensitive=case_insensitive,
    )
    text = engine.load_text(url=url, as_markdown=as_markdown)

    found = _WebGrep.iter_matches(text, compiled, context=context)
    matches = list(islice(found, limit + 1))
    truncated = len(matches) > limit
    if truncated:
        matches = matches[:limit]

    max_chars = cfg.max_text_chars
    items: list[dict[str, Any]] = []
    for m in matches:
        content, cut_c = _WebGrep.clip(m["content"], max_chars)
        before, cut_b = _WebGrep.clip_many(m["before"], max_chars)
        after, cut_a = _WebGrep.clip_many(m["after"], max_chars)
        item: dict[str, Any] = {
            "line": m["line"],
            "content": content,
            "before": before,
            "after": after,
        }
        if cut_c or cut_b or cut_a:
            item["truncated_lines"] = True
        items.append(item)

    note = _WebGrep.note(url, items, truncated=truncated, max_chars=max_chars)
    return TableResult(rows=items, note=note, metadata={"url": url})
