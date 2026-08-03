"""Чтение Confluence: страница, grep по странице, CQL-поиск, список spaces.

Логика инструментов лежит в соседних приватных модулях как обычные функции;
здесь только обёртки langchain, общий конфиг секции и перевод ошибок сети
в ErrorResult.
"""

from collections.abc import Callable, Mapping
from typing import Annotated, Literal

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from boba.settings import LLMStringList
from boba.tool.kb.confluence._fetch import (
    ConfluenceFetchPageConfig,
    confluence_fetch_page,
)
from boba.tool.kb.confluence._grep_page import (
    ConfluenceGrepPageConfig,
    confluence_grep_page,
)
from boba.tool.kb.confluence._list_spaces import (
    ConfluenceListSpacesConfig,
    confluence_list_spaces,
)
from boba.tool.kb.confluence._search_cql import (
    ConfluenceSearchCqlConfig,
    confluence_search_cql,
)
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.toolkit.pack import pack_result
from boba.toolkit.result import ErrorResult, TextResult, ToolResult
from boba.toolkit.sandbox import SandboxToolConfig
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceTools", "ConfluenceToolsConfig", "build_confluence_tools"]


class ConfluenceToolsConfig(BaseModel):
    """Общий конфиг секции [tool.confluence] для всех инструментов чтения."""

    model_config = ConfigDict(extra="ignore")

    sandbox: SandboxToolConfig = Field(
        description="Окружение и точка входа payload'а: [tool.confluence.sandbox].",
    )

    confluence: HttpProfile = Field(
        description='Web-профиль Confluence ссылкой `confluence = "${web.<name>}"`.',
    )
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match в grep.",
    )


class ConfluenceTools:
    """Собирает langchain-инструменты чтения Confluence."""

    def __init__(
        self,
        cfg: ConfluenceToolsConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._cfg = cfg
        self._caller = ConfluenceCaller("confluence", cfg.sandbox, path_vars)

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
        cfg = ConfluenceFetchPageConfig(
            confluence=self._cfg.confluence,
            body_format=self._cfg.body_format,
        )

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
                    cfg, owner._caller, page_id=page_id, as_markdown=as_markdown
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(TextResult(text=text))

        return confluence_fetch

    def _grep_page(self) -> BaseTool:
        owner = self
        cfg = ConfluenceGrepPageConfig(
            confluence=self._cfg.confluence,
            body_format=self._cfg.body_format,
            max_text_chars=self._cfg.max_text_chars,
        )

        @tool(response_format="content_and_artifact")
        def confluence_grep(  # noqa: PLR0913 — независимые флаги grep'а
            page_id: Annotated[
                str,
                Field(
                    min_length=1,
                    description=(
                        "ID страницы Confluence "
                        "(из URL `viewpage.action?pageId=<id>`)."
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
                    cfg,
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
        cfg = ConfluenceSearchCqlConfig(confluence=self._cfg.confluence)

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
                        "Ограничить поиск списком space key, "
                        'например ["DQ", "IPKD"].'
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
                    cfg,
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
        cfg = ConfluenceListSpacesConfig(confluence=self._cfg.confluence)

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
                    cfg,
                    owner._caller, pattern=pattern, space_type=space_type, limit=limit
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(result)

        return confluence_spaces


def build_confluence_tools(
    cfg: ConfluenceToolsConfig,
    path_vars: Callable[[], Mapping[str, str]],
) -> list[BaseTool]:
    """Собрать инструменты чтения Confluence."""
    return ConfluenceTools(cfg, path_vars).build()
