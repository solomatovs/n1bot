"""Tool: загрузка страниц из Confluence по ID."""

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
)
from boba_domain.importing.loading import ConfluenceImportParams
from boba_domain.workspace import Workspace

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class ImportPagesParams:
    """Параметры загрузки страниц из Confluence."""

    page_ids: str = field(
        metadata={"description": "ID страниц через запятую (например '12345,67890')"}
    )
    timeout: int = field(
        default=20, metadata={"description": "Таймаут запроса в секундах"}
    )
    ssl_verify: bool = field(
        default=False, metadata={"description": "Проверять SSL сертификаты"}
    )


class ImportConfluencePagesTool(Tool[DocPipelineEvent, ImportPagesParams]):
    """Загрузка страниц из Confluence по ID."""

    def __init__(self, ws: Workspace, import_factory: ConfluenceImportFactory) -> None:
        self._ws = ws
        self._import_factory = import_factory

    @property
    def name(self) -> str:
        return "import_confluence_pages"

    @property
    def description(self) -> str:
        return (
            "Загрузить страницы из Confluence по их ID. "
            "Укажи ID через запятую. Страницы сохраняются как HTML в рабочую папку. "
            "После загрузки выполни index_documents для индексации."
        )

    @property
    def params_type(self) -> type[ImportPagesParams]:
        return ImportPagesParams

    def execute(self, params: ImportPagesParams) -> Iterator[DocToolOutput]:
        page_ids = [pid.strip() for pid in params.page_ids.split(",") if pid.strip()]
        if not page_ids:
            yield ToolResult(content="Не указаны ID страниц.")
            return

        import_params = ConfluenceImportParams(
            timeout=params.timeout,
            ssl_verify=params.ssl_verify,
        )
        importer = self._import_factory(import_params)

        ok = 0
        failed = 0
        for event in importer.import_pages(page_ids, self._ws.folder_path):
            match event:
                case ImportPageSaved():
                    ok += 1
                case ImportPageFailed():
                    failed += 1
                case ImportDone():
                    pass

        yield ToolResult(
            content=f"Загрузка завершена: {ok} страниц сохранено, {failed} ошибок.",
        )
