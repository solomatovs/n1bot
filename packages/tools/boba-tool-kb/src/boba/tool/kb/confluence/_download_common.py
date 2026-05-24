"""
Общая запись Confluence-страниц и их вложений на ФС
для unified-tool `confluence_download`

Два режима tool'а (`space_keys` / `page_ids`) делают одно и то же тело:
для каждого `HttpRequest` из переданного `RequestSource`
`ConfluenceContentTransport` сначала отдаёт декодированную HTML-страницу,
а затем все её вложения как бинарные `RawDocument`'ы (по очереди).
download:

- **page** (без `ConfluenceKeys.ATTACHMENT_INFO`) — HTML / Markdown пишется
  в `{dest_dir}/{space_key}/{ancestor_title}/.../{page_id}.{html|md}`.
  В HTML/Markdown все `<img src>` и `<a href>`, указывающие на attachment'ы
  этой страницы, переписываются на локальные пути `{page_id}_files/{filename}`
  (см. `_rewrite_attachment_urls`).
- **attachment** (с `ConfluenceKeys.ATTACHMENT_INFO`) — бинарь пишется в
  `{dest_dir}/{space_key}/{ancestor_title}/.../{page_id}_files/{filename}`,
  где `{filename}` это `_sanitize_component(attachment.title)`.

Поддиректории повторяют структуру Confluence-URL: `{space_key}` сверху,
затем дерево предков страницы (root → direct parent). Если `space_key`
отсутствует в metadata, этот уровень пропускается. attachment-документы
наследуют `SPACE_KEY`/`ANCESTORS_TITLES` родителя через
`make_attachment_request`, так что в той же папке оказываются.

Caller (tool-обёртка) собирает `RequestSource` (Pages|Space|Cql) и
зовёт `download_pages`.
"""

from __future__ import annotations

import io
import posixpath
import re
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import markdownify
from bs4 import BeautifulSoup

from boba.indexing import (
    BinaryStream,
    PipelineContext,
    PipelineId,
    RawDocument,
    ReaderKeys,
    RequestSource,
)
from boba.tool.kb.confluence._pipeline_common import iter_confluence_documents
from boba.tool.kb.confluence.attachments import AttachmentFilter, AttachmentInfo
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest
from boba.workspace.contract import WorkspaceShell

__all__ = ["download_pages"]

_FS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FS_COMPONENT_MAX = 120
_FILES_DIR_SUFFIX = "_files"
"""Convention для каталога с вложениями: `{page_id}_files/` рядом со страницей.

Совместимо с тем, как `Save As Webpage` в браузерах раскладывает ресурсы
страницы — узнаваемо и для людей, и для downstream-тулов."""

def _sanitize_component(name: str) -> str:
    """Filesystem-safe имя компонента пути из произвольного title.

    Заменяет запрещённые символы на `_`, схлопывает пробелы, обрезает
    точки/пробелы по краям (Windows-совместимость) и длину до 120 символов.
    Возвращает `"_"` если после очистки строка пустая.
    """
    cleaned = _FS_FORBIDDEN.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > _FS_COMPONENT_MAX:
        cleaned = cleaned[:_FS_COMPONENT_MAX].rstrip(" .")
    return cleaned or "_"


def download_pages(  # noqa: PLR0913 — keyword-only helper, явный набор deps
    *,
    shell: WorkspaceShell[Any],
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    dest_dir: str,
    as_markdown: bool,
    pipeline_id: PipelineId,
    attachment_filter: AttachmentFilter | None = None,
) -> dict[str, Any]:
    """
    Скачивает страницы (HTML/Markdown) и их вложения (бинарь) в `dest_dir`
    внутри переданного `shell` (workspace-relative путь).

    Возвращает `{dest_dir, saved, total}`, где `saved` — список записей вида
    `{kind, page_id, ..., path, bytes}`. `path` — workspace-relative, тот же
    формат принимают `cat`/`grep`/`ls` из `boba-tool-files`.

    `attachment_filter` (если задан) сужает скачиваемые вложения по
    `media_type`/`title` fnmatch-globs — отсеянные не запрашиваются по HTTP
    и не пишутся на диск. Сами страницы фильтр не трогает.
    """
    dest_rel = dest_dir.strip("/")
    _ensure_dir(shell, dest_rel)

    pctx = PipelineContext(pipeline_id=pipeline_id)
    docs = iter_confluence_documents(
        request_source=request_source,
        conn=conn,
        pctx=pctx,
        attachment_filter=attachment_filter,
    )
    try:
        saved = list(_stream_records(docs, shell, dest_rel, as_markdown=as_markdown))
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence download failed: {type(e).__name__}: {e}",
        ) from e

    return {
        "dest_dir": dest_rel,
        "saved": saved,
        "total": len(saved),
    }


def _stream_records(
    docs: Iterable[RawDocument],
    shell: WorkspaceShell[Any],
    dest_rel: str,
    *,
    as_markdown: bool,
) -> Iterator[dict[str, str]]:
    """Generator-loop: один документ → один write → один yield записи.

    Без накопления: запись на диск происходит сразу при получении документа,
    `saved`-список материализуется только на API-границе. Это даёт два
    преимущества: (1) первый файл пишется как только пришёл первый HTTP,
    а не после всех; (2) очень большие выборки не держат всю историю
    в памяти, кроме итогового списка коротких dict-записей.

    Free-function, экспортируется в `__all__` неявно — тесты драйвят его
    напрямую с фейк-iter'ом, не поднимая Confluence + Transport.
    """
    for decoded in docs:
        if decoded.metadata.has(ConfluenceKeys.ATTACHMENT_INFO):
            yield _write_attachment(decoded, shell, dest_rel)
        else:
            yield _write_page(decoded, shell, dest_rel, as_markdown=as_markdown)


def _write_page(
    decoded: RawDocument,
    shell: WorkspaceShell[Any],
    dest_rel: str,
    *,
    as_markdown: bool,
) -> dict[str, str]:
    page_id = decoded.metadata.get(ConfluenceKeys.PAGE_ID) or ""
    title = decoded.metadata.get(ReaderKeys.PAGE_TITLE) or ""
    url = decoded.source_id
    space_key = decoded.metadata.get(ConfluenceKeys.SPACE_KEY) or ""
    attachments = decoded.metadata.get(ConfluenceKeys.ATTACHMENTS) or ()
    html = decoded.handle.read().decode("utf-8", errors="replace")
    if attachments:
        html = _rewrite_attachment_urls(
            html=html,
            attachments=attachments,
            local_dir=f"{page_id}{_FILES_DIR_SUFFIX}",
        )
    if as_markdown:
        body = markdownify.markdownify(html, heading_style="ATX")
        frontmatter = _md_frontmatter(
            url=url, title=title, page_id=page_id, space_key=space_key,
        )
        payload = (frontmatter + body).encode("utf-8")
        ext = "md"
    else:
        header = _html_header(
            url=url, title=title, page_id=page_id, space_key=space_key,
        )
        payload = header + html.encode("utf-8")
        ext = "html"
    page_dir = _compute_page_dir(dest_rel, decoded)
    _ensure_dir(shell, page_dir)
    file_path = posixpath.join(page_dir, f"{page_id}.{ext}")
    _atomic_write_stream(shell, file_path, io.BytesIO(payload))
    return {
        "kind": "page",
        "page_id": page_id,
        "title": title,
        "url": url,
        "space_key": space_key,
        "path": file_path,
        "bytes": str(len(payload)),
    }


def _write_attachment(
    decoded: RawDocument,
    shell: WorkspaceShell[Any],
    dest_rel: str,
) -> dict[str, str]:
    att = decoded.metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
    if att is None:
        msg = (
            "internal: attachment RawDocument missing ATTACHMENT_INFO metadata "
            f"(source_id={decoded.source_id!r})"
        )
        raise RuntimeError(msg)
    page_id = decoded.metadata.get(ConfluenceKeys.PAGE_ID) or ""
    filename = _sanitize_component(att.title or att.id)
    page_dir = _compute_page_dir(dest_rel, decoded)
    files_dir = posixpath.join(page_dir, f"{page_id}{_FILES_DIR_SUFFIX}")
    _ensure_dir(shell, files_dir)
    file_path = posixpath.join(files_dir, filename)
    counter = _CountingReader(decoded.handle)
    _atomic_write_stream(shell, file_path, counter)
    bytes_written = counter.total
    return {
        "kind": "attachment",
        "page_id": page_id,
        "attachment_id": att.id,
        "filename": att.title,
        "media_type": att.media_type,
        "url": decoded.source_id,
        "path": file_path,
        "bytes": str(bytes_written),
    }


def _compute_page_dir(dest_rel: str, decoded: RawDocument) -> str:
    """
    `{dest}/{space?}/{ancestor1}/.../{ancestorN}/`
    общее место для page и её attachments.

    Для attachment-документа эти ключи получены от родителя через
    `make_attachment_request` (см. `_pipeline_common`), так что file_path
    будет совпадать с тем, что вычислится для самой страницы — и
    attachment ляжет в `{page_id}_files/` рядом со страницей.
    """
    space_key = decoded.metadata.get(ConfluenceKeys.SPACE_KEY) or ""
    ancestors = decoded.metadata.get(ConfluenceKeys.ANCESTORS_TITLES) or ()
    parts = [dest_rel]
    if space_key:
        parts.append(_sanitize_component(space_key))
    for a_title in ancestors:
        parts.append(_sanitize_component(a_title))
    return posixpath.join(*parts)


def _ensure_dir(shell: WorkspaceShell[Any], path: str) -> None:
    if shell.exists(path):
        return
    try:
        shell.mkdir(path)
    except OSError as e:
        raise RuntimeError(
            f"Не удалось создать директорию {path!r}: {e}",
        ) from e


def _atomic_write_stream(
    shell: WorkspaceShell[Any], path: str, source: BinaryStream,
) -> None:
    """Атомарно записать поток в `path`: stream → tmp → fsync → replace."""
    try:
        shell.atomic_write_binary(path, source)
    except OSError as e:
        raise RuntimeError(f"Ошибка записи файла {path!r}: {e}") from e


class _CountingReader:
    """`BinaryStream`-обёртка, считающая сумму прочитанных байт."""

    def __init__(self, source: BinaryStream) -> None:
        self._source = source
        self.total = 0

    def read(self, n: int = -1, /) -> bytes:
        chunk = self._source.read(n)
        self.total += len(chunk)
        return chunk


def _confluence_attachment_filename(url: str) -> str | None:
    """`/download/attachments/<id>/<filename>?...` → `<filename>` (URL-decoded).

    Возвращает `None`, если URL не похож на Confluence-attachment.
    Принимает и относительные (`/download/...`), и абсолютные
    (`https://confl.example.com/wiki/download/...`) пути — нас интересует
    только последний path-сегмент после `/download/attachments/<id>/`.

    `download/thumbnails/` тоже распознаётся — Confluence иногда подменяет
    inline `<img src>` на превью; имя файла там то же самое.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    path = parsed.path
    if "/download/attachments/" not in path and "/download/thumbnails/" not in path:
        return None
    last = path.rsplit("/", 1)[-1]
    if not last:
        return None
    return unquote(last)


def _rewrite_attachment_urls(
    *,
    html: str,
    attachments: tuple[AttachmentInfo, ...],
    local_dir: str,
) -> str:
    """Replace `<img src>` / `<a href>`-URL'ы вложений на `{local_dir}/{filename}`.

    Сопоставление — по filename'у (последний path-сегмент после
    `/download/attachments/<id>/`): Confluence в HTML и в `_links.download`
    кладёт одинаковые base-имена, но query-параметры могут отличаться
    (`version=`/`modificationDate=`), поэтому string-equality по URL
    ненадёжна. Sanitize filename'а при rewrite'е тот же, что при записи
    на диск — поэтому ссылка точно бьётся в файл.
    """
    if not attachments or not html:
        return html
    name_map = {att.title: _sanitize_component(att.title) for att in attachments}
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["img", "a"]):
        attr = "src" if tag.name == "img" else "href"
        url = tag.get(attr)
        if not isinstance(url, str) or not url:
            continue
        filename = _confluence_attachment_filename(url)
        if filename and filename in name_map:
            tag[attr] = f"{local_dir}/{name_map[filename]}"
    return str(soup)


def _html_header(
    *, url: str, title: str, page_id: str, space_key: str,
) -> bytes:
    """HTML-комментарий с источником страницы — для цитирования LLM."""
    lines = ["<!--", f"source: {url}", f"title: {title}", f"page_id: {page_id}"]
    if space_key:
        lines.append(f"space: {space_key}")
    lines.append("-->")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _md_frontmatter(
    *, url: str, title: str, page_id: str, space_key: str,
) -> str:
    """YAML-frontmatter с источником страницы (для kb_ingest и навигации)."""
    lines = ["---", f"source: {url}", f"title: {title}", f"page_id: {page_id}"]
    if space_key:
        lines.append(f"space: {space_key}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)
