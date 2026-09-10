"""Индексация Confluence и чтение вложений: функции уровня модуля.

Конвейер (обход, разбор, эмбеддинг, запись в kb_chunks) исполняется в теле —
потому оно живёт в песочнице; модель эмбеддера грузится один раз на прогон.

Ошибки:
PostgresError — до хранилища не достучаться.
LedgerError — реестр источников недоступен.
httpx.HTTPError — Confluence недоступен или ответил статусом (чтение вложения).
TransportError — список страниц забрать не удалось.
ConfluencePayloadError — Confluence ответил не тем JSON, который ждали.
AttachmentNotFoundError — вложения с таким именем на странице нет.
LiteParseError — вложение скачалось, но не разбирается.
EmbeddingError — удалённый эмбеддер недоступен или ответил мусором.
Сбой отдельной страницы или вложения ingest переживает сам: источник уходит
в счётчик failed, прогон идёт дальше.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.confluence.models import (
    ConfluenceKeys,
    ConfluencePayloadError,
    PageCardSection,
    PageParseRequest,
    PageSection,
    PageSectionBase,
    PageSections,
    PageTableSection,
    TableShape,
)
from boba.db.postgres import PostgresError
from boba.indexing import (
    DocumentCardSection,
    LedgerError,
    Metadata,
    OutlineEntry,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
    TableSection,
    TransportError,
)
from boba.llm.embedding import EmbeddingConfig, EmbeddingError
from boba.llm.warm import WarmEmbedder
from boba.text.document import LiteParseError, LiteParseParams
from boba.tool.confluence.indexing_log import IngestProgress, LoggingReader
from boba.tool.confluence.ingest_base import (
    ConfluenceIngest,
    ConfluenceIngestConfig,
    IngestReport,
    IngestScope,
)
from boba.tool.confluence.request_sources import ConfluenceUrl
from boba.tool.confluence.tools import ConfluenceHttp, ConfluenceToolsConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool, warmup
from boba.toolkit.result import MarkdownResult, TableResult
from boba.toolkit.timing import Elapsed
from boba.toolkit.types import SecretRevealing

logger = logging.getLogger("boba.tool.confluence.ingest")

_ATTACHMENTS_DESCRIPTION = (
    "Читать ли вложения страниц: true — все вложения, разрешённые "
    "администратором (документы, таблицы, презентации, текст; картинки только "
    "при ocr=true); false — только текст страниц. Неизменившиеся вложения не "
    "скачиваются повторно."
)
_OCR_DESCRIPTION = (
    "OCR вложений: true распознаёт текст по картинкам и сканам, false — только "
    "текстовый слой. OCR дорог: минуты и гигабайты памяти на документ. "
    "Вложение, уже разобранное с OCR, повторно не разбирается."
)


class AttachmentNotFoundError(Exception):
    """Вложения с таким именем на странице нет; текст готов для пользователя."""


class IngestErrorKind(StrEnum):
    """Ожидаемые отказы ingest-инструментов."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    REQUEST_FAILED = "ingest_request_failed"
    ATTACHMENT_NOT_FOUND = "attachment_not_found"
    DOCUMENT_UNREADABLE = "document_unreadable"
    EMBEDDING_FAILED = "embedding_failed"


class IngestToolConfig(SecretRevealing, ConfluenceIngestConfig):
    """Конфиг ingest-инструментов; секция [tool.ingest]."""

    SECTION: ClassVar[str] = "tool.ingest"


class IngestWarmupConfig(BaseModel):
    """Конфиг прогрева зиготы ingest: нужен только эмбеддер."""

    model_config = ConfigDict(extra="ignore")

    embedding: EmbeddingConfig


@warmup
async def warm_embedder(cfg: IngestWarmupConfig) -> None:
    """Модель эмбеддингов поднимается в зиготе: вызовы берут её через COW."""
    embedder = WarmEmbedder.load(cfg.embedding)
    await embedder.embed_query("warm-up")


class LocalConfluenceReader(Reader[str]):
    """HTML-страница -> карточка, секции по заголовкам и таблицы.

    bs4 работает прямо здесь, в теле инструмента; наружу разбор отдаёт
    wire-формат PageSections, который ридер валидирует обратно в модели и
    переводит в доменные секции конвейера.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence")
    DOC_TYPE: ClassVar[str] = "confluence_html"

    def __init__(self, table_shape: TableShape) -> None:
        self._table_shape = table_shape

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    async def read(self, value: RawDocument) -> AsyncIterator[Section[str]]:
        """Разбор HTML уходит в поток: bs4 на большой странице считает секунды."""
        payload = await value.handle.read()
        if not payload.strip():
            return

        html = payload.decode("utf-8", errors="replace")
        title = value.metadata.get(ReaderKeys.PAGE_TITLE) or ""

        # bs4 тяжёлый: в процесс приложения модуль инструментов его не тянет
        from boba.tool.confluence.html import PageOps  # noqa: PLC0415

        logger.info("html parse start: %s, %d bytes", title or "?", len(payload))
        elapsed = Elapsed()
        request = PageParseRequest(
            html=html,
            title=title,
            table_shape=self._table_shape,
        )
        answer = await asyncio.to_thread(
            PageOps.confluence_sections,
            request.model_dump(mode="json"),
        )
        parsed = PageSections.model_validate(answer)
        logger.info(
            "html parse done: %s -> %d sections in %dms",
            title or "?",
            len(parsed.sections),
            elapsed.ms(),
        )
        for row in parsed.sections:
            yield self._section(value, row)

    def _section(self, value: RawDocument, row: PageSection) -> Section[str]:
        if isinstance(row, PageTableSection):
            return self._table(value, row)

        if isinstance(row, PageCardSection):
            return self._card(value, row)

        return Section(
            source_id=value.source_id,
            content=row.content,
            order=row.order,
            metadata=self._meta(value, row),
            tags=self._tags(value),
        )

    def _table(self, value: RawDocument, row: PageTableSection) -> TableSection:
        return TableSection(
            source_id=value.source_id,
            content=row.caption,
            order=row.order,
            metadata=self._meta(value, row),
            tags=self._tags(value),
            caption=row.caption,
            columns=row.columns,
            rows=row.rows,
            layout=row.layout,
        )

    def _card(self, value: RawDocument, row: PageCardSection) -> DocumentCardSection:
        outline: list[OutlineEntry] = []
        for item in row.outline:
            outline.append(
                OutlineEntry(level=item.level, text=item.text, anchor=item.anchor)
            )

        meta = self._meta(value, row)
        if row.links:
            meta = meta.set(ConfluenceKeys.LINKS, row.links)

        breadcrumb = value.metadata.get(ConfluenceKeys.ANCESTORS_TITLES) or ()
        return DocumentCardSection(
            source_id=value.source_id,
            content=row.title,
            order=row.order,
            metadata=meta,
            tags=self._tags(value),
            title=row.title,
            breadcrumb=breadcrumb,
            outline=tuple(outline),
            labels=self._labels(value),
            links=row.links,
        )

    @staticmethod
    def _labels(value: RawDocument) -> tuple[str, ...]:
        return value.metadata.get(ConfluenceKeys.LABELS) or ()

    @classmethod
    def _tags(cls, value: RawDocument) -> frozenset[str]:
        """Метки страницы — теги чанка: колонка tags индексируется и умеет
        фильтры HasTag/HasAnyTag, json-metadata не умеет ни того, ни другого."""
        return frozenset(cls._labels(value))

    @classmethod
    def _meta(cls, value: RawDocument, row: PageSectionBase) -> Metadata:
        """Метки из metadata убираются: они уже уехали в tags, а держать одно
        и то же в двух колонках каждой строки — лишний вес индекса."""
        meta = value.metadata.without(ConfluenceKeys.LABELS)
        meta = meta.set(ReaderKeys.DOC_TYPE, cls.DOC_TYPE)
        if row.heading_path:
            meta = meta.set(SectionKeys.HEADING_PATH, row.heading_path)

        if row.anchor:
            meta = meta.set(SectionKeys.ANCHOR, row.anchor)

        return meta


class IngestRun:
    """Сборка и запуск конвейера индексации по области обхода."""

    @staticmethod
    def routes(cfg: IngestToolConfig) -> dict[str, Reader[str]]:
        """HTML читает bs4-ридер, документы — liteparse, txt/md/csv — decode.

        Каждый роут обёрнут логом: иначе долгий разбор (OCR) молчит до конца.
        """
        # liteparse тяжёлый: грузится только в процессе прогона
        from boba.text import TextMedia  # noqa: PLC0415
        from boba.tool.confluence.document_log import (  # noqa: PLC0415
            LoggingDocumentReader,
        )

        documents = LoggingDocumentReader(cfg)
        plain: dict[str, Reader[str]] = {}
        for content_type in ConfluenceIngest.HTML_CONTENT_TYPES:
            plain[content_type] = LocalConfluenceReader(cfg.table_shape)

        for media_type in documents.media_types:
            plain[media_type] = documents

        for media_type, reader in TextMedia.readers(cfg.text_encodings).items():
            plain[media_type] = reader

        routes: dict[str, Reader[str]] = {}
        for media_type, inner in plain.items():
            routes[media_type] = LoggingReader(inner, logger)

        return routes

    @classmethod
    async def run(
        cls,
        cfg: IngestToolConfig,
        scope: IngestScope,
        *,
        attachments: bool,
        ocr: bool,
    ) -> IngestReport:
        run_cfg = cfg.with_ocr(ocr=ocr)
        progress = IngestProgress(logger)
        report = await ConfluenceIngest.ingest(
            run_cfg,
            scope,
            attachments=attachments,
            progress=progress,
            routes=cls.routes(run_cfg),
        )
        progress.say()
        return report


@tool
async def confluence_index_page(
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                'page_id страницы Confluence для индексации, например "950276": '
                "строка из URL `viewpage.action?pageId=<id>`."
            ),
        ),
    ],
    attachments: Annotated[bool, Field(description=_ATTACHMENTS_DESCRIPTION)] = False,
    ocr: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[IngestToolConfig, Injected],
) -> TableResult:
    """Индексирует одну страницу Confluence по page_id.

    Отчёт идёт двумя строками, pages и attachments: found — сколько нашлось
    в Confluence, indexed — записано заново, unchanged — уже в индексе и не
    менялось, skipped — отсечено правилами, failed — сорвалось, причина в
    колонке error. found без indexed это норма: содержимое не менялось.
    """
    report = await IngestRun.run(
        cfg,
        IngestScope.page(page_id),
        attachments=attachments,
        ocr=ocr,
    )

    return TableResult(rows=report.rows(), note=f"{report.note()}; page_id: {page_id}")


@tool
async def confluence_index_cql(
    cql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "CQL-запрос Confluence, например `space = DQ AND type = page`."
            ),
        ),
    ],
    attachments: Annotated[bool, Field(description=_ATTACHMENTS_DESCRIPTION)] = False,
    ocr: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[IngestToolConfig, Injected],
) -> TableResult:
    """Индексирует страницы Confluence, найденные CQL-запросом.

    Запрос исполняет поиск Confluence, а он знает не весь сайт: контента
    архивных спейсов в нём нет, и только что созданные страницы появляются
    с задержкой. Такой спейс индексируется через confluence_index_space —
    тот идёт списком страниц спейса, а не поиском.

    Отчёт идёт двумя строками, pages и attachments: found — сколько нашлось
    в Confluence, indexed — записано заново, unchanged — уже в индексе и не
    менялось, skipped — отсечено правилами, failed — сорвалось, причина в
    колонке error. found без indexed это норма: содержимое не менялось.
    """
    report = await IngestRun.run(
        cfg,
        IngestScope.query(cql),
        attachments=attachments,
        ocr=ocr,
    )

    return TableResult(rows=report.rows(), note=report.note())


@tool
async def confluence_index_space(
    space_key: Annotated[
        str,
        Field(
            min_length=1,
            description='Ключ спейса целиком, например "DQ".',
        ),
    ],
    attachments: Annotated[bool, Field(description=_ATTACHMENTS_DESCRIPTION)] = False,
    ocr: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[IngestToolConfig, Injected],
) -> TableResult:
    """Индексирует спейс Confluence целиком.

    Отчёт идёт двумя строками, pages и attachments: found — сколько нашлось
    в Confluence, indexed — записано заново, unchanged — уже в индексе и не
    менялось, skipped — отсечено правилами, failed — сорвалось, причина в
    колонке error. found без indexed это норма: содержимое не менялось.
    """
    report = await IngestRun.run(
        cfg,
        IngestScope.space(space_key),
        attachments=attachments,
        ocr=ocr,
    )

    return TableResult(
        rows=report.rows(), note=f"{report.note()}; space_key: {space_key}"
    )


@tool
async def confluence_attachment(
    page_id: Annotated[
        str,
        Field(min_length=1, description="ID страницы Confluence."),
    ],
    filename: Annotated[
        str,
        Field(min_length=1, description="Имя вложения на странице."),
    ],
    ocr: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    *,
    cfg: Annotated[IngestToolConfig, Injected],
) -> MarkdownResult:
    """Читает вложение страницы Confluence и возвращает его текст."""
    run_cfg = cfg.with_ocr(ocr=ocr)

    rest_cfg = ConfluenceToolsConfig(
        confluence=run_cfg.confluence, body_format=run_cfg.body_format
    )
    data = await ConfluenceHttp.page_json(rest_cfg, page_id)

    link = _attachment_link(data, filename)
    if not link:
        titles = _attachment_titles(data)
        msg = (
            f"attachment {filename!r} not found on confluence page {page_id!r}; "
            f"page attachments: {titles}"
        )
        raise AttachmentNotFoundError(msg)

    content = await ConfluenceHttp.get(rest_cfg, ConfluenceUrl.link(link))

    from boba.liteparse.engine import LiteParseEngine  # noqa: PLC0415

    params = LiteParseParams.model_validate(
        run_cfg.model_dump(include=set(LiteParseParams.model_fields))
    )
    # парсер нативный и держит GIL: без потока он застопорил бы event loop
    result = await asyncio.to_thread(
        LiteParseEngine.parse_bytes, params, content, filename
    )

    return MarkdownResult(text=result.text)


def _attachment_link(data: dict[str, Any], filename: str) -> str:
    children = data.get("children")
    if not isinstance(children, dict):
        return ""

    attachments = children.get("attachment")
    if not isinstance(attachments, dict):
        return ""

    for item in attachments.get("results") or []:
        if str(item.get("title") or "") != filename:
            continue

        links = item.get("_links")
        if isinstance(links, dict):
            return str(links.get("download") or "")

    return ""


def _attachment_titles(data: dict[str, Any]) -> list[str]:
    children = data.get("children")
    if not isinstance(children, dict):
        return []

    attachments = children.get("attachment")
    if not isinstance(attachments, dict):
        return []

    titles: list[str] = []
    for item in attachments.get("results") or []:
        titles.append(str(item.get("title") or ""))

    return titles


EXPECTED: Mapping[type[Exception], IngestErrorKind] = {
    PostgresError: IngestErrorKind.DATABASE_UNAVAILABLE,
    LedgerError: IngestErrorKind.DATABASE_UNAVAILABLE,
    httpx.HTTPError: IngestErrorKind.REQUEST_FAILED,
    TransportError: IngestErrorKind.REQUEST_FAILED,
    ConfluencePayloadError: IngestErrorKind.REQUEST_FAILED,
    AttachmentNotFoundError: IngestErrorKind.ATTACHMENT_NOT_FOUND,
    LiteParseError: IngestErrorKind.DOCUMENT_UNREADABLE,
    EmbeddingError: IngestErrorKind.EMBEDDING_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    confluence_index_page,
    confluence_index_cql,
    confluence_index_space,
    confluence_attachment,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
