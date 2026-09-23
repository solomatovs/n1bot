"""Чтение Confluence: функции уровня модуля, модуль — обычная программа.

REST-запросы, пагинация и разбор HTML исполняются в теле — потому оно живёт
в песочнице: наружу не уезжает ни сырой ответ, ни исходная разметка.

Ошибки:
TransportError — REST недоступен или ответил статусом.
ConfluencePayloadError — ответ не той формы, которую ждали.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncGenerator, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal, Self

import httpx
from pydantic import ConfigDict, Field, ValidationError

from boba.confluence.address import ConfluenceAddresses
from boba.confluence.models import (
    ConfluencePayloadError,
    ConfluenceSpaceItem,
    SpaceMask,
)
from boba.confluence.parsing import JsonNode
from boba.confluence.rest import CflRest, CflRestBuilder, SpaceType
from boba.text.grep import GrepLimits, TextGrep
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import MarkdownResult, TableResult
from boba.toolkit.types import LLMStringList, SecretRevealing
from boba.toolkit.window import RowLimit, RowOffset, RowPage, RowWindow
from boba.transport.http import (
    ByteStream,
    HttpTransport,
    HttpTransportConfig,
    TransportError,
)
from boba.transport.http.connection import HttpConnection


class ConfluenceErrorKind(StrEnum):
    """Ожидаемые отказы confluence-инструментов."""

    REQUEST_FAILED = "confluence_request_failed"


class ConfluenceToolsConfig(SecretRevealing):
    """Конфиг инструментов чтения Confluence; секция [tool.confluence]."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.confluence"

    confluence: HttpConnection = Field(
        description='Web-профиль Confluence ссылкой `confluence = "${web.<name>}"`.',
    )
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match в grep.",
    )
    transport: HttpTransportConfig = Field(
        default_factory=HttpTransportConfig,
        description=(
            "Поведение HTTP-транспорта процесса: таймауты, пул, дамп обмена; "
            'ссылкой `transport = "${http}"`.'
        ),
    )


class AddressColumn(StrEnum):
    """Колонки выдачи confluence_address."""

    URL = "url"


class ConfluenceHttp:
    """REST Confluence на время одного вызова инструмента: транспорт по
    соединению и секции, адреса — CflRestBuilder, обмен — CflRest."""

    def __init__(self, cfg: ConfluenceToolsConfig) -> None:
        self._cfg = cfg
        self._builder = CflRestBuilder()
        self._http = HttpTransport(cfg.confluence, cfg.transport)
        self._rest = CflRest(self._http)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.close()

    async def get(self, path: httpx.URL) -> bytes:
        return await self._rest.get(path)

    @asynccontextmanager
    async def fetch(self, path: httpx.URL) -> AsyncGenerator[ByteStream, None]:
        async with self._rest.fetch(path) as stream:
            yield stream

    async def page_json(self, page_id: str) -> dict[str, Any]:
        path = self._builder.page_fetch_path(page_id, body_format=self._cfg.body_format)

        return await self._rest.get_json(path)

    async def search_json(
        self, cql: str, *, limit: int, start: int, expand: str
    ) -> dict[str, Any]:
        path = self._builder.cql_search_path(
            cql, limit=limit, start=start, expand=expand
        )

        return await self._rest.get_json(path)

    async def spaces_json(
        self, space_type: SpaceType, *, limit: int
    ) -> tuple[dict[str, Any], httpx.URL]:
        """Список спейсов и адрес запроса: адрес нужен тексту ошибки разбора."""
        path = self._builder.space_list_path(space_type, limit=limit)

        return await self._rest.get_json(path), path


class ConfluencePageText:
    """Контент страницы: HTML нужного формата и конверсия в markdown."""

    HEADING_STYLE: ClassVar[str] = "ATX"

    def __init__(self, cfg: ConfluenceToolsConfig, http: ConfluenceHttp) -> None:
        self._body_format = cfg.body_format
        self._http = http

    async def of_page(self, page_id: str, *, as_markdown: bool) -> str:
        data = await self._http.page_json(page_id)
        html = JsonNode(data).body_html(self._body_format)
        if not as_markdown:
            return html

        return self.markdown_of(html)

    def markdown_of(self, html: str) -> str:
        # bs4 и markdownify тяжёлые: грузятся только в теле инструмента
        from boba.confluence.html import MarkdownRender  # noqa: PLC0415

        request = {"html": html, "heading_style": self.HEADING_STYLE}

        return str(MarkdownRender(request).run()["markdown"])

    def excerpt_of(self, html: str, snippet_chars: int) -> str:
        from boba.confluence.html import PlainTextRender  # noqa: PLC0415

        excerpt = ""
        if html:
            excerpt = str(PlainTextRender({"html": html}).run()["text"])

        if len(excerpt) > snippet_chars:
            excerpt = excerpt[: snippet_chars - 1].rstrip() + "…"

        return excerpt


class SpaceList:
    """Разбор выдачи /rest/api/space, фильтр спейсов по шаблону вызова и
    строка таблицы для одного спейса с адресом по профилю."""

    def __init__(self, pattern: str | None, connection: HttpConnection) -> None:
        masks: list[str] = []
        if pattern is not None:
            masks.append(pattern)

        self._mask = SpaceMask(masks)
        self._connection = connection

    def items(
        self, data: Mapping[str, Any], path: httpx.URL
    ) -> Sequence[ConfluenceSpaceItem]:
        found: list[ConfluenceSpaceItem] = []
        for raw in JsonNode(dict(data)).results():
            try:
                found.append(ConfluenceSpaceItem.model_validate(raw))
            except ValidationError as exc:
                msg = (
                    f"GET {path} on confluence: expected space results, "
                    f"got {json.dumps(raw, ensure_ascii=False)[:200]}: {exc}"
                )
                raise ConfluencePayloadError(msg) from exc

        return found

    def matches(self, space: ConfluenceSpaceItem) -> bool:
        """Glob по ключу или названию целиком; без шаблона проходят все."""
        return self._mask.matches(space)

    def row(self, space: ConfluenceSpaceItem) -> dict[str, Any]:
        return {
            "key": space.key,
            "name": space.name,
            "type": space.type,
            "status": space.status,
            "url": space.url_at(self._connection),
        }


class CqlQuery:
    """CQL-запрос полнотекстового поиска: текст и необязательный список спейсов."""

    def __init__(self, query: str, spaces: Sequence[str] | None) -> None:
        self._query = query
        self._spaces = tuple(spaces or ())

    def render(self) -> str:
        text_block = f"text ~ {self.literal(self._query)}"
        if not self._spaces:
            return text_block

        if len(self._spaces) == 1:
            space_block = f"space = {self.literal(self._spaces[0])}"
        else:
            literals: list[str] = []
            for space in self._spaces:
                literals.append(self.literal(space))

            space_block = f"space in ({', '.join(literals)})"

        return f"({text_block}) and ({space_block})"

    @staticmethod
    def literal(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')

        return f'"{escaped}"'


class CqlSearch:
    """Разбор выдачи поиска: строка таблицы на hit и подпись навигации."""

    SNIPPET_DEFAULT: ClassVar[int] = 1000
    SNIPPET_DESC: ClassVar[str] = (
        "Максимальная длина сниппета на каждый hit (символов). По умолчанию 1000."
    )

    def __init__(
        self, connection: HttpConnection, text: ConfluencePageText, snippet_chars: int
    ) -> None:
        self._profile = connection
        self._text = text
        self._snippet_chars = snippet_chars

    def result(self, page: RowPage, data: JsonNode) -> TableResult:
        """Страница hits; totalSize отдаёт не всякая версия Confluence, при
        нём note получает хвост total=N."""
        note = page.note()

        total = data.int("totalSize", default=-1)
        if total >= 0:
            note = f"{note}; total={total}"

        return TableResult(rows=page.rows, note=note)

    def hit_rows(self, hits: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for hit in hits:
            yield self.hit_row(hit)

    def hit_row(self, hit: dict[str, Any]) -> dict[str, Any]:
        node = JsonNode(hit)
        excerpt = self._text.excerpt_of(node.body_html("view"), self._snippet_chars)

        url = self._profile.root_url()
        if webui := node.webui():
            url = self._profile.url_of(webui)

        return {
            "page_id": node.str("id"),
            "title": node.str("title"),
            "space_key": node.str("space", "key"),
            "url": str(url),
            "excerpt": excerpt,
        }


@tool
async def confluence_fetch(
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
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown. "
                "Иначе возвращает исходный Confluence-HTML."
            ),
        ),
    ] = True,
    *,
    cfg: Annotated[ConfluenceToolsConfig, Injected],
) -> MarkdownResult:
    """Скачивает одну Confluence-страницу и возвращает её контент."""
    async with ConfluenceHttp(cfg) as http:
        pages = ConfluencePageText(cfg, http)
        text = await pages.of_page(page_id, as_markdown=as_markdown)

    return MarkdownResult(text=text)


@tool
async def confluence_grep(  # noqa: PLR0913 — независимые флаги grep'а
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description="ID страницы Confluence (из URL viewpage.action?pageId=<id>).",
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
                "Искать по Markdown-конверсии вместо исходного "
                "Confluence-HTML. По умолчанию true."
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
        Field(ge=1, description="Максимум совпадений. По умолчанию 100."),
    ] = 100,
    fixed_string: Annotated[
        bool,
        Field(description="Литеральный поиск без regex. По умолчанию false."),
    ] = False,
    *,
    cfg: Annotated[ConfluenceToolsConfig, Injected],
) -> MarkdownResult:
    """Ищет совпадения по тексту одной Confluence-страницы."""
    async with ConfluenceHttp(cfg) as http:
        pages = ConfluencePageText(cfg, http)
        text = await pages.of_page(page_id, as_markdown=as_markdown)

    compiled = TextGrep.compile_pattern(
        pattern, fixed_string=fixed_string, case_insensitive=case_insensitive
    )

    limits = GrepLimits(context=context, limit=limit, clip_chars=cfg.max_text_chars)
    report = TextGrep.report(text, compiled, limits, f"page_id={page_id}")

    return MarkdownResult(
        text=report.render(),
        language=report.LANG,
        note=report.note,
        metadata={"page_id": page_id},
    )


@tool
async def confluence_search(  # noqa: PLR0913 — окно выдачи задаёт вызов
    query: Annotated[
        str,
        Field(min_length=1, description="Строка полнотекстового поиска в Confluence."),
    ],
    spaces: Annotated[
        LLMStringList | None,
        Field(
            description=(
                "Ограничение поиска по space-ключам Confluence. "
                "Не передавай (или `null`) — поиск по всем space'ам."
            ),
        ),
    ] = None,
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=CqlSearch.SNIPPET_DESC),
    ] = CqlSearch.SNIPPET_DEFAULT,
    *,
    offset: RowOffset,
    limit: RowLimit,
    cfg: Annotated[ConfluenceToolsConfig, Injected],
) -> TableResult:
    """Ищет страницы в Confluence через CQL и возвращает таблицу hits.

    Выдача постраничная: сколько показано и как листать, сказано в note.
    """
    window = RowWindow(offset=offset, limit=limit)

    async with ConfluenceHttp(cfg) as http:
        search = CqlSearch(cfg.confluence, ConfluencePageText(cfg, http), snippet_chars)
        data = await http.search_json(
            CqlQuery(query, spaces).render(),
            limit=window.served_probe(),
            start=window.offset,
            expand="body.view,version,space",
        )

    node = JsonNode(data)

    page = RowPage(window, skipped=window.offset)
    page.take(search.hit_rows(node.results()))

    return search.result(page, node)


@tool
async def confluence_spaces(
    pattern: Annotated[
        str | None,
        Field(
            description=(
                "Glob-шаблон (регистронезависимо) для key/name спейса. "
                "Совпадение по полю целиком: `*data*` — содержит data."
            ),
        ),
    ] = None,
    space_type: Annotated[
        Literal["global", "personal", "any"],
        Field(description="Тип space: global / personal / any."),
    ] = "global",
    limit: Annotated[
        int,
        Field(ge=1, le=1000, description="Максимум спейсов в ответе."),
    ] = 200,
    *,
    cfg: Annotated[ConfluenceToolsConfig, Injected],
) -> TableResult:
    """Список spaces Confluence с опциональным glob-фильтром.

    В строке есть адрес спейса: по нему открывают его в браузере и с него
    начинают обход, не собирая ссылку из ключа руками. Колонка status
    показывает архивные спейсы (archived): их содержимое живо и индексируется,
    но поиск Confluence его не отдаёт, поэтому по CQL такой спейс выглядит
    пустым.
    """
    async with ConfluenceHttp(cfg) as http:
        data, path = await http.spaces_json(SpaceType(space_type), limit=limit)

    spaces = SpaceList(pattern, cfg.confluence)
    rows: list[dict[str, Any]] = []
    for space in spaces.items(data, path):
        if not spaces.matches(space):
            continue

        rows.append(spaces.row(space))

    return TableResult(rows=rows)


@tool
async def confluence_address(
    cfg: Annotated[ConfluenceToolsConfig, Injected],
) -> TableResult:
    """Корневой url Confluence без учётных данных и формы url его объектов.

    Ничего не запрашивает. Спейс, страница и вложение адресуются REST-путями
    под этим корнем; формы перечислены в подписи ответа.
    """
    row = {AddressColumn.URL.value: str(cfg.confluence.public_url())}

    return TableResult(rows=[row], note=ConfluenceAddresses.prompt())


EXPECTED: Mapping[type[Exception], ConfluenceErrorKind] = {
    TransportError: ConfluenceErrorKind.REQUEST_FAILED,
    ConfluencePayloadError: ConfluenceErrorKind.REQUEST_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    confluence_fetch,
    confluence_grep,
    confluence_search,
    confluence_spaces,
    confluence_address,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
