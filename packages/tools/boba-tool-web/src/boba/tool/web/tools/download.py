"""
Tool web_download + WebDownloadConfig

`{dest_dir}/{host}/{sanitized_url_path}.{html|md}`.
Frontmatter с source-URL
"""

from __future__ import annotations

import posixpath
from typing import Annotated, Any, ClassVar

import httpx
from pydantic import Field

from boba.indexing import PipelineContext, PipelineId
from boba.settings import LLMStringList
from boba.tool.web.connection import WebConnection
from boba.tool.web.request_source import WebUrlsRequestSource
from boba.tool.web.tools._artifact import WebArtifact
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.http import HttpTransport
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceShell

__all__ = ["WebDownloadConfig", "web_download"]


class WebDownloadConfig(WebConnection):
    """Config tool'а `web_download`: web-профили (`WebConnection`) + dest_dir."""

    dest_dir: str = Field(
        min_length=1,
        description=(
            "Workspace-relative корень куда писать скачанные страницы. "
            "Создаётся если не существует."
        ),
    )
    max_returned_files: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Сколько путей возвращать LLM в ответе. Если total > лимита, "
            "список обрезается и `truncated=True` в ответе — LLM может "
            "достать полный список через `ls`/`tree`."
        ),
    )


class _WebDownloader:
    """Loop + per-doc запись на ФС; класс чтобы держать helper'ы вместе."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("web.download")

    def __init__(
        self,
        *,
        shell: WorkspaceShell,
        connection: WebConnection,
        dest_dir: str,
        as_markdown: bool,
    ) -> None:
        self._shell = shell
        self._connection = connection
        self._dest_rel = dest_dir.strip("/")
        self._as_markdown = as_markdown

    def run(self, urls: list[str]) -> list[dict[str, str]]:
        WebArtifact.ensure_parent(self._shell, posixpath.join(self._dest_rel, "_"))
        pctx = PipelineContext(pipeline_id=self.PIPELINE_ID)
        saved: list[dict[str, str]] = []
        try:
            for url in urls:
                # per-host транспорт: timeout/ssl/auth берутся из профиля хоста
                transport = HttpTransport(self._connection.resolve_profile(url))
                source = WebUrlsRequestSource(urls=(url,), connection=self._connection)
                for raw in transport.stream(pctx, source.stream(pctx)):
                    to_save = self._write_one(
                        raw_source_id=str(raw.source_id),
                        body=raw.handle,
                    )
                    saved.append(to_save)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"web_download failed: {type(e).__name__}: {e}",
            ) from e
        return saved

    def _write_one(
        self,
        *,
        raw_source_id: str,
        body: Any,
    ) -> dict[str, str]:
        rel = WebArtifact.relative_path(raw_source_id, as_markdown=self._as_markdown)
        path = posixpath.join(self._dest_rel, rel) if self._dest_rel else rel
        if self._as_markdown:
            WebArtifact.write_markdown(self._shell, path, url=raw_source_id, body=body)
            kind = "md"
        else:
            WebArtifact.write_raw(self._shell, path, url=raw_source_id, body=body)
            kind = "html"
        meta = self._shell.meta(path)
        return {
            "url": raw_source_id,
            "path": path,
            "kind": kind,
            "bytes": str(meta.size),
        }


@tool
def web_download(
    cfg: Annotated[WebDownloadConfig, FromConfig()],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    urls: Annotated[
        LLMStringList,
        Field(
            description=("Список URL для скачивания"),
        ),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=("true — конвертирует HTML→Markdown"),
        ),
    ],
) -> dict[str, Any]:
    """
    Скачивает список URL; возвращает `{saved, total, truncated}`.

    `urls` — обязателен. `as_markdown` — обязателен (нет неявного default'а).
    """
    if not urls:
        msg = "web_download: urls пуст — нечего скачивать."
        raise ValueError(msg)
    downloader = _WebDownloader(
        shell=shell,
        connection=cfg,
        dest_dir=cfg.dest_dir,
        as_markdown=as_markdown,
    )
    saved = downloader.run(urls)
    total = len(saved)
    limit = cfg.max_returned_files
    truncated = limit is not None and total > limit
    returned = saved[:limit] if truncated else saved
    return {
        "dest_dir": cfg.dest_dir.strip("/"),
        "total": total,
        "returned": len(returned),
        "truncated": truncated,
        "saved": returned,
    }
