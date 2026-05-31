"""Доступ к Confluence Server REST: URL-builder, пагинатор, `RequestSource`'ы.

- `ConfluenceRest`        — @staticmethod-фабрики путей и `HttpRequest`'ов
  (viewpage URL, content/space/cql пути, page/attachment-запросы).
- `ConfluencePaginator`   — httpx-клиент для пагинированных discovery-запросов
  (+ `discover_spaces/discover_space_pages/discover_pages_by_cql`).
- `RequestSource`'ы       — CQL / Pages / Space / MultiSpace (discovery для
  ingest), и `CqlSearch` (один запрос на `/content/search` для online-tool'ов).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, ClassVar, TypeVar
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel

from boba.indexing import (
    Metadata,
    PipelineContext,
    RequestSource,
    SourceId,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import (
    AttachmentInfo,
    ConfluenceKeys,
    ConfluencePageItem,
    ConfluenceSpaceItem,
)
from boba.transport.http import HttpRequest

__all__ = [
    "ConfluenceCqlRequestSource",
    "ConfluenceCqlSearchRequestSource",
    "ConfluenceMultiSpaceRequestSource",
    "ConfluencePagesRequestSource",
    "ConfluencePaginator",
    "ConfluenceRest",
    "ConfluenceSpaceRequestSource",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ConfluenceRest:
    """Фабрики Confluence REST: URL/path-builders и `HttpRequest`-конструкторы."""

    DEFAULT_PAGE_LIMIT: ClassVar[int] = 50

    @staticmethod
    def viewpage_url(base_url: str, page_id: str) -> str:
        """Stable canonical URL страницы — `{base}/pages/viewpage.action?pageId={id}`.

        Используется как `Request.source_id` (отдельно от URL REST-запроса).
        Не зависит от `body_format` или других expand-параметров; стабилен при
        изменении REST-эндпоинтов; кликабелен в браузере.
        """
        return f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"

    @staticmethod
    def page_fetch_path(page_id: str, *, body_format: str) -> str:
        """`/rest/api/content/{id}?expand=…` — выгрузка одной страницы с expand-полями.

        `children.attachment` раскрывает список вложений (`results[]` с
        `id`/`title`/`extensions.mediaType`/`extensions.fileSize`/`_links.download`/
        `version.number`) прямо в основном ответе — это позволяет Decoder'у
        положить их в `ConfluenceKeys.ATTACHMENTS` для последующего fan-out'а
        без дополнительного round-trip'а на `/child/attachment`.
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
        """`/rest/api/space?…` — список space'ов с опциональным фильтром по типу.

        `space_type` ∈ {`global`, `personal`, `any`}: `any` снимает фильтр.
        `expand` — необязательное (например, `description.plain`).
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
        """`/rest/api/space/{key}/content?type=page&…` — все страницы space'а."""
        return f"/rest/api/space/{space_key}/content?type=page&limit={limit}&start=0"

    @staticmethod
    def cql_search_path(
        cql: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        expand: str | None = None,
    ) -> str:
        """`/rest/api/content/search?cql=…&…` — CQL-поиск страниц.

        `cql` URL-encode'ится здесь; не нужно quote'ить заранее.
        `expand` — необязательное (например, `body.view,version,space`).
        """
        expand_q = f"&expand={expand}" if expand else ""
        cql_q = quote(cql, safe="")
        return f"/rest/api/content/search?cql={cql_q}&limit={limit}{expand_q}"

    @staticmethod
    def extract_host(base_url: str) -> str:
        """
        выбирает только netloc из переданного url
        `https://confl.x.com/wiki/` → `confl.x.com`
        """
        netloc = urlparse(base_url).netloc
        return netloc or base_url.split("://", 1)[-1].split("/", 1)[0]

    @staticmethod
    def make_page_request(
        *,
        base_url: str,
        host: str,
        auth: httpx.Auth | None,
        page_id: str,
        body_format: str,
    ) -> HttpRequest:
        """HttpRequest на выгрузку страницы.

        `url`        — absolute REST endpoint (что Transport исполняет).
        `source_id`  — stable viewpage URL (canonical id документа). Отдельный
                       от REST URL, который меняется с эндпоинтами.
        `metadata`   — page_id и host для дальнейших стадий (Decoder/Reader).
        """
        path = ConfluenceRest.page_fetch_path(page_id, body_format=body_format)
        view_url = ConfluenceRest.viewpage_url(base_url, page_id)
        return HttpRequest(
            url=f"{base_url.rstrip('/')}{path}",
            method="GET",
            auth=auth,
            source_id=SourceId(view_url),
            metadata=(
                Metadata.empty()
                .set(ConfluenceKeys.SOURCE_URL, view_url)
                .set(ConfluenceKeys.PAGE_ID, page_id)
                .set(ConfluenceKeys.HOST, host)
            ),
        )

    @staticmethod
    def make_attachment_request(
        *,
        base_url: str,
        auth: httpx.Auth | None,
        parent_metadata: Metadata,
        attachment: AttachmentInfo,
    ) -> HttpRequest:
        """HttpRequest на бинарную выгрузку одного вложения.

        `url`            — `{base_url}{attachment.download_path}` (Confluence отдаёт
                           `_links.download` с `?version=&modificationDate=`).
        `source_id`      — тот же абсолютный download-URL: стабильный
                           per-attachment-version, уникален в pipeline, кликабелен.
        `metadata`       — `ATTACHMENT_INFO` (полный snapshot одного attachment'а —
                           маркер того, что этот `RawDocument` — вложение, и носитель
                           полей для download'а) + `PAGE_ID`/`HOST`/`SPACE_KEY`/
                           `ANCESTORS_TITLES` родительской страницы (чтобы download мог
                           положить файл в ту же папку, что и сам page).

        `TransportKeys.CONTENT_TYPE` не пресетим: HttpTransport заполнит из ответа.
        Если ответ почему-то без `Content-Type`, downstream'у доступен
        `attachment.media_type` из `ConfluenceKeys.ATTACHMENT_INFO`.
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
        url = f"{base_url.rstrip('/')}{attachment.download_path}"
        meta = meta.set(ConfluenceKeys.SOURCE_URL, url)
        return HttpRequest(
            url=url,
            method="GET",
            auth=auth,
            source_id=SourceId(url),
            metadata=meta,
        )


class ConfluencePaginator:
    """httpx-клиент для пагинированных Confluence REST discovery-запросов.

    Каждый постраничный GET повторяется до `MAX_ATTEMPTS` раз на 5xx и
    transport-ошибках (timeout/connect) с линейным backoff'ом — большие
    Confluence (напр. Apache cwiki) отдают нестабильные 500 на глубокой
    пагинации. 4xx (клиентские) не ретраятся. После исчерпания попыток
    исключение пробрасывается наверх (caller решает: fail или degrade).
    """

    MAX_ATTEMPTS: ClassVar[int] = 3
    RETRY_BACKOFF_SEC: ClassVar[float] = 1.0

    def __init__(self, conn: ConfluenceConnection):
        self._client = httpx.Client(
            base_url=conn.base_url.rstrip("/"),
            timeout=conn.timeout_sec,
            auth=conn.make_auth(),
            verify=conn.ssl_verify,
        )

    def __call__(self, path: str, item: type[T]) -> Iterator[T]:
        """
        Выполняет запрос и возвращает итератор по результату
        """
        next_path: str | None = path
        while next_path:
            data = self._get_json(next_path)
            for raw in self._extract_results(data):
                yield item.model_validate(raw)

            next_path = self._next_link(data)

    def _get_json(self, path: str) -> dict[str, Any]:
        """GET `path` → JSON c retry до `MAX_ATTEMPTS` на 5xx/transport-ошибках."""
        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                resp = self._client.get(path)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if not e.response.is_server_error:  # 4xx — не ретраим
                    raise
                last_exc = e
            except httpx.TransportError as e:  # timeout / connect — transient
                last_exc = e
            if attempt < self.MAX_ATTEMPTS:
                delay = self.RETRY_BACKOFF_SEC * attempt
                logger.warning(
                    "Confluence GET %s неудачно (%s); retry %d/%d через %.1fs",
                    path,
                    type(last_exc).__name__,
                    attempt,
                    self.MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
        if last_exc is None:  # недостижимо: цикл либо вернул, либо выставил last_exc
            msg = f"Confluence GET {path}: неизвестная ошибка"
            raise httpx.HTTPError(msg)
        raise last_exc

    @staticmethod
    def _extract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
        """top-level 'results' или вложенный 'page.results'."""
        res = data.get("results")
        if isinstance(res, list):
            return res

        res = data.get("page", {}).get("results")
        if isinstance(res, list):
            return res

        return []

    @staticmethod
    def _next_link(data: dict[str, Any]) -> str | None:
        next_path = data.get("_links", {}).get("next")
        if not next_path:
            return None

        return str(next_path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._client.close()

    @classmethod
    def discover_spaces(
        cls, conn: ConfluenceConnection, space_type: str,
    ) -> Iterable[str]:
        """Yield space-keys через `/rest/api/space`."""
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
        """Yield page-id всех страниц в space через `/rest/api/space/{key}/content`."""
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
        """Yield page-id страниц по CQL-запросу через `/rest/api/content/search`."""
        with cls(conn) as paginator:
            for item in paginator(
                ConfluenceRest.cql_search_path(cql), ConfluencePageItem,
            ):
                page_id = item.id.strip()
                if page_id:
                    yield page_id


class ConfluenceCqlRequestSource(RequestSource[HttpRequest]):
    """CQL-запрос: `space = DOCS AND lastModified > '2024-01-01'` и т.п.

    Discovery — через `/rest/api/content/search?cql=...` с пагинацией.
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

    def name(self) -> str:
        return f"ConfluenceCqlRequestSource({self._cql!r})"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        auth = self._conn.make_auth()
        for page_id in ConfluencePaginator.discover_pages_by_cql(self._conn, self._cql):
            yield ConfluenceRest.make_page_request(
                base_url=self._conn.base_url,
                host=self._host,
                auth=auth,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluencePagesRequestSource(RequestSource[HttpRequest]):
    """Явный список page-id'ов; без discovery — page_ids фиксированы в ctor'е."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: httpx.Auth | None,
        page_ids: Sequence[str],
        body_format: str = "export_view",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = ConfluenceRest.extract_host(base_url)
        self._auth = auth
        self._page_ids = list(page_ids)
        self._body_format = body_format

    def name(self) -> str:
        return f"ConfluencePagesRequestSource({len(self._page_ids)} pages)"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        for page_id in self._page_ids:
            yield ConfluenceRest.make_page_request(
                base_url=self._base_url,
                host=self._host,
                auth=self._auth,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluenceSpaceRequestSource(RequestSource[HttpRequest]):
    """Все страницы space через `/rest/api/space/{key}/content`."""

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_key: str,
        body_format: str = "export_view",
    ) -> None:
        self._conn = conn
        self._space_key = space_key
        self._body_format = body_format
        self._host = ConfluenceRest.extract_host(conn.base_url)

    def name(self) -> str:
        return f"ConfluenceSpaceRequestSource({self._host}/{self._space_key})"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        auth = self._conn.make_auth()
        for page_id in ConfluencePaginator.discover_space_pages(
            self._conn, self._space_key,
        ):
            yield ConfluenceRest.make_page_request(
                base_url=self._conn.base_url,
                host=self._host,
                auth=auth,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluenceMultiSpaceRequestSource(RequestSource[HttpRequest]):
    """Все страницы из НЕСКОЛЬКИХ space'ов — последовательно через
    `ConfluenceSpaceRequestSource` для каждого ключа.

    Pipeline-семантика: всё ведёт себя как ОДНА выгрузка над union страниц.
    Cleanup идёт через touch-based mark (reconcile refresh'ит updated_at для
    всех виденных chunk'ов; `FullCleanup` сносит остальные).
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_keys: Sequence[str],
        body_format: str = "export_view",
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

    def name(self) -> str:
        keys = ",".join(self._space_keys)
        return f"ConfluenceMultiSpaceRequestSource({self._host}/[{keys}])"

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        for src in self._inner:
            yield from src.stream(ctx)


class ConfluenceCqlSearchRequestSource(RequestSource[HttpRequest]):
    """CQL-запрос → один `HttpRequest` на `/content/search`.

    В отличии от `ConfluenceCqlRequestSource` (discovery: один request на
    каждую найденную страницу), этот источник эмитит ОДИН request на сам
    search-endpoint и оставляет JSON-ответ нетронутым — его разбирает
    `ConfluenceSearchHitsReader`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: httpx.Auth | None,
        cql: str,
        limit: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = ConfluenceRest.extract_host(base_url)
        self._auth = auth
        self._cql = cql
        self._limit = limit

    def name(self) -> str:
        return (
            f"ConfluenceCqlSearchRequestSource(cql={self._cql!r}, limit={self._limit})"
        )

    def stream(self, ctx: PipelineContext) -> Iterable[HttpRequest]:
        del ctx
        path = ConfluenceRest.cql_search_path(
            self._cql,
            limit=self._limit,
            expand="body.view,version,space",
        )

        yield HttpRequest(
            url=f"{self._base_url}{path}",
            method="GET",
            auth=self._auth,
            source_id=SourceId(f"confluence:cql:{self._cql}"),
            metadata=Metadata.empty().set(ConfluenceKeys.HOST, self._host),
        )
