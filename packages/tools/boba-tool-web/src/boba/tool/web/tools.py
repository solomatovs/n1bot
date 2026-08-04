"""Чтение веб-страниц: скачивание окна строк и поиск по содержимому."""

from typing import Annotated

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.tool.web._fetch import web_fetch
from boba.tool.web._grep import WebGrepConfig, web_grep
from boba.tool.web.caller import WebCaller
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.result import ErrorResult, JsonResult, ToolResult, pack_result

__all__ = ["WebTools", "build_web_tools"]


class WebTools:
    """Собирает langchain-инструменты работы с вебом."""

    def __init__(
        self,
        cfg: WebGrepConfig,
        launchers: LauncherFactory,
    ) -> None:
        self._cfg = cfg
        self._web = WebCaller("web", launchers)

    def build(self) -> list[BaseTool]:
        return [self._fetch(), self._grep()]

    @staticmethod
    def _failed(error: Exception) -> ErrorResult:
        return ErrorResult(message=str(error), error_kind="web_failed")

    def _fetch(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def web_fetch_page(
            url: Annotated[str, Field(min_length=1, description="URL для скачивания")],
            as_markdown: Annotated[
                bool,
                Field(description="true — конвертирует HTML->Markdown"),
            ],
            line_offset: Annotated[
                int,
                Field(ge=0, description="Вернуть контент начиная со строки"),
            ],
            line_count: Annotated[
                int,
                Field(ge=1, description="Сколько строк вернуть начиная с line_offset"),
            ],
        ) -> tuple[str, ToolResult]:
            """Скачивает URL и возвращает окно строк; total_lines — для пагинации."""
            try:
                payload = web_fetch(
                    owner._cfg,
                    owner._web,
                    url=url,
                    as_markdown=as_markdown,
                    line_offset=line_offset,
                    line_count=line_count,
                )
            except Exception as e:
                return pack_result(owner._failed(e))
            return pack_result(JsonResult(payload=payload))

        return web_fetch_page

    def _grep(self) -> BaseTool:
        owner = self

        @tool(response_format="content_and_artifact")
        def web_grep_page(  # noqa: PLR0913
            url: Annotated[str, Field(min_length=1, description="URL для скачивания.")],
            pattern: Annotated[
                str,
                Field(
                    min_length=1,
                    description="Python-regex; литерал при fixed_string=true.",
                ),
            ],
            as_markdown: Annotated[
                bool,
                Field(description="Искать по Markdown-конверсии вместо HTML."),
            ] = True,
            case_insensitive: Annotated[
                bool,
                Field(description="Игнорировать регистр. По умолчанию false."),
            ] = False,
            context: Annotated[
                int,
                Field(ge=0, description="Строк контекста до и после совпадения."),
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
            """Скачивает страницу и ищет в её содержимом совпадения pattern."""
            try:
                result = web_grep(
                    owner._cfg,
                    owner._web,
                    url=url,
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

        return web_grep_page


def build_web_tools(
    cfg: WebGrepConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return WebTools(cfg, launchers).build()
