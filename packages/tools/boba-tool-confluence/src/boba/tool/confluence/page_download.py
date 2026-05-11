"""Tool: скачать REST-JSON страниц Confluence в workspace-директорию.

Каждая страница из списка page_ids сохраняется как `{dest_dir}/{page_id}.json`
с сырым ответом REST API (без вынятия body). Дальше по этим файлам можно
ходить обычными file-tools (cat/grep/...).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

import httpx

from boba.indexing import PipelineContext, PipelineId
from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import NonEmpty
from boba.schema.coercion.types import ParseCsvList
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.keys import ConfluenceKeys
from boba.tool.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = [
    "ConfluencePageDownloadTool",
    "ConfluencePageDownloadToolConfig",
    "PageDownloadArgs",
]


@dataclass(frozen=True)
class PageDownloadArgs:
    """Скачивает указанные страницы Confluence в workspace как REST-JSON.

    На каждую страницу создаётся файл `{dest_dir}/{page_id}.json` с сырым
    ответом REST. Для последующего поиска используйте file-tools.
    """

    page_ids: Annotated[
        list[str],
        "ID страниц для скачивания (как в confluence_search/page_outline). "
        "В JSON-формате передавайте массив, через CLI — строку через запятую.",
        ParseCsvList(),
        NonEmpty(),
    ]
    dest_dir: Annotated[
        str,
        "Директория внутри workspace, куда сохранять файлы (создаётся, "
        "если не существует).",
        NonEmpty(),
    ]


@dataclass(frozen=True)
class ConfluencePageDownloadToolConfig:
    """DTO tool'а: connection + body_format + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    body_format: str
    prompt: PromptOverlay


class ConfluencePageDownloadTool(
    Tool[PageDownloadArgs, ConfluencePageDownloadToolConfig]
):
    """Скачивание страниц Confluence в workspace-директорию."""

    _PIPELINE_ID: ClassVar[PipelineId] = PipelineId("confluence.page_download")

    def __init__(self, cfg, ctx, source_id) -> None:
        super().__init__(cfg, ctx, source_id)
        self._shell: ProjectWorkspaceShell = ctx.get(ProjectWorkspaceShell)

    def execute(self, ctx: ToolContext, req: PageDownloadArgs) -> ToolResult:
        del ctx
        dest_dir = req.dest_dir.rstrip("/")
        try:
            if not self._shell.exists(dest_dir):
                self._shell.mkdir(dest_dir)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Не удалось создать директорию {dest_dir!r}: {e}",
            ) from e

        source = ConfluencePagesRequestSource(
            base_url=self._cfg.base_url,
            auth=ConfluenceConnection.make_auth(self._cfg),
            page_ids=req.page_ids,
            body_format=self._cfg.body_format,
        )
        transport = ConfluenceConnection.make_transport(self._cfg)
        pctx = PipelineContext(pipeline_id=self._PIPELINE_ID)

        saved: list[dict[str, str]] = []
        try:
            for http_req in source.stream(pctx):
                page_id = http_req.metadata.get(ConfluenceKeys.PAGE_ID) or ""
                for raw in transport.stream(pctx, [http_req]):
                    path = f"{dest_dir}/{page_id}.json"
                    payload = raw.handle.read()
                    self._write(path, payload)
                    saved.append({
                        "page_id": page_id,
                        "path": path,
                        "bytes": str(len(payload)),
                    })
        except httpx.HTTPError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Confluence page download failed: {type(e).__name__}: {e}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка записи в workspace: {e}",
            ) from e

        return JsonResult(
            payload={
                "dest_dir": dest_dir,
                "saved": saved,
                "total": len(saved),
            }
        )

    def _write(self, path: str, payload: bytes) -> None:
        with self._shell.write_binary(path) as f:
            f.write(payload)
