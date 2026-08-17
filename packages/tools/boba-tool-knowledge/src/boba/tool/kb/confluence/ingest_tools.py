"""Индексация Confluence и чтение вложений: функции уровня модуля.

Конвейер (обход, разбор, эмбеддинг, запись в kb_chunks) исполняется в теле —
потому оно живёт в песочнице; модель эмбеддера грузится один раз на прогон.

Ошибки:
PostgresError — до хранилища не достучаться.
httpx.HTTPError — Confluence недоступен или ответил статусом.
AttachmentNotFoundError — вложения с таким именем на странице нет.
LiteParseError — вложение скачалось, но не разбирается.
Сбой разбора отдельного документа ingest переживает сам, наружу не выходит.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

import httpx
from langchain_core.tools import InjectedToolArg, tool
from pydantic import Field

from boba.db.postgres import PostgresError
from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    RequestSource,
    Section,
    SectionKeys,
)
from boba.text.document import LiteParseError, LiteParseParams
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.ingest_base import (
    ConfluenceIngest,
    ConfluenceIngestConfig,
)
from boba.tool.kb.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluenceMultiSpaceRequestSource,
    ConfluencePagesRequestSource,
    ConfluenceRequest,
)
from boba.tool.kb.confluence.tools import ConfluenceHttp, ConfluenceToolsConfig
from boba.tool.kb.indexing_log import Elapsed, IngestProgress, LoggingReader
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import TableResult, TextResult, ToolResult, pack_result
from boba.toolkit.types import LLMStringList, SecretRevealing

logger = logging.getLogger("boba.tool.kb.confluence.ingest")

_ATTACHMENTS_DESCRIPTION = (
    "Какие вложения страниц читать. Список масок через запятую. Маска без "
    "косой черты — имя файла: `*.pdf`, `*.docx`, `отчёт*.xlsx`. Маска с косой "
    "чертой — тип содержимого: `application/pdf`, `image/*`. "
    "Пусто (по умолчанию) — вложения не читаются, индексируется только текст "
    "страниц; так быстрее всего и меньше всего памяти. `*` — все вложения "
    "страницы. Картинки (`*.png`, `image/*`) читаются только при "
    "ocr_enabled=true, иначе пропускаются: без распознавания текста в них нет."
)
_OCR_DESCRIPTION = (
    "OCR вложений: true распознаёт текст по картинкам (сканы, картинковые "
    "PDF), false — только текстовый слой. OCR дорог: минуты и гигабайты "
    "памяти на документ."
)
_WORKERS_DESCRIPTION = (
    "Параллелизм OCR, 1..4; ~50-100 MiB памяти на воркер. "
    "При ocr_enabled=false не влияет."
)
_LANGUAGE_DESCRIPTION = (
    "Язык OCR в формате Tesseract: 'rus+eng' для русских документов, "
    "'eng' для английских."
)


class AttachmentNotFoundError(Exception):
    """Вложения с таким именем на странице нет; текст готов для пользователя."""


class IngestErrorKind(StrEnum):
    """Ожидаемые отказы ingest-инструментов."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    REQUEST_FAILED = "ingest_request_failed"
    ATTACHMENT_NOT_FOUND = "attachment_not_found"
    DOCUMENT_UNREADABLE = "document_unreadable"


class IngestToolConfig(SecretRevealing, ConfluenceIngestConfig):
    """Конфиг ingest-инструментов; секция [tool.ingest]."""

    SECTION: ClassVar[str] = "tool.ingest"

class LocalConfluenceReader(Reader[str]):
    """HTML-страница -> секции по заголовкам; bs4 работает прямо здесь."""

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence")
    DOC_TYPE: ClassVar[str] = "confluence_html"

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
        from boba.tool.kb.html.payload import PageOps  # noqa: PLC0415

        logger.info("html parse start: %s, %d bytes", title or "?", len(payload))
        elapsed = Elapsed()
        answer = await asyncio.to_thread(
            PageOps.confluence_sections,
            {"html": html, "title": title},
        )
        logger.info(
            "html parse done: %s -> %d sections in %dms",
            title or "?",
            len(answer["sections"]),
            elapsed.ms(),
        )
        for row in answer["sections"]:
            yield Section(
                source_id=value.source_id,
                content=row["content"],
                order=row["order"],
                metadata=self._meta(value, row),
            )

    @classmethod
    def _meta(cls, value: RawDocument, row: dict[str, Any]):
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, cls.DOC_TYPE)
        if row["heading_path"]:
            meta = meta.set(SectionKeys.HEADING_PATH, row["heading_path"])
        if row["anchor"]:
            meta = meta.set(SectionKeys.ANCHOR, row["anchor"])
        return meta


class IngestRun:
    """Сборка и запуск конвейера индексации по способу обхода."""

    @staticmethod
    def routes(cfg: IngestToolConfig) -> dict[str, Reader[str]]:
        """HTML читает bs4-ридер, документы — liteparse, txt/md/csv — decode.

        Каждый роут обёрнут логом: иначе долгий разбор (OCR) молчит до конца.
        """
        # liteparse тяжёлый: грузится только в процессе прогона
        from boba.text import TextMedia  # noqa: PLC0415
        from boba.tool.kb.confluence.document_log import (  # noqa: PLC0415
            LoggingDocumentReader,
        )

        documents = LoggingDocumentReader(cfg)
        plain: dict[str, Reader[str]] = {}
        for content_type in ConfluenceIngest.HTML_CONTENT_TYPES:
            plain[content_type] = LocalConfluenceReader()
        for media_type in documents.media_types:
            plain[media_type] = documents
        for media_type, reader in TextMedia.readers(cfg.text_encodings).items():
            plain[media_type] = reader

        routes: dict[str, Reader[str]] = {}
        for media_type, inner in plain.items():
            routes[media_type] = LoggingReader(inner, logger)
        return routes

    @classmethod
    async def run(  # noqa: PLR0913 — параметры прогона независимы
        cls,
        cfg: IngestToolConfig,
        source: RequestSource[ConfluenceRequest],
        progress: IngestProgress,
        *,
        attachments: str,
        prune_missing: bool,
        force_update: bool,
    ) -> dict[str, Any]:
        stats = await ConfluenceIngest.ingest(
            cfg,
            source,
            prune_missing,
            force_update,
            attachments=attachments,
            progress=progress,
            routes=cls.routes(cfg),
        )
        progress.say()
        return stats

    @staticmethod
    def connection(cfg: IngestToolConfig) -> ConfluenceConnection:
        return ConfluenceConnection(
            profile=cfg.confluence, body_format=cfg.body_format
        )


@tool(response_format="content_and_artifact")
async def confluence_index_pages(  # noqa: PLR0913 — фасад LLM, параметры независимы
    page_ids: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Список page_id страниц Confluence для индексации, "
                'например ["950276", "950278"]. Каждый id — строка '
                "из URL `viewpage.action?pageId=<id>`."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Удалить из коллекции чанки, которых нет среди "
                "страниц текущего запуска."
            ),
        ),
    ] = False,
    force_update: Annotated[
        bool,
        Field(
            description=(
                "Переиндексировать страницы целиком, минуя пропуск "
                "неизменившихся."
            ),
        ),
    ] = False,
    attachments: Annotated[str, Field(description=_ATTACHMENTS_DESCRIPTION)] = "",
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    num_workers: Annotated[
        int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
    ] = 1,
    ocr_language: Annotated[
        str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
    ] = "rus+eng",
    *,
    cfg: Annotated[IngestToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Индексирует явный список страниц Confluence по page_id."""
    run_cfg = cfg.with_parser(
        ocr_enabled=ocr_enabled, num_workers=num_workers, ocr_language=ocr_language
    )

    conn = IngestRun.connection(run_cfg)
    progress = IngestProgress(logger)
    source = ConfluencePagesRequestSource(
        base_url=conn.base_url,
        page_ids=list(page_ids),
        body_format=conn.body_format,
        progress=progress,
    )

    stats = await IngestRun.run(
        run_cfg, source, progress,
        attachments=attachments,
        prune_missing=prune_missing, force_update=force_update,
    )

    note = f"page_ids ({len(page_ids)}): {', '.join(page_ids)}"
    table = TableResult(rows=[stats], note=note)
    return pack_result(table)


@tool(response_format="content_and_artifact")
async def confluence_index_cql(  # noqa: PLR0913 — фасад LLM, параметры независимы
    cql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "CQL-запрос Confluence, например `space = DQ AND type = page`."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(description="Удалить чанки, не попавшие в выборку."),
    ] = False,
    attachments: Annotated[str, Field(description=_ATTACHMENTS_DESCRIPTION)] = "",
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    num_workers: Annotated[
        int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
    ] = 1,
    ocr_language: Annotated[
        str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
    ] = "rus+eng",
    *,
    cfg: Annotated[IngestToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Индексирует страницы Confluence, найденные CQL-запросом."""
    run_cfg = cfg.with_parser(
        ocr_enabled=ocr_enabled, num_workers=num_workers, ocr_language=ocr_language
    )

    conn = IngestRun.connection(run_cfg)
    progress = IngestProgress(logger)
    source = ConfluenceCqlRequestSource(
        conn=conn,
        cql=cql,
        body_format=conn.body_format,
        progress=progress,
    )

    stats = await IngestRun.run(
        run_cfg, source, progress,
        attachments=attachments,
        prune_missing=prune_missing, force_update=False,
    )

    table = TableResult(rows=[stats])
    return pack_result(table)


@tool(response_format="content_and_artifact")
async def confluence_index_spaces(  # noqa: PLR0913 — фасад LLM, параметры независимы
    space_keys: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description='Ключи спейсов целиком, например ["DQ", "IPKD"].',
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(description="Удалить чанки, не попавшие в выборку."),
    ] = False,
    force_update: Annotated[
        bool,
        Field(description="Переиндексировать страницы целиком."),
    ] = False,
    attachments: Annotated[str, Field(description=_ATTACHMENTS_DESCRIPTION)] = "",
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    num_workers: Annotated[
        int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
    ] = 1,
    ocr_language: Annotated[
        str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
    ] = "rus+eng",
    *,
    cfg: Annotated[IngestToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Индексирует спейсы Confluence целиком."""
    run_cfg = cfg.with_parser(
        ocr_enabled=ocr_enabled, num_workers=num_workers, ocr_language=ocr_language
    )

    conn = IngestRun.connection(run_cfg)
    progress = IngestProgress(logger)
    source = ConfluenceMultiSpaceRequestSource(
        conn=conn,
        space_keys=list(space_keys),
        body_format=conn.body_format,
        progress=progress,
    )

    stats = await IngestRun.run(
        run_cfg, source, progress,
        attachments=attachments,
        prune_missing=prune_missing, force_update=force_update,
    )

    note = f"space_keys ({len(space_keys)}): {', '.join(space_keys)}"
    table = TableResult(rows=[stats], note=note)
    return pack_result(table)


@tool(response_format="content_and_artifact")
async def confluence_attachment(  # noqa: PLR0913 — фасад LLM, параметры независимы
    page_id: Annotated[
        str,
        Field(min_length=1, description="ID страницы Confluence."),
    ],
    filename: Annotated[
        str,
        Field(min_length=1, description="Имя вложения на странице."),
    ],
    ocr_enabled: Annotated[bool, Field(description=_OCR_DESCRIPTION)] = False,
    num_workers: Annotated[
        int, Field(ge=1, le=4, description=_WORKERS_DESCRIPTION)
    ] = 1,
    ocr_language: Annotated[
        str, Field(min_length=1, description=_LANGUAGE_DESCRIPTION)
    ] = "rus+eng",
    *,
    cfg: Annotated[IngestToolConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Читает вложение страницы Confluence и возвращает его текст."""
    run_cfg = cfg.with_parser(
        ocr_enabled=ocr_enabled, num_workers=num_workers, ocr_language=ocr_language
    )

    rest_cfg = ConfluenceToolsConfig(
        confluence=run_cfg.confluence, body_format=run_cfg.body_format
    )
    data = await ConfluenceHttp.page_json(rest_cfg, page_id)

    link = _attachment_link(data, filename)
    if not link:
        msg = f"attachment {filename!r} not found on page {page_id!r}"
        raise AttachmentNotFoundError(msg)

    content = await ConfluenceHttp.get(rest_cfg, link)

    from boba.liteparse.engine import LiteParseEngine  # noqa: PLC0415

    params = LiteParseParams.model_validate(
        run_cfg.model_dump(include=set(LiteParseParams.model_fields))
    )
    # парсер нативный и держит GIL: без потока он застопорил бы event loop
    result = await asyncio.to_thread(
        LiteParseEngine.parse_bytes, params, content, filename
    )

    artifact = TextResult(text=result.text)
    return pack_result(artifact)


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


EXPECTED: Mapping[type[Exception], IngestErrorKind] = {
    PostgresError: IngestErrorKind.DATABASE_UNAVAILABLE,
    httpx.HTTPError: IngestErrorKind.REQUEST_FAILED,
    AttachmentNotFoundError: IngestErrorKind.ATTACHMENT_NOT_FOUND,
    LiteParseError: IngestErrorKind.DOCUMENT_UNREADABLE,
}

TOOLS: Final = ToolMain.toolset(
    confluence_index_pages,
    confluence_index_cql,
    confluence_index_spaces,
    confluence_attachment,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
