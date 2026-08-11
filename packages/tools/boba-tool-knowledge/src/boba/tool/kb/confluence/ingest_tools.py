"""Индексация Confluence в базу знаний и чтение вложений.

Пишет чанки в kb_chunks: страницы по списку id, по CQL-запросу или по
спейсам целиком. Логика в приватных модулях, здесь обёртки langchain.
"""

from typing import Annotated, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.kb.confluence._fetch_attachment import (
    confluence_fetch_attachment,
)
from boba.tool.kb.confluence._ingest_cql import confluence_ingest_cql
from boba.tool.kb.confluence._ingest_pages import (
    confluence_ingest_pages,
)
from boba.tool.kb.confluence._ingest_spaces import (
    confluence_ingest_spaces,
)
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import (
    ErrorResult,
    TableResult,
    TextResult,
    ToolResult,
    pack_result,
)
from boba.toolkit.types import LLMStringList

__all__ = ["ConfluenceIngestTools", "build_confluence_ingest_tools"]


class ConfluenceIngestTools:
    """Собирает langchain-инструменты индексации Confluence."""

    OCR_DESCRIPTION: ClassVar[str] = (
        "OCR вложений: true распознаёт текст по картинкам (сканы, картинковые "
        "PDF), false — только текстовый слой. OCR дорог: минуты и гигабайты "
        "памяти на документ."
    )
    WORKERS_DESCRIPTION: ClassVar[str] = (
        "Параллелизм OCR, 1..4; ~50-100 MiB памяти на воркер. "
        "При ocr_enabled=false не влияет."
    )
    LANGUAGE_DESCRIPTION: ClassVar[str] = (
        "Язык OCR в формате Tesseract: 'rus+eng' для русских документов, "
        "'eng' для английских."
    )

    def __init__(
        self,
        cfg: ConfluenceIngestConfig,
        launchers: LauncherFactory,
    ) -> None:
        """Настройки секции живут в узлах реестра стадий, фасадам они не нужны."""
        self._ingest = ConfluenceIngestCaller("ingest", launchers)
        self._caller = ConfluenceCaller("ingest", launchers)

    def build(self) -> list[BaseTool]:
        return [
            self._ingest_pages(),
            self._ingest_cql(),
            self._ingest_spaces(),
            self._fetch_attachment(),
        ]

    @staticmethod
    def _failed(error: Exception) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="confluence_ingest_failed")

    def _ingest_pages(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_index_pages(  # noqa: PLR0913 — фасад LLM, параметры независимы
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
                        "Переиндексировать страницы целиком, минуя "
                        "пропуск неизменившихся."
                    ),
                ),
            ] = False,
            ocr_enabled: Annotated[
                bool, Field(description=owner.OCR_DESCRIPTION)
            ] = False,
            num_workers: Annotated[
                int, Field(ge=1, le=4, description=owner.WORKERS_DESCRIPTION)
            ] = 1,
            ocr_language: Annotated[
                str, Field(min_length=1, description=owner.LANGUAGE_DESCRIPTION)
            ] = "rus+eng",
        ) -> tuple[str, ToolResult]:
            """Индексирует явный список страниц Confluence по page_id."""
            try:
                result = confluence_ingest_pages(
                    owner._ingest,
                    page_ids=page_ids,
                    prune_missing=prune_missing,
                    force_update=force_update,
                    ocr_enabled=ocr_enabled,
                    num_workers=num_workers,
                    ocr_language=ocr_language,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(result)

        return confluence_index_pages

    def _ingest_cql(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_index_cql(
            cql: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "CQL-запрос Confluence, например "
                        '`space = DQ AND type = page`.'
                    ),
                ),
            ],
            prune_missing: Annotated[
                bool,
                Field(description="Удалить чанки, не попавшие в выборку."),
            ] = False,
            ocr_enabled: Annotated[
                bool, Field(description=owner.OCR_DESCRIPTION)
            ] = False,
            num_workers: Annotated[
                int, Field(ge=1, le=4, description=owner.WORKERS_DESCRIPTION)
            ] = 1,
            ocr_language: Annotated[
                str, Field(min_length=1, description=owner.LANGUAGE_DESCRIPTION)
            ] = "rus+eng",
        ) -> tuple[str, ToolResult]:
            """Индексирует страницы Confluence, найденные CQL-запросом."""
            try:
                summary = confluence_ingest_cql(
                    owner._ingest,
                    cql=cql,
                    prune_missing=prune_missing,
                    ocr_enabled=ocr_enabled,
                    num_workers=num_workers,
                    ocr_language=ocr_language,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(TableResult(rows=[summary]))

        return confluence_index_cql

    def _ingest_spaces(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_index_spaces(  # noqa: PLR0913 — фасад LLM, параметры независимы
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
            ocr_enabled: Annotated[
                bool, Field(description=owner.OCR_DESCRIPTION)
            ] = False,
            num_workers: Annotated[
                int, Field(ge=1, le=4, description=owner.WORKERS_DESCRIPTION)
            ] = 1,
            ocr_language: Annotated[
                str, Field(min_length=1, description=owner.LANGUAGE_DESCRIPTION)
            ] = "rus+eng",
        ) -> tuple[str, ToolResult]:
            """Индексирует спейсы Confluence целиком."""
            try:
                result = confluence_ingest_spaces(
                    owner._ingest,
                    space_keys=space_keys,
                    prune_missing=prune_missing,
                    force_update=force_update,
                    ocr_enabled=ocr_enabled,
                    num_workers=num_workers,
                    ocr_language=ocr_language,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(result)

        return confluence_index_spaces

    def _fetch_attachment(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_attachment(
            page_id: Annotated[
                str,
                Field(min_length=1, description="ID страницы Confluence."),
            ],
            filename: Annotated[
                str,
                Field(min_length=1, description="Имя вложения на странице."),
            ],
            ocr_enabled: Annotated[
                bool, Field(description=owner.OCR_DESCRIPTION)
            ] = False,
            num_workers: Annotated[
                int, Field(ge=1, le=4, description=owner.WORKERS_DESCRIPTION)
            ] = 1,
            ocr_language: Annotated[
                str, Field(min_length=1, description=owner.LANGUAGE_DESCRIPTION)
            ] = "rus+eng",
        ) -> tuple[str, ToolResult]:
            """Читает вложение страницы Confluence и возвращает его текст."""
            try:
                text = confluence_fetch_attachment(
                    owner._caller,
                    page_id=page_id,
                    filename=filename,
                    ocr_enabled=ocr_enabled,
                    num_workers=num_workers,
                    ocr_language=ocr_language,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(TextResult(text=text))

        return confluence_attachment


def build_confluence_ingest_tools(
    cfg: ConfluenceIngestConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    """Собрать инструменты индексации Confluence."""
    return ConfluenceIngestTools(cfg, launchers).build()
