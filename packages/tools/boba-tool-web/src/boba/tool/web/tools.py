"""Web-инструменты: функции уровня модуля, модуль — обычная программа.

Запуск: `python -m boba.tool.web.tools <имя> --флаги` — та же команда у
launcher'а приложения и у человека в терминале. Ссылка скачивается
соединением хоста, тело читает роутер boba-doc: html становится markdown,
pdf/docx/xlsx/pptx и картинки — текстом (картинки и сканы через OCR).

Ошибки:
TransportError — страница не скачалась: сеть, TLS, статус после ретраев.
UnknownConnectionError — имя соединения вне whitelist'а.
UnknownHostError — хост URL не покрыт выбранным соединением.
DocumentError — тело не распознано как документ или не прочитано ридером.
OcrUnavailableError — вызов просил OCR, а секция [tool.web] держит
    ocr.provider = off.
ResultTooLargeError — содержимое превысило max_result_chars конфига.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, BinaryIO, ClassVar, Final

import httpx
from pydantic import ConfigDict, Field

from boba.doc.bridge import AsyncPipe
from boba.doc.config import DocSection, OcrUnavailableError
from boba.doc.document import DocumentError, DocumentHint, DocumentKind
from boba.llm.providers import LlmProviders, LlmProviderTypes
from boba.text.grep import GrepLimits, TextGrep
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.result import MarkdownResult, ResultTooLargeError, TableResult
from boba.toolkit.types import SecretRevealing
from boba.transport.http import (
    HttpRequest,
    HttpTransport,
    HttpTransportConfig,
    TransportError,
)
from boba.transport.http.connection import HttpConnection, UnknownHostError

_OCR_DESCRIPTION = (
    "OCR для картинок и сканов: true распознаёт текст по изображениям, "
    "false — только текстовый слой документа. OCR дорог: секунды на страницу."
)
_AS_MARKDOWN_DESCRIPTION = (
    "true — HTML конвертируется в Markdown, false — HTML как есть. "
    "Документы (pdf, docx, xlsx, pptx, картинки) всегда приходят текстом."
)


LLM: Final = LlmProviders(LlmProviderTypes.installed())
"""Модели процесса инструмента: чат-модель OCR живёт здесь."""


class WebErrorKind(StrEnum):
    """Ожидаемые отказы web-инструментов."""

    REQUEST_FAILED = "web_request_failed"
    UNKNOWN_TARGET = "unknown_target"
    UNKNOWN_HOST = "unknown_host"
    DOCUMENT_UNREADABLE = "document_unreadable"
    OCR_UNAVAILABLE = "ocr_unavailable"
    RESULT_TOO_LARGE = "result_too_large"


WebTarget = Annotated[HttpConnection, UserConnection]
"""Параметр-соединение web-инструментов: имя от модели, соединение от хоста."""


class AddressColumn(StrEnum):
    """Колонки выдачи web_address."""

    CONNECTION = "connection"
    URL = "url"


class WebToolsConfig(DocSection, SecretRevealing):
    """Секция [tool.web]: чтение документов boba-doc с OCR, лимиты выдачи и
    транспорт."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    SECTION: ClassVar[str] = "tool.web"

    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины строки grep-выдачи: совпадения и контекста.",
    )
    max_result_chars: int = Field(
        default=1_000_000,
        ge=1,
        description="Потолок суммарного объёма результата (символов).",
    )
    transport: HttpTransportConfig = Field(
        default_factory=HttpTransportConfig,
        description=(
            "Поведение HTTP-транспорта процесса: таймауты, пул, дамп обмена; "
            'ссылкой `transport = "${http}"`.'
        ),
    )


class PageFormat(StrEnum):
    """Формат содержимого страницы; он же язык markdown-блока показа."""

    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"

    @classmethod
    def of(cls, kind: DocumentKind, *, as_markdown: bool) -> PageFormat:
        if kind is not DocumentKind.HTML:
            return cls.TEXT

        if as_markdown:
            return cls.MARKDOWN

        return cls.HTML


@dataclass(frozen=True)
class WebText:
    """Прочитанное содержимое ссылки и вид документа, из которого оно взято."""

    text: str
    kind: DocumentKind


@dataclass(frozen=True)
class LineWindow:
    """Окно строк страницы: показанный кусок и его место в документе."""

    url: str
    offset: int
    lines: Sequence[str]
    total: int

    @classmethod
    def of(cls, url: str, page: str, offset: int, count: int) -> LineWindow:
        lines = page.splitlines()
        window = lines[offset : offset + count]

        return cls(url=url, offset=offset, lines=window, total=len(lines))

    def text(self) -> str:
        return "\n".join(self.lines)

    def note(self) -> str:
        """Сводка окна: источник и место среза в документе."""
        if not self.lines:
            return f"url={self.url}; no lines at offset {self.offset} of {self.total}"

        first = self.offset + 1
        last = self.offset + len(self.lines)

        return f"url={self.url}; lines {first}-{last} of {self.total}"


class WebPage:
    """Скачивание ссылки соединением хоста и её текст роутером boba-doc.

    Роутер и движок OCR собираются в конструкторе из секции и флага OCR
    вызова; тело ответа льётся пипой в поток ридера без буфера целиком.
    Библиотеки форматов тяжёлые и живут в песочнице, поэтому импортируются
    здесь, а не при загрузке модуля: манифест плагина импортирует модуль в
    процессе приложения.
    """

    CONTENT_TYPE: ClassVar[str] = "content-type"

    def __init__(
        self, connection: HttpConnection, cfg: WebToolsConfig, *, ocr_enabled: bool
    ) -> None:
        from boba.doc.ocr import OcrEngines  # noqa: PLC0415
        from boba.doc.router import DocumentRouter  # noqa: PLC0415

        self._cfg = cfg.for_call(ocr=ocr_enabled)
        self._router = DocumentRouter(self._cfg, OcrEngines(LLM).of(self._cfg.ocr))
        self._http = HttpTransport(connection, cfg.transport)

    async def load(self, url: str, *, as_markdown: bool) -> WebText:
        request = HttpRequest(url=url, follow_redirects=True)
        async with self._http, self._http.fetch(request) as response:
            hint = self._hint(url, response.headers)
            loaded = await AsyncPipe.run(
                response.stream, self._consumer(hint, as_markdown=as_markdown)
            )

        limit = self._cfg.max_result_chars
        if len(loaded.text) > limit:
            raise ResultTooLargeError.chars_limit(limit)

        return loaded

    def _hint(self, url: str, headers: Mapping[str, str]) -> DocumentHint:
        """Что известно о теле до байтов: media_type ответа и имя файла из
        последнего сегмента пути ссылки."""
        media_type = headers.get(self.CONTENT_TYPE, "")
        filename = PurePosixPath(httpx.URL(url).path).name

        return DocumentHint(media_type=media_type, filename=filename)

    def _consumer(
        self, hint: DocumentHint, *, as_markdown: bool
    ) -> Callable[[BinaryIO], WebText]:
        """Потребитель потока тела: роутер определяет вид, html без конверсии
        читается как текст."""

        def read(source: BinaryIO) -> WebText:
            kind, stream = self._router.detect(source, hint)

            opened = kind
            if kind is DocumentKind.HTML and not as_markdown:
                opened = DocumentKind.TEXT

            return WebText(text=self._router.read_text_as(opened, stream), kind=kind)

        return read


@tool
async def web_fetch_page(  # noqa: PLR0913
    url: Annotated[str, Field(min_length=1, description="URL для скачивания")],
    connection: WebTarget,
    as_markdown: Annotated[bool, Field(description=_AS_MARKDOWN_DESCRIPTION)],
    line_offset: Annotated[
        int,
        Field(ge=0, description="Вернуть контент начиная со строки"),
    ],
    line_count: Annotated[
        int,
        Field(ge=1, description="Сколько строк вернуть начиная с line_offset"),
    ],
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[WebToolsConfig, Injected],
) -> MarkdownResult:
    """Скачивает URL соединением connection (см. connection_list) и возвращает
    окно строк его текста; строка под текстом называет срез и общее число
    строк — по ней листай страницу дальше."""
    bound = connection.for_url(url)

    page = await WebPage(bound, cfg, ocr_enabled=ocr_enabled).load(
        url, as_markdown=as_markdown
    )

    window = LineWindow.of(url, page.text, line_offset, line_count)

    return MarkdownResult(
        text=window.text(),
        language=PageFormat.of(page.kind, as_markdown=as_markdown),
        note=window.note(),
        metadata={"url": url, "kind": page.kind.value},
    )


@tool
async def web_grep_page(  # noqa: PLR0913
    url: Annotated[
        str,
        Field(min_length=1, description="URL для скачивания."),
    ],
    connection: WebTarget,
    pattern: Annotated[
        str,
        Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
    ],
    as_markdown: Annotated[bool, Field(description=_AS_MARKDOWN_DESCRIPTION)] = True,
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
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[WebToolsConfig, Injected],
) -> MarkdownResult:
    """Найти совпадения pattern в тексте ссылки, скачанной соединением
    connection (см. connection_list)."""
    bound = connection.for_url(url)

    page = await WebPage(bound, cfg, ocr_enabled=ocr_enabled).load(
        url, as_markdown=as_markdown
    )

    compiled = TextGrep.compile_pattern(
        pattern, fixed_string=fixed_string, case_insensitive=case_insensitive
    )

    limits = GrepLimits(context=context, limit=limit, clip_chars=cfg.max_text_chars)
    report = TextGrep.report(page.text, compiled, limits, f"url={url}")

    return MarkdownResult(
        text=report.render(),
        language=report.LANG,
        note=report.note,
        metadata={"url": url, "kind": page.kind.value},
    )


@tool
async def web_address(connection: WebTarget) -> TableResult:
    """Корневой url web-соединения без учётных данных: схема, хост, порт, путь.

    Ничего не запрашивает. Страницы адресуются путями под этим корнем.
    """
    row = {
        AddressColumn.CONNECTION.value: connection.source.name,
        AddressColumn.URL.value: str(connection.public_url()),
    }

    return TableResult(rows=[row])


EXPECTED: Mapping[type[Exception], WebErrorKind] = {
    TransportError: WebErrorKind.REQUEST_FAILED,
    UnknownHostError: WebErrorKind.UNKNOWN_HOST,
    DocumentError: WebErrorKind.DOCUMENT_UNREADABLE,
    OcrUnavailableError: WebErrorKind.OCR_UNAVAILABLE,
    ResultTooLargeError: WebErrorKind.RESULT_TOO_LARGE,
}

TOOLS: Final = ToolMain.toolset(web_fetch_page, web_grep_page, web_address)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
