"""Tool: загрузка страниц из Confluence по ID."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List

from adapters.confluence_importer import ConfluenceImporter
from domain.agent.events import DocPipelineEvent
from domain.agent.tools import Tool, ToolEvent, ToolOutput, ToolResult
from domain.importing.confluence import ImportDone, ImportPageFailed, ImportPageSaved
from domain.importing.loading import ConfluenceImportParams
from domain.config import AppConfig
from domain.workspace import Workspace

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class ImportPagesParams:
    """Параметры загрузки страниц из Confluence."""
    page_ids: str = field(metadata={"description": "ID страниц через запятую (например '12345,67890')"})
    timeout: int = field(default=20, metadata={"description": "Таймаут запроса в секундах"})
    ssl_verify: bool = field(default=False, metadata={"description": "Проверять SSL сертификаты"})


class ImportConfluencePagesTool(Tool[DocPipelineEvent, ImportPagesParams]):
    """Загрузка страниц из Confluence по ID."""

    def __init__(self, ws: Workspace, cfg: AppConfig) -> None:
        self._ws = ws
        self._cfg = cfg

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
        importer = ConfluenceImporter(self._cfg, import_params)

        ok = 0
        failed = 0
        for event in importer.import_pages(page_ids, self._ws.folder_path):
            match event:
                case ImportPageSaved(page_id=pid, title=title):
                    ok += 1
                case ImportPageFailed(page_id=pid, error=err):
                    failed += 1
                case ImportDone():
                    pass

        yield ToolResult(
            content=f"Загрузка завершена: {ok} страниц сохранено, {failed} ошибок.",
        )
