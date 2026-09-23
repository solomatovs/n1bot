"""Обход Confluence для конвейера индексации: режимы списка и discovery.

- ContentListing       — откуда берётся список страниц: SpaceListing (список
  контента спейса), CqlListing (поиск по CQL), PageListing (одна страница).
- ConfluenceDiscovery  — RequestSource: страницы с версиями и вложениями без
  тел; запрос на каждую страницу и на каждое вложение, прошедшее гейт.

Адреса, пагинатор и endpoint живут в boba.confluence.rest.

Ошибки:
TransportError — Confluence недоступен, ответил статусом или оборвал тело.
ConfluencePayloadError — ответ списка не разбирается как контент Confluence.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from boba.confluence.models import (
    AttachmentBlock,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
    ConfluenceAttachmentItem,
    ConfluenceContent,
    ConfluenceSourceIds,
    ParseGrade,
)
from boba.confluence.rest import (
    CflPaginator,
    CflRestBuilder,
    ConfluenceConnection,
    ConfluenceRequest,
)
from boba.indexing import RequestSource
from boba.tool.confluence.indexing_log import IngestProgress
from boba.transport.http import TransportError

__all__ = [
    "ConfluenceDiscovery",
    "ContentListing",
    "CqlListing",
    "PageListing",
    "SpaceListing",
]

logger = logging.getLogger(__name__)


class ContentListing(ABC):
    """Откуда прогон берёт список страниц: спейс, запрос CQL или одна страница.

    Базовый класс трёх режимов обхода. Реализацию выбирает IngestScope и
    отдаёт ConfluenceDiscovery, который из полученных страниц строит запросы
    тел и вложений — сами режимы отличаются только источником списка.
    """

    @abstractmethod
    def label(self) -> str:
        """Что обходим — для логов и сообщений об ошибках."""
        ...

    @abstractmethod
    def contents(self, paginator: CflPaginator) -> AsyncIterator[ConfluenceContent]:
        """Страницы обхода: версии и вложения, без тел."""
        ...

    def missing(self) -> Sequence[str]:
        """Id страниц, которых источник данных не отдал; проверяет их конвейер."""
        return ()


class SpaceListing(ContentListing):
    """Страницы спейса списком контента, а не поиском.

    Поиск Confluence не видит архивные спейсы и отстаёт от свежих правок, а
    список спейса читает базу, поэтому обход спейса идёт им.
    """

    def __init__(self, space_key: str) -> None:
        self._space_key = space_key
        self._rest = CflRestBuilder()

    def label(self) -> str:
        return f"space {self._space_key}"

    def contents(self, paginator: CflPaginator) -> AsyncIterator[ConfluenceContent]:
        return paginator(
            self._rest.space_content_path(self._space_key),
            ConfluenceContent,
        )


class CqlListing(ContentListing):
    """Страницы выборки CQL: запрос задаёт вызывающий, поиск исполняет.

    Всё, что вне поискового индекса (архивные спейсы, только что созданные
    страницы), в такую выборку не попадает — это свойство самого поиска.
    """

    def __init__(self, cql: str) -> None:
        self._cql = cql
        self._rest = CflRestBuilder()

    @property
    def cql(self) -> str:
        return self._cql

    def label(self) -> str:
        return f"cql {self._cql}"

    def contents(self, paginator: CflPaginator) -> AsyncIterator[ConfluenceContent]:
        return paginator(
            self._rest.cql_search_path(
                self._cql,
                expand=CflRestBuilder.DISCOVERY_EXPAND,
            ),
            ConfluenceContent,
        )


class PageListing(ContentListing):
    """Одна страница по id: прямой запрос вместо поиска.

    Страница читается по адресу, поэтому режим работает и в архивном спейсе.
    Ответ 404 запоминается в missing(): страницы нет, и конвейер снимет её
    с индекса вместе с вложениями.
    """

    def __init__(self, page_id: str) -> None:
        self._page_id = page_id
        self._rest = CflRestBuilder()
        self._missing: list[str] = []

    def label(self) -> str:
        return f"page {self._page_id}"

    async def contents(
        self, paginator: CflPaginator
    ) -> AsyncIterator[ConfluenceContent]:
        self._missing = []
        try:
            content = await paginator.one(
                self._rest.page_summary_path(self._page_id),
                ConfluenceContent,
            )
        except TransportError as exc:
            logger.info("page %s is not readable: %s", self._page_id, exc)
            self._missing.append(self._page_id)
            return

        yield content

    def missing(self) -> Sequence[str]:
        return tuple(self._missing)


class ConfluenceDiscovery(RequestSource[ConfluenceRequest]):
    """Обход по CQL: страницы с версиями и вложениями, без тел.

    На каждую страницу — запрос тела с отметкой версии; конвейер сам решит по
    реестру, нужно ли его исполнять. На каждое вложение — запрос скачивания с
    отпечатком из списка, а отсечённое гейтом уходит с причиной в отметке:
    конвейер его не скачивает, но помнит, что оно существует.
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        listing: ContentListing,
        gate: AttachmentGate,
        grade: ParseGrade,
        progress: IngestProgress,
    ) -> None:
        self._conn = conn
        self._rest = CflRestBuilder()
        self._source_ids = ConfluenceSourceIds()
        self._listing = listing
        self._gate = gate
        self._grade = grade
        self._progress = progress

    @property
    def listing(self) -> ContentListing:
        return self._listing

    async def requests(self) -> AsyncIterator[ConfluenceRequest]:
        logger.info("discovery start: %s", self._listing.label())
        async with CflPaginator(self._conn) as paginator:
            async for content in self._listing.contents(paginator):
                self._progress.pages_found(1)
                yield self._rest.make_page_request(
                    connection=self._conn.connection,
                    content=content,
                    body_format=self._conn.body_format,
                )

                async for request in self._attachment_requests(paginator, content):
                    yield request

            for page_id in self._listing.missing():
                yield self._rest.make_gone_request(
                    connection=self._conn.connection,
                    page_id=page_id,
                    body_format=self._conn.body_format,
                )

        self._progress.pages_closed()

    async def _attachment_requests(
        self,
        paginator: CflPaginator,
        content: ConfluenceContent,
    ) -> AsyncIterator[ConfluenceRequest]:
        connection = self._conn.connection
        body_url = self._rest.page_body_path(
            content.id, body_format=self._conn.body_format
        )
        page_source = self._source_ids.of(connection, str(body_url))
        async for att in self._attachments(paginator, content):
            self._progress.attachments_found(1)
            verdict = self._gate.verdict(att)
            if verdict is not AttachmentVerdict.TAKE:
                logger.info(
                    "attachment skipped (%s): id=%s title=%r media_type=%r",
                    verdict.value,
                    att.id,
                    att.title,
                    att.media_type,
                )

            yield self._rest.make_attachment_request(
                connection=connection,
                page=content,
                page_source=page_source,
                attachment=att,
                grade=self._grade,
                skip=verdict.skipped(),
            )

    async def _attachments(
        self,
        paginator: CflPaginator,
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
        path = self._rest.attachments_path(content.id)
        async for item in paginator(path, ConfluenceAttachmentItem):
            yield item.info()
