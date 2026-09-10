"""Доступ к Confluence Server REST: CQL, пути, пагинатор, discovery и проба.

- ConfluenceCql        — сборка CQL для трёх режимов обхода и пробы существования.
- ConfluenceRest       — фабрики путей и запросов страниц и вложений.
- ConfluencePaginator  — httpx-клиент для пагинированных discovery-запросов.
- ConfluenceDiscovery  — RequestSource: список страниц с версиями и вложениями
  без тел; запрос на каждую страницу и на каждое вложение, прошедшее гейт.
- ConfluenceProbe      — SourceProbe: какие из невиденных страниц исчезли.

Ошибки:
TransportError — Confluence недоступен, ответил статусом или оборвал тело.
ConfluencePayloadError — ответ списка не разбирается как контент Confluence.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ValidationError

from boba.indexing import (
    Metadata,
    ReaderKeys,
    Request,
    RequestSource,
    SourceId,
    SourceLedger,
    SourceMark,
    SourceProbe,
    SourceRecord,
    TransportError,
    TransportKeys,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import (
    AttachmentBlock,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
    ConfluenceAttachmentItem,
    ConfluenceContent,
    ConfluenceKeys,
    ConfluenceMarks,
    ConfluencePayloadError,
    ConfluenceSourceId,
    ConfluenceSpaceItem,
    ParseGrade,
)
from boba.tool.kb.confluence.parsing import ConfluenceJson
from boba.tool.kb.indexing_log import IngestProgress
from boba.toolkit.timing import Elapsed
from boba.transport.http import CancellableHttpTransport, HttpRequest
from boba.transport.http.profile import HttpConnection

__all__ = [
    "ConfluenceCql",
    "ConfluenceDiscovery",
    "ConfluencePaginator",
    "ConfluenceProbe",
    "ConfluenceRequest",
    "ConfluenceRest",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ConfluenceRequest(Request):
    """Индексационный план запроса Confluence: чистый HTTP, metadata, отметка.

    source_id НЕ часть запроса: его выводит транспорт из реально запрашиваемого
    URL (корень профиля + http.url, без волатильного query) через
    ConfluenceSourceId. Поэтому http.url несёт только path. mark — отпечаток
    версии из списка, по которому конвейер решает, качать ли тело.
    """

    http: HttpRequest
    mark: SourceMark
    metadata: Metadata = field(default_factory=Metadata.empty)


class ConfluenceCql:
    """Сборка CQL: режимы обхода и проба существования одной грамматикой."""

    @staticmethod
    def literal(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @classmethod
    def space(cls, space_key: str) -> str:
        return f"space = {cls.literal(space_key)} and type = page"

    @classmethod
    def page(cls, page_id: str) -> str:
        return f"id = {cls.literal(page_id)}"

    @classmethod
    def ids(cls, page_ids: Iterable[str]) -> str:
        quoted: list[str] = []
        for page_id in page_ids:
            quoted.append(cls.literal(page_id))

        return f"id in ({', '.join(quoted)})"


class ConfluenceRest:
    """Фабрики Confluence REST: URL/path-builders и HttpRequest-конструкторы."""

    DEFAULT_PAGE_LIMIT: ClassVar[int] = 50

    DISCOVERY_EXPAND: ClassVar[str] = (
        "version,space,ancestors,"
        "children.attachment.version,children.attachment.extensions"
    )
    """Что раскрывать в списке: версии и вложения без тел страниц."""

    ATTACHMENTS_EXPAND: ClassVar[str] = "version"

    @staticmethod
    def page_fetch_path(page_id: str, *, body_format: str) -> str:
        """Страница целиком: тело и вложения — для инструментов чтения."""
        expand = (
            f"body.{body_format},version,ancestors,space,metadata.labels,"
            "children.attachment.version,children.attachment.extensions"
        )
        segment = ConfluenceRest._segment(page_id)
        query = ConfluenceRest._query({"expand": expand})
        return f"/rest/api/content/{segment}?{query}"

    @staticmethod
    def page_body_path(page_id: str, *, body_format: str) -> str:
        """Тело страницы для индексации; вложения уже известны из списка."""
        expand = f"body.{body_format},version,ancestors,space,metadata.labels"
        segment = ConfluenceRest._segment(page_id)
        query = ConfluenceRest._query({"expand": expand})
        return f"/rest/api/content/{segment}?{query}"

    @staticmethod
    def attachments_path(
        page_id: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> str:
        """Полный список вложений страницы: раскрытие в списке ограничено."""
        segment = ConfluenceRest._segment(page_id)
        params: dict[str, object] = {
            "limit": limit,
            "start": 0,
            "expand": ConfluenceRest.ATTACHMENTS_EXPAND,
        }
        query = ConfluenceRest._query(params)
        return f"/rest/api/content/{segment}/child/attachment?{query}"

    @staticmethod
    def space_path(space_key: str) -> str:
        """Один space: 404 на несуществующий ключ."""
        return f"/rest/api/space/{ConfluenceRest._segment(space_key)}"

    @staticmethod
    def space_list_path(
        space_type: str,
        *,
        expand: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> str:
        params: dict[str, object] = {"limit": limit, "start": 0}
        if space_type != "any":
            params["type"] = space_type

        if expand:
            params["expand"] = expand

        return f"/rest/api/space?{ConfluenceRest._query(params)}"

    @staticmethod
    def cql_search_path(
        cql: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        start: int = 0,
        expand: str | None = None,
    ) -> str:
        params: dict[str, object] = {"cql": cql, "limit": limit, "start": start}
        if expand:
            params["expand"] = expand

        return f"/rest/api/content/search?{ConfluenceRest._query(params)}"

    @staticmethod
    def _segment(value: str) -> str:
        """Сегмент пути: id и ключи идут от LLM, `/`, `?`, `#` в них — просто байты."""
        return quote(value, safe="")

    @staticmethod
    def _query(params: Mapping[str, object]) -> str:
        """Query-строка; запятая в expand остаётся запятой, как ждёт Confluence."""
        return urlencode(params, quote_via=quote, safe=",")

    @staticmethod
    def make_page_request(
        *,
        profile: HttpConnection,
        content: ConfluenceContent,
        body_format: str,
    ) -> ConfluenceRequest:
        path = ConfluenceRest.page_body_path(content.id, body_format=body_format)
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, content.id)
            .set(ConfluenceKeys.HOST, profile.address_host())
        )
        return ConfluenceRequest(
            http=HttpRequest(url=path, method="GET"),
            mark=ConfluenceMarks.page(content.version.number),
            metadata=meta,
        )

    @staticmethod
    def make_attachment_request(
        *,
        profile: HttpConnection,
        page: ConfluenceContent,
        page_source: SourceId,
        attachment: AttachmentInfo,
        grade: ParseGrade,
    ) -> ConfluenceRequest:
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.ATTACHMENT_INFO, attachment)
            .set(TransportKeys.CONTENT_TYPE, attachment.media_type)
            .set(ReaderKeys.PAGE_TITLE, attachment.title)
            .set(ConfluenceKeys.PAGE_ID, page.id)
            .set(ConfluenceKeys.HOST, profile.address_host())
        )
        if page.space.key:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, page.space.key)

        if ancestors := page.ancestor_titles():
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ancestors)

        if page.links.webui:
            parent_url = str(profile.url_of(page.links.webui))
            meta = meta.set(ConfluenceKeys.PARENT_URL, parent_url)

        att_path = attachment.download_path
        if attachment.webui:
            att_path = attachment.webui

        meta = meta.set(ConfluenceKeys.SOURCE_URL, str(profile.url_of(att_path)))
        return ConfluenceRequest(
            http=HttpRequest(url=attachment.download_path, method="GET"),
            mark=ConfluenceMarks.attachment(
                attachment, parent=page_source, grade=grade
            ),
            metadata=meta,
        )


class ConfluencePaginator:
    """httpx-клиент для пагинированных Confluence REST discovery-запросов.

    Исполнение и retry (5xx/transport) — внутри HttpTransport, собранного из
    conn.profile; пагинатор лишь строит path, ходит по `_links.next` и
    разбирает результаты в модель item.
    """

    def __init__(self, conn: ConfluenceConnection):
        self._http = CancellableHttpTransport(conn.profile)

    async def __call__(self, path: str, item: type[T]) -> AsyncIterator[T]:
        next_path: str | None = path
        while next_path:
            data = await self.get_json(next_path)
            results = ConfluenceJson.results(data)
            next_path = ConfluenceJson.next_link(data)
            logger.info(
                "discovery page: %d items, next=%s",
                len(results),
                bool(next_path),
            )
            for raw in results:
                yield self._item(item, raw, path)

    @staticmethod
    def _item(item: type[T], raw: dict[str, Any], path: str) -> T:
        try:
            return item.model_validate(raw)
        except ValidationError as exc:
            msg = (
                f"confluence discovery: GET {path} expected {item.__name__} items, "
                f"got {json.dumps(raw)[:200]}: {exc}"
            )
            raise ConfluencePayloadError(msg) from exc

    async def get_json(self, path: str) -> dict[str, Any]:
        """Один GET с разбором JSON: статус и обрыв уходят TransportError."""
        logger.info("discovery request: GET %s", path)
        elapsed = Elapsed()
        try:
            async with self._http.fetch(HttpRequest(url=path)) as resp:
                payload = await resp.stream.read()
        except httpx.HTTPError as exc:
            msg = f"GET {path} on confluence: {type(exc).__name__}: {exc}"
            raise TransportError(msg) from exc

        logger.info("discovery response: %d bytes in %dms", len(payload), elapsed.ms())
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            msg = (
                f"GET {path} on confluence: expected JSON, got {payload[:200]!r}: {exc}"
            )
            raise ConfluencePayloadError(msg) from exc

        return ConfluenceJson.as_dict(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._http.close()

    @classmethod
    async def discover_spaces(
        cls,
        conn: ConfluenceConnection,
        space_type: str,
    ) -> AsyncIterator[str]:
        async with cls(conn) as paginator:
            async for item in paginator(
                ConfluenceRest.space_list_path(space_type),
                ConfluenceSpaceItem,
            ):
                if item.key:
                    yield item.key


class ConfluenceDiscovery(RequestSource[ConfluenceRequest]):
    """Обход по CQL: страницы с версиями и вложениями, без тел.

    На каждую страницу — запрос тела с отметкой версии; конвейер сам решит по
    реестру, нужно ли его исполнять. На каждое вложение, прошедшее гейт, —
    запрос скачивания с отпечатком из списка. Вложения, отсечённые гейтом,
    отмечаются в реестре увиденными: они существуют, просто не индексируются.
    """

    def __init__(  # noqa: PLR0913 — обход, гейт, реестр и счёт независимы
        self,
        *,
        conn: ConfluenceConnection,
        cql: str,
        gate: AttachmentGate,
        grade: ParseGrade,
        ledger: SourceLedger,
        progress: IngestProgress,
    ) -> None:
        self._conn = conn
        self._cql = cql
        self._gate = gate
        self._grade = grade
        self._ledger = ledger
        self._progress = progress

    @property
    def cql(self) -> str:
        return self._cql

    async def requests(self) -> AsyncIterator[ConfluenceRequest]:
        logger.info("discovery start: %s", self._cql)
        path = ConfluenceRest.cql_search_path(
            self._cql,
            expand=ConfluenceRest.DISCOVERY_EXPAND,
        )
        async with ConfluencePaginator(self._conn) as paginator:
            async for content in paginator(path, ConfluenceContent):
                self._progress.pages_found(1)
                yield ConfluenceRest.make_page_request(
                    profile=self._conn.profile,
                    content=content,
                    body_format=self._conn.body_format,
                )

                async for request in self._attachment_requests(paginator, content):
                    yield request

        self._progress.pages_closed()

    async def _attachment_requests(
        self,
        paginator: ConfluencePaginator,
        content: ConfluenceContent,
    ) -> AsyncIterator[ConfluenceRequest]:
        profile = self._conn.profile
        page_source = ConfluenceSourceId.of(
            profile,
            ConfluenceRest.page_body_path(
                content.id, body_format=self._conn.body_format
            ),
        )
        async for att in self._attachments(paginator, content):
            verdict = self._gate.verdict(att)
            if verdict is not AttachmentVerdict.TAKE:
                source = ConfluenceSourceId.of(profile, att.download_path)
                await self._ledger.touch([source], at=time.time())
                logger.info(
                    "attachment skipped (%s): id=%s title=%r media_type=%r",
                    verdict.value,
                    att.id,
                    att.title,
                    att.media_type,
                )
                continue

            self._progress.attachments_found(1)
            yield ConfluenceRest.make_attachment_request(
                profile=profile,
                page=content,
                page_source=page_source,
                attachment=att,
                grade=self._grade,
            )

    @staticmethod
    async def _attachments(
        paginator: ConfluencePaginator,
        content: ConfluenceContent,
    ) -> AsyncIterator[AttachmentInfo]:
        """Вложения страницы: из раскрытия списка, а при усечении — полным списком."""
        block: AttachmentBlock = content.children.attachment
        if not block.truncated():
            for item in block.results:
                yield item.info()

            return

        logger.info(
            "page %s: attachment expansion truncated at %d, listing all",
            content.id,
            block.size,
        )
        path = ConfluenceRest.attachments_path(content.id)
        async for item in paginator(path, ConfluenceAttachmentItem):
            yield item.info()


class ConfluenceProbe(SourceProbe):
    """Проба существования страниц одним CQL `id in (...)` на пакет.

    В ответе поиска только живые и доступные учётке страницы; остальные из
    пакета исчезли, и их вместе с вложениями снимает конвейер.
    """

    def __init__(self, conn: ConfluenceConnection) -> None:
        self._conn = conn

    async def gone(self, records: Sequence[SourceRecord]) -> Sequence[SourceId]:
        by_page: dict[str, SourceId] = {}
        for record in records:
            page_id = ConfluenceSourceId.page_id_of(record.source_id)
            if page_id is None:
                continue

            by_page[page_id] = record.source_id

        if not by_page:
            return ()

        alive = await self._alive(list(by_page))
        gone: list[SourceId] = []
        for page_id, source_id in by_page.items():
            if page_id in alive:
                continue

            gone.append(source_id)

        logger.info("probe: %d pages asked, %d gone", len(by_page), len(gone))
        return gone

    async def _alive(self, page_ids: Sequence[str]) -> set[str]:
        path = ConfluenceRest.cql_search_path(
            ConfluenceCql.ids(page_ids),
            limit=len(page_ids),
        )
        alive: set[str] = set()
        async with ConfluencePaginator(self._conn) as paginator:
            async for content in paginator(path, ConfluenceContent):
                alive.add(content.id)

        return alive
