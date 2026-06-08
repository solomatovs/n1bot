"""Доступ к Confluence Server REST: URL-builder, пагинатор, RequestSource'ы.

- ConfluenceRest        — @staticmethod-фабрики путей и HttpRequest'ов
  (content/space/cql пути, page/attachment-запросы).
- ConfluencePaginator   — httpx-клиент для пагинированных discovery-запросов
  (+ discover_spaces/discover_space_pages/discover_pages_by_cql).
- RequestSource'ы       — CQL / Pages / Space / MultiSpace (discovery для
  ingest), и CqlSearch (один запрос на /content/search для online-tool'ов).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar
from urllib.parse import quote, urlparse

from pydantic import BaseModel

from boba.indexing import (
    Metadata,
    RequestSource,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import (
    AttachmentInfo,
    ConfluenceKeys,
    ConfluencePageItem,
    ConfluenceSpaceItem,
)
from boba.tool.kb.confluence.parsing import ConfluenceJson
from boba.transport.http import HttpRequest, HttpTransport

__all__ = [
    "ConfluenceCqlRequestSource",
    "ConfluenceCqlSearchRequestSource",
    "ConfluenceMultiSpaceRequestSource",
    "ConfluencePagesRequestSource",
    "ConfluencePaginator",
    "ConfluenceRequest",
    "ConfluenceRest",
    "ConfluenceSpaceRequestSource",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ConfluenceRequest:
    """Индексационный план запроса Confluence: чистый HTTP + metadata.

    Удовлетворяет Request-протокол (только metadata). source_id НЕ часть запроса:
    его выводит транспорт из реально запрашиваемого URL (base_url профиля +
    http.url, без волатильного query) — см. ConfluenceHttpTransport.source_id.
    Поэтому http.url несёт только path; base_url знает профиль/транспорт.
    Логические/презентационные URL (viewpage и т.п.) живут в metadata.

    http — чистый HttpRequest (path), который исполняет HttpTransport. Обогащение
    metadata данными ответа делает ConfluenceHttpTransport при сборке RawDocument.
    """

    http: HttpRequest
    metadata: Metadata = field(default_factory=Metadata.empty)


class ConfluenceRest:
    """Фабрики Confluence REST: URL/path-builders и HttpRequest-конструкторы."""

    DEFAULT_PAGE_LIMIT: ClassVar[int] = 50

    @staticmethod
    def page_fetch_path(page_id: str, *, body_format: str) -> str:
        """/rest/api/content/{id}?expand=… — выгрузка одной страницы с expand-полями.

        children.attachment раскрывает список вложений (results[] с
        id/title/extensions.mediaType/extensions.fileSize/_links.download/
        version.number) прямо в основном ответе — это позволяет Decoder'у
        положить их в ConfluenceKeys.ATTACHMENTS для последующего fan-out'а
        без дополнительного round-trip'а на /child/attachment.
        """
        expand = (
            f"body.{body_format},version,ancestors,space,metadata.labels,"
            "children.attachment.version,children.attachment.extensions"
        )
        return f"/rest/api/content/{page_id}?expand={expand}"

    @staticmethod
    def space_list_path(
        space_type: str,
        *,
        expand: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> str:
        """/rest/api/space?… — список space'ов с опциональным фильтром по типу.

        space_type ∈ {global, personal, any}: any снимает фильтр.
        expand — необязательное (например, description.plain).
        """
        type_filter = "" if space_type == "any" else f"&type={space_type}"
        expand_q = f"&expand={expand}" if expand else ""
        return f"/rest/api/space?limit={limit}&start=0{type_filter}{expand_q}"

    @staticmethod
    def space_pages_path(
        space_key: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> str:
        """/rest/api/space/{key}/content?type=page&… — все страницы space'а."""
        return f"/rest/api/space/{space_key}/content?type=page&limit={limit}&start=0"

    @staticmethod
    def cql_search_path(
        cql: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        expand: str | None = None,
    ) -> str:
        """/rest/api/content/search?cql=…&… — CQL-поиск страниц.

        cql URL-encode'ится здесь; не нужно quote'ить заранее.
        expand — необязательное (например, body.view,version,space).
        """
        expand_q = f"&expand={expand}" if expand else ""
        cql_q = quote(cql, safe="")
        return f"/rest/api/content/search?cql={cql_q}&limit={limit}{expand_q}"

    @staticmethod
    def extract_host(base_url: str) -> str:
        """
        выбирает только netloc из переданного url
        https://confl.x.com/wiki/ -> confl.x.com
        """
        netloc = urlparse(base_url).netloc
        return netloc or base_url.split("://", 1)[-1].split("/", 1)[0]

    @staticmethod
    def make_page_request(
        *,
        host: str,
        page_id: str,
        body_format: str,
    ) -> ConfluenceRequest:
        """ConfluenceRequest на выгрузку страницы.

        http.url   — относительный REST path с expand (`/rest/api/content/{id}?…`);
                       base_url приклеит транспорт из профиля. Из реально
                       запрошенного URL он же выведет source_id (без query:
                       `{base}/rest/api/content/{id}`) — стабильный адрес ресурса.
        metadata   — page_id и host. SOURCE_URL (кликабельный viewpage) здесь НЕ
                       ставим: его проставит ConfluenceJsonDecoder из `_links`
                       ответа (base+webui) — авторитетный URL от самого Confluence.
        """
        path = ConfluenceRest.page_fetch_path(page_id, body_format=body_format)
        return ConfluenceRequest(
            http=HttpRequest(url=path, method="GET"),
            metadata=(
                Metadata.empty()
                .set(ConfluenceKeys.PAGE_ID, page_id)
                .set(ConfluenceKeys.HOST, host)
            ),
        )

    @staticmethod
    def make_attachment_request(
        *,
        base_url: str,
        parent_metadata: Metadata,
        attachment: AttachmentInfo,
    ) -> ConfluenceRequest:
        """HttpRequest на бинарную выгрузку одного вложения.

        http.url       — относительный download-path (base_url приклеит транспорт);
                           из него транспорт выводит source_id (URL без query).
        metadata       — ATTACHMENT_INFO (snapshot вложения — маркер, что это
                           вложение, и носитель полей для download'а) +
                           PAGE_ID/HOST/SPACE_KEY/ANCESTORS_TITLES родителя.
                           Для цитаты: SOURCE_URL = UI-link самого вложения
                           (attachment._links.webui, фолбэк на download), а
                           PARENT_URL = URL родительской страницы (её webui).

        TransportKeys.CONTENT_TYPE не пресетим: его заполнит из ответа
        ConfluenceHttpTransport.
        Если ответ почему-то без Content-Type, downstream'у доступен
        attachment.media_type из ConfluenceKeys.ATTACHMENT_INFO.
        """
        meta = Metadata.empty().set(ConfluenceKeys.ATTACHMENT_INFO, attachment)
        if (page_id := parent_metadata.get(ConfluenceKeys.PAGE_ID)) is not None:
            meta = meta.set(ConfluenceKeys.PAGE_ID, page_id)
        if (host := parent_metadata.get(ConfluenceKeys.HOST)) is not None:
            meta = meta.set(ConfluenceKeys.HOST, host)
        if (space := parent_metadata.get(ConfluenceKeys.SPACE_KEY)) is not None:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, space)
        ancestors = parent_metadata.get(ConfluenceKeys.ANCESTORS_TITLES)
        if ancestors is not None:
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ancestors)
        # URL родительской страницы (её _links.webui, записан декодером в SOURCE_URL)
        if (parent_url := parent_metadata.get(ConfluenceKeys.SOURCE_URL)) is not None:
            meta = meta.set(ConfluenceKeys.PARENT_URL, parent_url)
        # SOURCE_URL вложения = его UI-link (webui); фолбэк — download-URL
        base = base_url.rstrip("/")
        att_url = (
            f"{base}{attachment.webui}" if attachment.webui
            else f"{base}{attachment.download_path}"
        )
        meta = meta.set(ConfluenceKeys.SOURCE_URL, att_url)
        return ConfluenceRequest(
            http=HttpRequest(url=attachment.download_path, method="GET"),
            metadata=meta,
        )


class ConfluencePaginator:
    """httpx-клиент для пагинированных Confluence REST discovery-запросов.

    Каждый постраничный GET повторяется до retry_attempts раз (из web-профиля)
    на 5xx и transport-ошибках (timeout/connect) с линейным backoff'ом — большие
    Confluence (напр. Apache cwiki) отдают нестабильные 500 на глубокой
    пагинации. 4xx (клиентские) не ретраятся. После исчерпания попыток
    исключение пробрасывается наверх (caller решает: fail или degrade).

    Исполнение и retry (5xx/transport) — внутри HttpTransport, собранного из
    conn.profile; пагинатор лишь строит path и парсит JSON, а base_url к нему
    подставляет httpx-клиент из профиля (HttpTransport(base_url=...)).
    """

    def __init__(self, conn: ConfluenceConnection):
        self._http = HttpTransport(conn.profile)

    def __call__(self, path: str, item: type[T]) -> Iterator[T]:
        """
        Выполняет запрос и возвращает итератор по результату
        """
        next_path: str | None = path
        while next_path:
            data = self._get_json(next_path)
            for raw in ConfluenceJson.results(data):
                yield item.model_validate(raw)

            next_path = ConfluenceJson.next_link(data)

    def _get_json(self, path: str) -> dict[str, Any]:
        """GET path -> JSON; retry (5xx/transport) выполняет HttpTransport.

        path — относительный (`/rest/api/...`, в т.ч. next-link) или абсолютный;
        httpx-клиент сам приклеит base_url из профиля либо возьмёт absolute как есть.
        """
        with self._http.fetch(HttpRequest(url=path)) as resp:
            return json.loads(resp.stream.read())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._http.close()

    @classmethod
    def discover_spaces(
        cls, conn: ConfluenceConnection, space_type: str,
    ) -> Iterable[str]:
        """Yield space-keys через /rest/api/space."""
        with cls(conn) as paginator:
            for item in paginator(
                ConfluenceRest.space_list_path(space_type), ConfluenceSpaceItem,
            ):
                if item.key:
                    yield item.key

    @classmethod
    def discover_space_pages(
        cls, conn: ConfluenceConnection, space_key: str,
    ) -> Iterable[str]:
        """Yield page-id всех страниц в space через /rest/api/space/{key}/content."""
        with cls(conn) as paginator:
            for item in paginator(
                ConfluenceRest.space_pages_path(space_key), ConfluencePageItem,
            ):
                page_id = item.id.strip()
                if page_id:
                    yield page_id

    @classmethod
    def discover_pages_by_cql(
        cls, conn: ConfluenceConnection, cql: str,
    ) -> Iterable[str]:
        """Yield page-id страниц по CQL-запросу через /rest/api/content/search."""
        with cls(conn) as paginator:
            for item in paginator(
                ConfluenceRest.cql_search_path(cql), ConfluencePageItem,
            ):
                page_id = item.id.strip()
                if page_id:
                    yield page_id


class ConfluenceCqlRequestSource(RequestSource[ConfluenceRequest]):
    """CQL-запрос: space = DOCS AND lastModified > '2024-01-01' и т.п.

    Discovery — через /rest/api/content/search?cql=... с пагинацией.
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        cql: str,
        body_format: str = "export_view",
    ) -> None:
        self._conn = conn
        self._cql = cql
        self._body_format = body_format
        self._host = ConfluenceRest.extract_host(conn.base_url)

    def requests(self) -> Iterable[ConfluenceRequest]:

        for page_id in ConfluencePaginator.discover_pages_by_cql(self._conn, self._cql):
            yield ConfluenceRest.make_page_request(
                host=self._host,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluencePagesRequestSource(RequestSource[ConfluenceRequest]):
    """Явный список page-id'ов; без discovery — page_ids фиксированы в ctor'е."""

    def __init__(
        self,
        *,
        base_url: str,
        page_ids: Sequence[str],
        body_format: str,
    ) -> None:
        self._host = ConfluenceRest.extract_host(base_url)
        self._page_ids = list(page_ids)
        self._body_format = body_format

    def requests(self) -> Iterable[ConfluenceRequest]:
        for page_id in self._page_ids:
            yield ConfluenceRest.make_page_request(
                host=self._host,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluenceSpaceRequestSource(RequestSource[ConfluenceRequest]):
    """Все страницы space через /rest/api/space/{key}/content."""

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_key: str,
        body_format: str,
    ) -> None:
        self._conn = conn
        self._space_key = space_key
        self._body_format = body_format
        self._host = ConfluenceRest.extract_host(conn.base_url)

    def requests(self) -> Iterable[ConfluenceRequest]:

        for page_id in ConfluencePaginator.discover_space_pages(
            self._conn, self._space_key,
        ):
            yield ConfluenceRest.make_page_request(
                host=self._host,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluenceMultiSpaceRequestSource(RequestSource[ConfluenceRequest]):
    """Все страницы из НЕСКОЛЬКИХ space'ов — последовательно через
    ConfluenceSpaceRequestSource для каждого ключа.

    Pipeline-семантика: всё ведёт себя как ОДНА выгрузка над union страниц.
    Cleanup идёт через touch-based mark (reconcile refresh'ит updated_at для
    всех виденных chunk'ов; FullCleanup сносит остальные).
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_keys: Sequence[str],
        body_format: str,
    ) -> None:
        if not space_keys:
            raise ValueError("space_keys пуст")
        self._inner = [
            ConfluenceSpaceRequestSource(
                conn=conn,
                space_key=k,
                body_format=body_format,
            )
            for k in space_keys
        ]
        self._space_keys = tuple(space_keys)
        self._host = ConfluenceRest.extract_host(conn.base_url)

    def requests(self) -> Iterable[ConfluenceRequest]:
        for src in self._inner:
            yield from src.requests()


class ConfluenceCqlSearchRequestSource(RequestSource[ConfluenceRequest]):
    """CQL-запрос -> один HttpRequest на /content/search.

    В отличии от ConfluenceCqlRequestSource (discovery: один request на
    каждую найденную страницу), этот источник эмитит ОДИН request на сам
    search-endpoint и оставляет JSON-ответ нетронутым — его разбирает
    ConfluenceSearchHitsReader.
    """

    def __init__(
        self,
        *,
        base_url: str,
        cql: str,
        limit: int,
    ) -> None:
        self._host = ConfluenceRest.extract_host(base_url)
        self._cql = cql
        self._limit = limit

    def requests(self) -> Iterable[ConfluenceRequest]:
        path = ConfluenceRest.cql_search_path(
            self._cql,
            limit=self._limit,
            expand="body.view,version,space",
        )

        # online-tool: не индексируется (per-hit source_id проставляет
        # ConfluenceSearchHitsReader), source_id запроса (его выведет транспорт
        # из search-URL) никуда не пишется. http.url — относительный path.
        yield ConfluenceRequest(
            http=HttpRequest(url=path, method="GET"),
            metadata=Metadata.empty().set(ConfluenceKeys.HOST, self._host),
        )
