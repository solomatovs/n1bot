"""Чтение Confluence: страница, grep по странице, CQL-поиск, список spaces.

Логика инструментов лежит в соседних приватных модулях как обычные функции;
здесь только обёртки langchain, общий конфиг секции и перевод ошибок сети
в ErrorResult.
"""

from typing import Annotated, Literal

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.kb.confluence._fetch import confluence_fetch_page
from boba.tool.kb.confluence._grep_page import confluence_grep_page
from boba.tool.kb.confluence._list_spaces import confluence_list_spaces
from boba.tool.kb.confluence._search_cql import confluence_search_cql
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.tools_config import ConfluenceToolsConfig
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import ErrorResult, TextResult, ToolResult, pack_result
from boba.toolkit.types import LLMStringList

__all__ = ["ConfluenceTools", "ConfluenceToolsConfig", "build_confluence_tools"]


class ConfluenceTools:
    """Собирает langchain-инструменты чтения Confluence."""

    def __init__(
        self,
        cfg: ConfluenceToolsConfig,
        launchers: LauncherFactory,
    ) -> None:
        """Настройки секции живут в узлах реестра стадий, фасадам они не нужны."""
        self._caller = ConfluenceCaller("confluence", launchers)

    def build(self) -> list[BaseTool]:
        return [
            self._fetch_page(),
            self._grep_page(),
            self._search_cql(),
            self._list_spaces(),
        ]

    @staticmethod
    def _failed(error: Exception) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="confluence_failed")

    def _fetch_page(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_fetch(
            page_id: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "ID страницы Confluence "
                        "(из URL `viewpage.action?pageId=<id>`). "
                        "Attachment'ы не скачиваются."
                    ),
                ),
            ],
            as_markdown: Annotated[
                bool,
                Field(
                    description=(
                        "Если true — конвертирует HTML в Markdown. "
                        "Иначе возвращает исходный Confluence-HTML."
                    ),
                ),
            ] = True,
        ) -> tuple[str, ToolResult]:
            """Скачивает одну Confluence-страницу и возвращает её контент."""
            try:
                text = confluence_fetch_page(
                    owner._caller, page_id=page_id, as_markdown=as_markdown
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(TextResult(text=text))

        return confluence_fetch

    def _grep_page(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_grep(  # noqa: PLR0913 — независимые флаги grep'а
            page_id: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "ID страницы Confluence (из URL `viewpage.action?pageId=<id>`)."
                    ),
                ),
            ],
            pattern: Annotated[
                str,
                Field(
                    min_length=1,
                    description="Python-regex; литерал при fixed_string=true.",
                ),
            ],
            as_markdown: Annotated[
                bool,
                Field(
                    description=(
                        "Искать по Markdown-конверсии вместо исходного "
                        "Confluence-HTML. По умолчанию true."
                    ),
                ),
            ] = True,
            case_insensitive: Annotated[
                bool,
                Field(description="Игнорировать регистр. По умолчанию false."),
            ] = False,
            context: Annotated[
                int,
                Field(
                    ge=0,
                    description="Строк контекста до и после каждого совпадения.",
                ),
            ] = 0,
            limit: Annotated[
                int,
                Field(ge=1, description="Максимум совпадений. По умолчанию 100."),
            ] = 100,
            fixed_string: Annotated[
                bool,
                Field(description="Литеральный поиск без regex. По умолчанию false."),
            ] = False,
        ) -> tuple[str, ToolResult]:
            """Ищет совпадения по тексту одной Confluence-страницы."""
            try:
                result = confluence_grep_page(
                    owner._caller,
                    page_id=page_id,
                    pattern=pattern,
                    as_markdown=as_markdown,
                    case_insensitive=case_insensitive,
                    context=context,
                    limit=limit,
                    fixed_string=fixed_string,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(result)

        return confluence_grep

    def _search_cql(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_search(
            query: Annotated[
                str,
                Field(min_length=1, description="Поисковый запрос (текст)."),
            ],
            spaces: Annotated[
                LLMStringList | None,
                Field(
                    description=(
                        'Ограничить поиск списком space key, например ["DQ", "IPKD"].'
                    ),
                ),
            ] = None,
            limit: Annotated[
                int,
                Field(ge=1, description="Максимум найденных страниц."),
            ] = 20,
            snippet_chars: Annotated[
                int,
                Field(ge=1, description="Длина сниппета в символах."),
            ] = 500,
        ) -> tuple[str, ToolResult]:
            """Ищет страницы в Confluence через CQL и возвращает таблицу hits."""
            try:
                result = confluence_search_cql(
                    owner._caller,
                    query=query,
                    spaces=spaces,
                    limit=limit,
                    snippet_chars=snippet_chars,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(result)

        return confluence_search

    def _list_spaces(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def confluence_spaces(
            pattern: Annotated[
                str | None,
                Field(
                    description=(
                        "Glob-шаблон (регистронезависимо) для key/name спейса. "
                        "Совпадение по полю целиком: `*data*` — содержит data."
                    ),
                ),
            ] = None,
            space_type: Annotated[
                Literal["global", "personal", "any"],
                Field(description="Тип space: global / personal / any."),
            ] = "global",
            limit: Annotated[
                int,
                Field(ge=1, le=1000, description="Максимум спейсов в ответе."),
            ] = 200,
        ) -> tuple[str, ToolResult]:
            """Список spaces Confluence с опциональным glob-фильтром."""
            try:
                result = confluence_list_spaces(
                    owner._caller,
                    pattern=pattern,
                    space_type=space_type,
                    limit=limit,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(result)

        return confluence_spaces


def build_confluence_tools(
    cfg: ConfluenceToolsConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    """Собрать инструменты чтения Confluence."""
    return ConfluenceTools(cfg, launchers).build()
