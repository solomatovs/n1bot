"""Tool confluence_fetch_attachment + ConfluenceFetchAttachmentConfig.

Скачивает одно вложение Confluence (PDF/docx/xlsx/pptx) по page_id +
имени файла и возвращает его текст, распарсенный liteparse, прямо
вызывающему LLM — без записи на диск и без индексации. Замыкает цикл
поиска: kb_search отдаёт page_id + page_title (имя файла) индексированного
вложения, LLM перечитывает оригинал этим tool'ом и цитирует.

Переиспользует ConfluenceContentTransport.iter_documents (тот же page ->
attachments fan-out, что и ingest): AttachmentFilter сужает выгрузку до
одного вложения по title, парсинг — общий LiteParseEngine.

Config-секция: [tool.kb.confluence.fetch_attachment].
"""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from pydantic import ConfigDict, Field

from boba.chainlit2.agent.tools.confluence.connection import ConfluenceConnection
from boba.chainlit2.agent.tools.confluence.models import (
    AttachmentFilter,
    ConfluenceKeys,
)
from boba.chainlit2.agent.tools.confluence.pipeline import ConfluenceContentTransport
from boba.chainlit2.agent.tools.confluence.request_sources import (
    ConfluencePagesRequestSource,
)
from boba.liteparse import LiteParseEngine, LiteParseError, LiteParseParams
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceFetchAttachmentConfig", "confluence_fetch_attachment"]


class ConfluenceFetchAttachmentConfig(LiteParseParams):
    """Self-contained конфиг tool'а confluence_fetch_attachment.

    Наследует LiteParseParams (ocr_enabled/ocr_language/max_pages — те же
    настройки парсера, что у ingest вложений).
    """

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )


def confluence_fetch_attachment(
    cfg: ConfluenceFetchAttachmentConfig,
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "ID страницы Confluence, на которой лежит вложение "
                "(колонка page_id в результатах kb_search)."
            ),
        ),
    ],
    filename: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя файла вложения с расширением (колонка page_title в "
                "результатах kb_search), напр. 'report.pdf'."
            ),
        ),
    ],
) -> str:
    """Скачивает вложение Confluence по page_id+filename и возвращает его текст.

    Парсит PDF/docx/xlsx/pptx через liteparse и отдаёт весь извлечённый текст
    для дословного цитирования. Для поиска нужного вложения используй
    kb_search (он вернёт page_id и имя файла индексированного документа).
    """
    conn = ConfluenceConnection(profile=cfg.confluence, body_format=cfg.body_format)
    request_source = ConfluencePagesRequestSource(
        base_url=conn.base_url,
        page_ids=[page_id],
        body_format=conn.body_format,
    )
    att_filter = AttachmentFilter.from_lists(titles=[filename])

    try:
        for raw in ConfluenceContentTransport.iter_documents(
            request_source=request_source,
            conn=conn,
            attachment_filter=att_filter,
        ):
            attachment = raw.metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
            if attachment is None:
                continue  # сама страница (HTML) — пропускаем, нужен только attachment
            data = raw.handle.read()
            return LiteParseEngine.parse(cfg, data, attachment.title).text
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence fetch failed: {type(e).__name__}: {e}",
        ) from e
    except LiteParseError as e:
        raise RuntimeError(f"Не удалось распарсить вложение {filename!r}: {e}") from e

    raise RuntimeError(
        f"Вложение {filename!r} не найдено на странице page_id={page_id!r}",
    )
