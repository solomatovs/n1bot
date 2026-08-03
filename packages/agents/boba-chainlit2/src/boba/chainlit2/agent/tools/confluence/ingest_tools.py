"""Индексация Confluence в базу знаний и чтение вложений.

Пишет чанки в kb_chunks: страницы по списку id, по CQL-запросу или по
спейсам целиком. Логика в приватных модулях, здесь обёртки langchain.
"""

from collections.abc import Callable, Mapping
from typing import Annotated

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit2.agent.tools.confluence._fetch_attachment import (
    ConfluenceFetchAttachmentConfig,
    confluence_fetch_attachment,
)
from boba.chainlit2.agent.tools.confluence._ingest_cql import confluence_ingest_cql
from boba.chainlit2.agent.tools.confluence._ingest_pages import (
    confluence_ingest_pages,
)
from boba.chainlit2.agent.tools.confluence._ingest_spaces import (
    confluence_ingest_spaces,
)
from boba.chainlit2.agent.tools.confluence.caller import ConfluenceCaller
from boba.chainlit2.agent.tools.confluence.ingest_base import ConfluenceIngestConfig
from boba.chainlit2.agent.tools.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import (
    ErrorResult,
    TableResult,
    TextResult,
    ToolResult,
)
from boba.settings import LLMStringList

__all__ = ["ConfluenceIngestTools", "build_confluence_ingest_tools"]


class ConfluenceIngestTools:
    """Собирает langchain-инструменты индексации Confluence."""

    def __init__(
        self,
        cfg: ConfluenceIngestConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._cfg = cfg
        self._ingest = ConfluenceIngestCaller("ingest", cfg.sandbox, path_vars)
        self._caller = ConfluenceCaller("confluence", cfg.sandbox, path_vars)

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
        def confluence_index_pages(
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
        ) -> tuple[str, ToolResult]:
            """Индексирует явный список страниц Confluence по page_id."""
            try:
                result = confluence_ingest_pages(
                    owner._cfg,
                    owner._ingest,
                    page_ids=page_ids,
                    prune_missing=prune_missing,
                    force_update=force_update,
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
        ) -> tuple[str, ToolResult]:
            """Индексирует страницы Confluence, найденные CQL-запросом."""
            try:
                summary = confluence_ingest_cql(
                    owner._cfg,
                    owner._ingest, cql=cql, prune_missing=prune_missing
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(TableResult(rows=[summary]))

        return confluence_index_cql

    def _ingest_spaces(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_index_spaces(
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
        ) -> tuple[str, ToolResult]:
            """Индексирует спейсы Confluence целиком."""
            try:
                result = confluence_ingest_spaces(
                    owner._cfg,
                    owner._ingest,
                    space_keys=space_keys,
                    prune_missing=prune_missing,
                    force_update=force_update,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(result)

        return confluence_index_spaces

    def _fetch_attachment(self) -> BaseTool:
        owner = self
        cfg = ConfluenceFetchAttachmentConfig.model_validate(
            owner._cfg.model_dump(
                include=set(ConfluenceFetchAttachmentConfig.model_fields)
            )
        )

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
        ) -> tuple[str, ToolResult]:
            """Читает вложение страницы Confluence и возвращает его текст."""
            try:
                text = confluence_fetch_attachment(
                    cfg, owner._caller, page_id=page_id, filename=filename
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(TextResult(text=text))

        return confluence_attachment


def build_confluence_ingest_tools(
    cfg: ConfluenceIngestConfig,
    path_vars: Callable[[], Mapping[str, str]],
) -> list[BaseTool]:
    """Собрать инструменты индексации Confluence."""
    return ConfluenceIngestTools(cfg, path_vars).build()
