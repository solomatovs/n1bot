"""Tool: скачать HTML-страницы Confluence в workspace-директорию.

Каждая страница из списка page_ids сохраняется как `{dest_dir}/{page_id}.html` —
тело страницы в выбранной body_format-репрезентации (storage/view/export_view/...).
Дальше по этим файлам можно ходить html-tools (html_outline/html_section) или
обычными file-tools (cat/grep/...).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.indexing import PipelineContext, PipelineId, ReaderKeys
from boba.plugin.prompt import PromptOverlay
from boba.tool.confluence.connection import (
    ConfluenceConnection,
)
from boba.tool.confluence.decoder import ConfluenceJsonDecoder
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


class PageDownloadArgs(BaseModel):
    """Скачивает указанные страницы Confluence как HTML-файлы в workspace.

    На каждую страницу создаётся файл `{dest_dir}/{page_id}.html` с HTML-телом
    в выбранной body_format-репрезентации. Дальше — html_outline/html_section
    или обычные file-tools.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_ids: list[str] = Field(
        min_length=1,
        description=(
            "ID страниц для скачивания (как в confluence_search/page_outline). "
            "Передавайте JSON-массив строк."
        ),
    )
    dest_dir: str = Field(
        min_length=1,
        description=(
            "Директория внутри workspace, куда сохранять файлы (создаётся, "
            "если не существует)."
        ),
    )


@dataclass(frozen=True)
class ConfluencePageDownloadToolConfig:
    """DTO tool'а: connection + body_format + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    ssl_verify: bool
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
        decoder = ConfluenceJsonDecoder(body_format=self._cfg.body_format)
        pctx = PipelineContext(pipeline_id=self._PIPELINE_ID)

        saved: list[dict[str, str]] = []
        try:
            for http_req in source.stream(pctx):
                page_id = http_req.metadata.get(ConfluenceKeys.PAGE_ID) or ""
                for raw in transport.stream(pctx, [http_req]):
                    decoded = decoder.convert(raw)
                    title = decoded.metadata.get(ReaderKeys.PAGE_TITLE) or ""
                    url = decoded.source_id
                    space_key = decoded.metadata.get(ConfluenceKeys.SPACE_KEY) or ""
                    header = self._html_header(
                        url=url,
                        title=title,
                        page_id=page_id,
                        space_key=space_key,
                    )
                    html_bytes = header + decoded.handle.read()
                    path = f"{dest_dir}/{page_id}.html"
                    self._write(path, html_bytes)
                    saved.append(
                        {
                            "page_id": page_id,
                            "title": title,
                            "url": url,
                            "space_key": space_key,
                            "path": path,
                            "bytes": str(len(html_bytes)),
                        }
                    )
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

    @staticmethod
    def _html_header(
        *,
        url: str,
        title: str,
        page_id: str,
        space_key: str,
    ) -> bytes:
        """HTML-комментарий с источником страницы — для цитирования LLM."""
        lines = [
            "<!--",
            f"source: {url}",
            f"title: {title}",
            f"page_id: {page_id}",
        ]
        if space_key:
            lines.append(f"space: {space_key}")
        lines.append("-->")
        lines.append("")
        return "\n".join(lines).encode("utf-8")
