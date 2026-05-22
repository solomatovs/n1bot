"""Общая запись Confluence-страниц и их вложений на ФС для `confluence_download_*`-тулов.

`confluence_download_page` (явный список page_ids) и
`confluence_download_space` (весь space через discovery) делают одно и
то же тело: для каждого `HttpRequest` из переданного `RequestSource`
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

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
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
from boba.tool.kb.confluence.attachments import AttachmentInfo
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest

__all__ = ["download_pages"]

_FS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FS_COMPONENT_MAX = 120
_FILES_DIR_SUFFIX = "_files"
"""Convention для каталога с вложениями: `{page_id}_files/` рядом со страницей.

Совместимо с тем, как `Save As Webpage` в браузерах раскладывает ресурсы
страницы — узнаваемо и для людей, и для downstream-тулов."""

_STREAM_CHUNK = 64 * 1024


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


def download_pages(
    *,
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    dest_dir: str,
    as_markdown: bool,
    pipeline_id: PipelineId,
) -> dict[str, Any]:
    """
    Скачивает страницы (HTML/Markdown) и их вложения (бинарь) в `dest_dir`.

    Возвращает `{dest_dir, saved, total}`, где `saved` — список записей вида
    `{kind, page_id, ..., path, bytes}`. `kind` ∈ {`"page"`, `"attachment"`}.
    """
    dest_path = Path(dest_dir.rstrip("/"))
    _ensure_dir(dest_path)

    pctx = PipelineContext(pipeline_id=pipeline_id)
    docs = iter_confluence_documents(
        request_source=request_source, conn=conn, pctx=pctx,
    )
    try:
        saved = list(_stream_records(docs, dest_path, as_markdown=as_markdown))
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence download failed: {type(e).__name__}: {e}",
        ) from e

    return {
        "dest_dir": str(dest_path),
        "saved": saved,
        "total": len(saved),
    }


def _stream_records(
    docs: Iterable[RawDocument],
    dest_path: Path,
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
            yield _write_attachment(decoded, dest_path)
        else:
            yield _write_page(decoded, dest_path, as_markdown=as_markdown)


def _write_page(
    decoded: RawDocument, dest_path: Path, *, as_markdown: bool,
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
    page_dir = _compute_page_dir(dest_path, decoded)
    _ensure_dir(page_dir)
    file_path = page_dir / f"{page_id}.{ext}"
    _write_bytes(file_path, payload)
    return {
        "kind": "page",
        "page_id": page_id,
        "title": title,
        "url": url,
        "space_key": space_key,
        "path": str(file_path),
        "bytes": str(len(payload)),
    }


def _write_attachment(decoded: RawDocument, dest_path: Path) -> dict[str, str]:
    att = decoded.metadata.get(ConfluenceKeys.ATTACHMENT_INFO)
    if att is None:
        msg = (
            "internal: attachment RawDocument missing ATTACHMENT_INFO metadata "
            f"(source_id={decoded.source_id!r})"
        )
        raise RuntimeError(msg)
    page_id = decoded.metadata.get(ConfluenceKeys.PAGE_ID) or ""
    filename = _sanitize_component(att.title or att.id)
    page_dir = _compute_page_dir(dest_path, decoded)
    files_dir = page_dir / f"{page_id}{_FILES_DIR_SUFFIX}"
    _ensure_dir(files_dir)
    file_path = files_dir / filename
    bytes_written = _stream_to_file(decoded.handle, file_path)
    return {
        "kind": "attachment",
        "page_id": page_id,
        "attachment_id": att.id,
        "filename": att.title,
        "media_type": att.media_type,
        "url": decoded.source_id,
        "path": str(file_path),
        "bytes": str(bytes_written),
    }


def _compute_page_dir(dest_path: Path, decoded: RawDocument) -> Path:
    """`{dest}/{space?}/{ancestor1}/.../{ancestorN}/` — общее место для page и её attachments.

    Для attachment-документа эти ключи получены от родителя через
    `make_attachment_request` (см. `_pipeline_common`), так что file_path
    будет совпадать с тем, что вычислится для самой страницы — и
    attachment ляжет в `{page_id}_files/` рядом со страницей.
    """
    space_key = decoded.metadata.get(ConfluenceKeys.SPACE_KEY) or ""
    ancestors = decoded.metadata.get(ConfluenceKeys.ANCESTORS_TITLES) or ()
    page_dir = dest_path
    if space_key:
        page_dir = page_dir / _sanitize_component(space_key)
    for a_title in ancestors:
        page_dir = page_dir / _sanitize_component(a_title)
    return page_dir


def _ensure_dir(p: Path) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"Не удалось создать директорию {str(p)!r}: {e}",
        ) from e


def _write_bytes(p: Path, payload: bytes) -> None:
    try:
        p.write_bytes(payload)
    except OSError as e:
        raise RuntimeError(f"Ошибка записи файла {str(p)!r}: {e}") from e


def _stream_to_file(handle: BinaryStream, p: Path) -> int:
    """Чанками пишет `handle` в файл; возвращает количество записанных байт.

    Используем `read(chunk_size)` (а не `read()`) — для крупных вложений
    (PDF/видео) не хотим тащить всё в память до записи. `_ResponseHandle`
    поверх `httpx` корректно отдаёт chunked-ответ.
    """
    total = 0
    try:
        with p.open("wb") as f:
            while chunk := handle.read(_STREAM_CHUNK):
                f.write(chunk)
                total += len(chunk)
    except OSError as e:
        raise RuntimeError(f"Ошибка записи файла {str(p)!r}: {e}") from e
    return total


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
