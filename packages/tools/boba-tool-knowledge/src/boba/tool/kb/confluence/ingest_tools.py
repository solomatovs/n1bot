"""Индексация Confluence в базу знаний и чтение вложений.

Пишет чанки в kb_chunks: страницы по списку id, по CQL-запросу или по
спейсам целиком — режим выбирает вызывающий. Логика прогона в узле стадии,
здесь обёртки langchain.
"""

from typing import Annotated, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field, ValidationError

from boba.tool.kb.confluence._fetch_attachment import (
    confluence_fetch_attachment,
)
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.tool.kb.confluence.ingest_protocol import IngestMode, IngestSource
from boba.toolkit.channels import ValidationSummary
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
        self._ingest_caller = ConfluenceIngestCaller("ingest", launchers)
        self._caller = ConfluenceCaller("ingest", launchers)

    def build(self) -> list[BaseTool]:
        return [
            self._ingest(),
            self._fetch_attachment(),
        ]

    @staticmethod
    def _failed(error: Exception) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="confluence_ingest_failed")

    @staticmethod
    def _invalid_source(error: ValidationError) -> ErrorResult:
        """Сводка вместо трейсбека: LLM правит вызов по тексту отказа."""
        return ErrorResult(
            message=ValidationSummary.of(error),
            error_kind="confluence_ingest_invalid_source",
        )

    def _ingest(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_ingest(  # noqa: PLR0913 — фасад LLM, параметры независимы
            mode: Annotated[
                IngestMode,
                Field(
                    description=(
                        "Which field lists the pages of this run: "
                        "'pages' takes page_ids, 'cql' takes cql, "
                        "'spaces' takes space_keys. Fields of the other "
                        "modes must stay empty."
                    ),
                ),
            ],
            page_ids: Annotated[  # noqa: B006 — схема фасада ждёт список по умолчанию
                LLMStringList,
                Field(
                    description=(
                        "Список page_id страниц Confluence для индексации, "
                        'например ["950276", "950278"]. Каждый id — строка '
                        "из URL `viewpage.action?pageId=<id>`. Только при "
                        "mode=pages."
                    ),
                ),
            ] = [],
            cql: Annotated[
                str,
                Field(
                    description=(
                        "CQL-запрос Confluence, например `space = DQ AND "
                        "type = page`. Только при mode=cql."
                    ),
                ),
            ] = "",
            space_keys: Annotated[  # noqa: B006 — схема фасада ждёт список по умолчанию
                LLMStringList,
                Field(
                    description=(
                        'Ключи спейсов целиком, например ["DQ", "IPKD"]. '
                        "Только при mode=spaces."
                    ),
                ),
            ] = [],
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
            """Индексирует страницы Confluence в базу знаний."""
            try:
                source = IngestSource(
                    mode=mode,
                    page_ids=list(page_ids),
                    cql=cql,
                    space_keys=list(space_keys),
                )
            except ValidationError as e:
                return pack_result(owner._invalid_source(e))

            try:
                stats = owner._ingest_caller.ingest(
                    source=source,
                    prune_missing=prune_missing,
                    force_update=force_update,
                    ocr_enabled=ocr_enabled,
                    num_workers=num_workers,
                    ocr_language=ocr_language,
                )
            except Exception as e:
                return pack_result(owner._failed(e))

            return pack_result(TableResult(rows=[stats], note=source.note()))

        return confluence_ingest

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
