"""Tool confluence_fetch_attachment: скачивание и парсинг вложения идут в песочнице."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from boba.tool.doc.liteparse import SandboxParserConfig
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.protocol import (
    ConfluenceAttachmentRequest,
)
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceFetchAttachmentConfig", "confluence_fetch_attachment"]


class ConfluenceFetchAttachmentConfig(SandboxParserConfig):
    """Конфиг tool'а confluence_fetch_attachment: соединение плюс парсер."""

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )


def confluence_fetch_attachment(
    cfg: ConfluenceFetchAttachmentConfig,
    caller: ConfluenceCaller,
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
    """Скачивает вложение Confluence по page_id+filename и возвращает его текст."""
    request = ConfluenceAttachmentRequest(
        op=ConfluenceAttachmentRequest.OP,
        base_url=cfg.confluence.base_url or "",
        profile=ConfluenceCaller.transport_of(cfg.confluence),
        page_id=page_id,
        body_format=cfg.body_format,
        filename=filename,
        params=cfg.parse_params(),
    )
    return caller.attachment(request)
