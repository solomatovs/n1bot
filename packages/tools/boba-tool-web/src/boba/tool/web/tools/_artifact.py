"""WebArtifact: helpers для записи скачанной web-страницы на ФС.

Один класс держит весь shared-API между web_fetch и web_download:
- путь файла из URL ({host}/{sanitized_path}.{ext}),
- frontmatter с source-метаданными (HTML-комментарий или YAML),
- streaming-запись raw HTML / буферизованная запись Markdown.

Правило проекта: без модуль-level helper'ов — всё статикой/nested внутри класса.
"""

from __future__ import annotations

import io
import posixpath
import re
from typing import ClassVar
from urllib.parse import urlparse

import markdownify

from boba.indexing import BinaryStream
from boba.workspace.contract import WorkspaceShell

__all__ = ["WebArtifact"]


class WebArtifact:
    """Static-API: путь файла + frontmatter + запись через WorkspaceShell."""

    FS_FORBIDDEN: ClassVar[re.Pattern[str]] = re.compile(
        r'[<>:"\\|?*\x00-\x1f]',
    )
    FS_COMPONENT_MAX: ClassVar[int] = 120
    DEFAULT_INDEX_NAME: ClassVar[str] = "index"

    class _ConcatBinaryStream:
        """BinaryStream-обёртка: header -> body без буферизации body целиком."""

        def __init__(self, *streams: BinaryStream) -> None:
            self._streams = list(streams)

        def read(self, n: int = -1, /) -> bytes:
            if n < 0:
                return b"".join(s.read(-1) for s in self._streams)
            buf = b""
            while self._streams and len(buf) < n:
                chunk = self._streams[0].read(n - len(buf))
                if not chunk:
                    self._streams.pop(0)
                    continue
                buf += chunk
            return buf

    @staticmethod
    def sanitize(component: str) -> str:
        """Filesystem-safe имя компонента пути."""
        cleaned = WebArtifact.FS_FORBIDDEN.sub("_", component)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        if len(cleaned) > WebArtifact.FS_COMPONENT_MAX:
            cleaned = cleaned[: WebArtifact.FS_COMPONENT_MAX].rstrip(" .")
        return cleaned or "_"

    @staticmethod
    def relative_path(url: str, *, as_markdown: bool) -> str:
        """URL -> workspace-relative путь без base-dir'а.

        {host}/{sanitized url path components}/{name}.{html|md}.
        Query/fragment отбрасываются (для имени файла не нужны).
        Пустой path или path='/' -> index.{ext}.
        """
        parsed = urlparse(url)
        host = (parsed.hostname or "_").lower()
        raw_parts = [p for p in parsed.path.split("/") if p]
        ext = "md" if as_markdown else "html"
        if not raw_parts:
            return posixpath.join(
                WebArtifact.sanitize(host),
                f"{WebArtifact.DEFAULT_INDEX_NAME}.{ext}",
            )
        sanitized = [WebArtifact.sanitize(p) for p in raw_parts]
        last = sanitized[-1]
        stem, dot, _ = last.rpartition(".")
        name = stem if dot else last
        sanitized[-1] = f"{name}.{ext}"
        return posixpath.join(WebArtifact.sanitize(host), *sanitized)

    @staticmethod
    def html_header(*, url: str) -> bytes:
        return f"<!--\nsource: {url}\n-->\n".encode()

    @staticmethod
    def md_frontmatter(*, url: str) -> str:
        return f"---\nsource: {url}\n---\n"

    @staticmethod
    def ensure_parent(shell: WorkspaceShell, path: str) -> None:
        """mkdir -p для всех родителей path внутри shell."""
        parent = posixpath.dirname(path)
        if not parent or shell.exists(parent):
            return
        try:
            shell.mkdir(parent)
        except OSError as e:
            raise RuntimeError(
                f"web: не удалось создать директорию {parent!r}: {e}",
            ) from e

    @staticmethod
    def write_raw(
        shell: WorkspaceShell,
        path: str,
        *,
        url: str,
        body: BinaryStream,
    ) -> None:
        """Стрим HTML-header'а + тела ответа в файл без буферизации body."""
        WebArtifact.ensure_parent(shell, path)
        header = WebArtifact.html_header(url=url)
        stream = WebArtifact._ConcatBinaryStream(io.BytesIO(header), body)
        try:
            shell.atomic_write_binary(path, stream)
        except OSError as e:
            raise RuntimeError(f"web: ошибка записи {path!r}: {e}") from e

    @staticmethod
    def write_markdown(
        shell: WorkspaceShell,
        path: str,
        *,
        url: str,
        body: BinaryStream,
    ) -> None:
        """Полностью читает HTML, конвертирует в Markdown, записывает.

        Markdownify не потоковый — для конвертации нужна вся страница в RAM.
        Это явное исключение из streaming-инварианта write_raw.
        """
        WebArtifact.ensure_parent(shell, path)
        html = body.read().decode("utf-8", errors="replace")
        md = markdownify.markdownify(html, heading_style="ATX")
        payload = (WebArtifact.md_frontmatter(url=url) + md).encode("utf-8")
        try:
            shell.atomic_write_binary(path, io.BytesIO(payload))
        except OSError as e:
            raise RuntimeError(f"web: ошибка записи {path!r}: {e}") from e
