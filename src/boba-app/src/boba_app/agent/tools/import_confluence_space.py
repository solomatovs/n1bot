"""Tool: загрузка целого пространства из Confluence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from boba_domain.agent.events import DocPipelineEvent
from boba_domain.core.tools import Tool, ToolOutput, ToolResult
from boba_domain.importing.confluence import (
    ConfluenceImportFactory,
    ImportDone,
    ImportPageFailed,
    ImportPageSaved,
    ImportSpaceEnumerated,
)
from boba_domain.importing.loading import ConfluenceImportParams, SpaceLoadParams
from boba_domain.workspace import Workspace

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class ImportSpaceParams:
    """Параметры загрузки пространства из Confluence."""

    space_key: str = field(
        metadata={"description": "Ключ пространства Confluence (например 'MYSPACE')"}
    )
    api_page_limit: int = field(
        default=50,
        metadata={"description": "Количество страниц за один запрос (1-200)"},
    )
    max_pages: int | None = field(
        default=None, metadata={"description": "Максимум страниц для загрузки"}
    )
    timeout: int = field(
        default=20, metadata={"description": "Таймаут запроса в секундах"}
    )
    ssl_verify: bool = field(
        default=False, metadata={"description": "Проверять SSL сертификаты"}
    )


class ImportConfluenceSpaceTool(Tool[DocPipelineEvent, ImportSpaceParams]):
    """Загрузка целого пространства из Confluence."""

    def __init__(self, ws: Workspace, import_factory: ConfluenceImportFactory) -> None:
        self._ws = ws
        self._import_factory = import_factory

    @property
    def name(self) -> str:
        return "import_confluence_space"

    @property
    def description(self) -> str:
        return (
            "Загрузить все страницы из пространства Confluence по его ключу. "
            "Страницы сохраняются как HTML в рабочую папку. "
            "После загрузки выполни index_documents для индексации."
        )

    @property
    def params_type(self) -> type[ImportSpaceParams]:
        return ImportSpaceParams

    def execute(self, params: ImportSpaceParams) -> Iterator[DocToolOutput]:
        import_params = ConfluenceImportParams(
            timeout=params.timeout,
            ssl_verify=params.ssl_verify,
        )
        space_params = SpaceLoadParams(
            api_page_limit=params.api_page_limit,
            max_pages=params.max_pages,
        )
        importer = self._import_factory(import_params)

        ok = 0
        failed = 0
        total = 0
        for event in importer.import_space(
            params.space_key, space_params, self._ws.folder_path
        ):
            match event:
                case ImportSpaceEnumerated(total=t):
                    total = t
                case ImportPageSaved():
                    ok += 1
                case ImportPageFailed():
                    failed += 1
                case ImportDone():
                    pass

        yield ToolResult(
            content=f"Загрузка пространства '{params.space_key}': "
            f"{ok} из {total} страниц сохранено, {failed} ошибок.",
        )
