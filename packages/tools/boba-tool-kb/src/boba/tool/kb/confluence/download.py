"""Tool confluence_download + ConfluenceDownloadConfig (unified HTTP-download).

Один tool, два режима — LLM заполняет РОВНО ОДИН из дискриминирующих
параметров (space_keys / page_ids):

- space_keys=[A, B]   -> все страницы перечисленных Confluence-spaces
                          (discovery /rest/api/space/{key}/content).
- page_ids=[123, 456] -> явный список страниц по ID.

as_markdown=True конвертирует HTML в Markdown (markdownify, ATX-заголовки)
и пишет .md с YAML-frontmatter; иначе — .html с HTML-комментарием-header'ом.

Запись на ФС инкапсулирована в ConfluenceDownloader: для каждого
HttpRequest из RequestSource ConfluenceContentTransport сначала отдаёт
декодированную HTML-страницу, а затем все её вложения как бинарные
RawDocument'ы (по очереди). Страница пишется в
{dest_dir}/{space}/{ancestors}/{page_id}.{html|md}, вложения — рядом в
{page_id}_files/{filename}; <img src>/<a href> переписываются на
локальные пути. Конфигурация — из секции [tool.kb.confluence.download].
"""

from __future__ import annotations

import io
import posixpath
import re
from collections.abc import Iterable, Iterator
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import unquote, urlparse

import httpx
import markdownify
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from boba.indexing import (
    BinaryStream,
    RawDocument,
    ReaderKeys,
    RequestSource,
)
from boba.settings import (
    LLMStringList,
    StringList,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import (
    AttachmentFilter,
    AttachmentInfo,
    ConfluenceKeys,
)
from boba.tool.kb.confluence.pipeline import ConfluenceContentTransport
from boba.tool.kb.confluence.request_sources import (
    ConfluenceMultiSpaceRequestSource,
    ConfluencePagesRequestSource,
    ConfluenceRequest,
)
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.http import HttpProfile
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceShell

__all__ = ["ConfluenceDownloadConfig", "confluence_download"]


class _CountingReader:
    """BinaryStream-обёртка, считающая сумму прочитанных байт."""

    def __init__(self, source: BinaryStream) -> None:
        self._source = source
        self.total = 0

    def read(self, n: int = -1, /) -> bytes:
        chunk = self._source.read(n)
        self.total += len(chunk)
        return chunk


class ConfluenceDownloadConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_download.

    Config-секция: [tool.kb.confluence.download].
    """

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    dest_dir: str = Field(
        default="kb/confluence",
        min_length=1,
        description=(
            "relative path для папки, в user сессии chainlit "
            "куда будут записаны скачанные файлы"
        ),
    )
    attachment_media_types: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.media_type` "
                "(напр. `application/pdf`, `image/*`). Если пусто И "
                "`attachment_titles` пуст — скачиваются ВСЕ вложения "
                "(старое поведение). Если задано — attachment проходит, "
                "если матчит хоть один паттерн в любом из двух списков (OR). "
                "Отсеянные вложения не запрашиваются по HTTP и не пишутся на диск."
            ),
        ),
    ] = []
    attachment_titles: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.title` "
                "(напр. `*.pdf`, `report-*.docx`). См. `attachment_media_types`."
            ),
        ),
    ] = []
    max_returned_files: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Сколько путей скачанных файлов возвращать LLM в ответе. "
            "Если задано и `total > max_returned_files` — список путей "
            "обрезается, в ответе появляется `truncated: true` и LLM "
            "должна получить полный список через `ls`/`tree`. "
            "По умолчанию (`None`) — возвращаются все."
        ),
    )


class ConfluenceDownloader:
    """Запись Confluence-страниц и их вложений на ФС внутри workspace-shell.

    Поддиректории повторяют структуру Confluence-URL: {space_key} сверху,
    затем дерево предков страницы (root -> direct parent). attachment-документы
    наследуют SPACE_KEY/ANCESTORS_TITLES родителя через
    ConfluenceRest.make_attachment_request, так что оказываются в той же папке.
    """

    _FS_FORBIDDEN: ClassVar[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    _FS_COMPONENT_MAX: ClassVar[int] = 120
    _FILES_DIR_SUFFIX: ClassVar[str] = "_files"
    """Convention для каталога с вложениями: {page_id}_files/ рядом со страницей.

    Совместимо с тем, как Save As Webpage в браузерах раскладывает ресурсы
    страницы — узнаваемо и для людей, и для downstream-тулов."""

    @staticmethod
    def run(  # noqa: PLR0913 — keyword-only helper, явный набор deps
        *,
        shell: WorkspaceShell[Any],
        request_source: RequestSource[ConfluenceRequest],
        conn: ConfluenceConnection,
        dest_dir: str,
        as_markdown: bool,
        attachment_filter: AttachmentFilter | None = None,
    ) -> dict[str, Any]:
        """
        Скачивает страницы (HTML/Markdown) и их вложения (бинарь) в dest_dir
        внутри переданного shell (workspace-relative путь).

        Возвращает {dest_dir, saved, total}, где saved — список записей вида
        {kind, page_id, ..., path, bytes}. path — workspace-relative, тот же
        формат принимают cat/grep/ls из boba-tool-files.

        attachment_filter (если задан) сужает скачиваемые вложения по
        media_type/title fnmatch-globs — отсеянные не запрашиваются по HTTP
        и не пишутся на диск. Сами страницы фильтр не трогает.
        """
        dest_rel = dest_dir.strip("/")
        ConfluenceDownloader._ensure_dir(shell, dest_rel)

        docs = ConfluenceContentTransport.iter_documents(
            request_source=request_source,
            conn=conn,
            attachment_filter=attachment_filter,
        )
        try:
            saved = list(
                ConfluenceDownloader._stream_records(
                    docs, shell, dest_rel, as_markdown=as_markdown,
                ),
            )
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Confluence download failed: {type(e).__name__}: {e}",
            ) from e

        return {
            "dest_dir": dest_rel,
            "saved": saved,
            "total": len(saved),
        }

    @staticmethod
    def _stream_records(
        docs: Iterable[RawDocument],
        shell: WorkspaceShell[Any],
        dest_rel: str,
        *,
        as_markdown: bool,
    ) -> Iterator[dict[str, str]]:
        """Generator-loop: один документ -> один write -> один yield записи.

        Без накопления: запись на диск происходит сразу при получении документа,
        saved-список материализуется только на API-границе. Это даёт два
        преимущества: (1) первый файл пишется как только пришёл первый HTTP,
        а не после всех; (2) очень большие выборки не держат всю историю
        в памяти, кроме итогового списка коротких dict-записей.
        """
        for decoded in docs:
            if decoded.metadata.has(ConfluenceKeys.ATTACHMENT_INFO):
                yield ConfluenceDownloader._write_attachment(decoded, shell, dest_rel)
            else:
                yield ConfluenceDownloader._write_page(
                    decoded, shell, dest_rel, as_markdown=as_markdown,
                )

    @staticmethod
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
            html = ConfluenceDownloader._rewrite_attachment_urls(
                html=html,
                attachments=attachments,
                local_dir=f"{page_id}{ConfluenceDownloader._FILES_DIR_SUFFIX}",
            )
        if as_markdown:
            body = markdownify.markdownify(html, heading_style="ATX")
            frontmatter = ConfluenceDownloader._md_frontmatter(
                url=url, title=title, page_id=page_id, space_key=space_key,
            )
            payload = (frontmatter + body).encode("utf-8")
            ext = "md"
        else:
            header = ConfluenceDownloader._html_header(
                url=url, title=title, page_id=page_id, space_key=space_key,
            )
            payload = header + html.encode("utf-8")
            ext = "html"
        page_dir = ConfluenceDownloader._compute_page_dir(dest_rel, decoded)
        ConfluenceDownloader._ensure_dir(shell, page_dir)
        file_path = posixpath.join(page_dir, f"{page_id}.{ext}")
        ConfluenceDownloader._atomic_write_stream(shell, file_path, io.BytesIO(payload))
        return {
            "kind": "page",
            "page_id": page_id,
            "title": title,
            "url": url,
            "space_key": space_key,
            "path": file_path,
            "bytes": str(len(payload)),
        }

    @staticmethod
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
        filename = ConfluenceDownloader._sanitize_component(att.title or att.id)
        page_dir = ConfluenceDownloader._compute_page_dir(dest_rel, decoded)
        files_dir = posixpath.join(
            page_dir, f"{page_id}{ConfluenceDownloader._FILES_DIR_SUFFIX}",
        )
        ConfluenceDownloader._ensure_dir(shell, files_dir)
        file_path = posixpath.join(files_dir, filename)
        counter = _CountingReader(decoded.handle)
        ConfluenceDownloader._atomic_write_stream(shell, file_path, counter)
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

    @staticmethod
    def _compute_page_dir(dest_rel: str, decoded: RawDocument) -> str:
        """
        {dest}/{space?}/{ancestor1}/.../{ancestorN}/
        общее место для page и её attachments.

        Для attachment-документа эти ключи получены от родителя через
        ConfluenceRest.make_attachment_request (см. pipeline), так что
        file_path будет совпадать с тем, что вычислится для самой страницы — и
        attachment ляжет в {page_id}_files/ рядом со страницей.
        """
        space_key = decoded.metadata.get(ConfluenceKeys.SPACE_KEY) or ""
        ancestors = decoded.metadata.get(ConfluenceKeys.ANCESTORS_TITLES) or ()
        parts = [dest_rel]
        if space_key:
            parts.append(ConfluenceDownloader._sanitize_component(space_key))
        for a_title in ancestors:
            parts.append(ConfluenceDownloader._sanitize_component(a_title))
        return posixpath.join(*parts)

    @staticmethod
    def _ensure_dir(shell: WorkspaceShell[Any], path: str) -> None:
        if shell.exists(path):
            return
        try:
            shell.mkdir(path)
        except OSError as e:
            raise RuntimeError(
                f"Не удалось создать директорию {path!r}: {e}",
            ) from e

    @staticmethod
    def _atomic_write_stream(
        shell: WorkspaceShell[Any], path: str, source: BinaryStream,
    ) -> None:
        """Атомарно записать поток в path: stream -> tmp -> fsync -> replace."""
        try:
            shell.atomic_write_binary(path, source)
        except OSError as e:
            raise RuntimeError(f"Ошибка записи файла {path!r}: {e}") from e

    @staticmethod
    def _sanitize_component(name: str) -> str:
        """Filesystem-safe имя компонента пути из произвольного title.

        Заменяет запрещённые символы на _, схлопывает пробелы, обрезает
        точки/пробелы по краям (Windows-совместимость) и длину до 120 символов.
        Возвращает "_" если после очистки строка пустая.
        """
        cleaned = ConfluenceDownloader._FS_FORBIDDEN.sub("_", name)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        if len(cleaned) > ConfluenceDownloader._FS_COMPONENT_MAX:
            cleaned = cleaned[: ConfluenceDownloader._FS_COMPONENT_MAX].rstrip(" .")
        return cleaned or "_"

    @staticmethod
    def _confluence_attachment_filename(url: str) -> str | None:
        """/download/attachments/<id>/<filename>?... -> <filename> (URL-decoded).

        Возвращает None, если URL не похож на Confluence-attachment.
        Принимает и относительные (/download/...), и абсолютные
        (https://confl.example.com/wiki/download/...) пути — нас интересует
        только последний path-сегмент после /download/attachments/<id>/.

        download/thumbnails/ тоже распознаётся — Confluence иногда подменяет
        inline <img src> на превью; имя файла там то же самое.
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

    @staticmethod
    def _rewrite_attachment_urls(
        *,
        html: str,
        attachments: tuple[AttachmentInfo, ...],
        local_dir: str,
    ) -> str:
        """Replace <img src> / <a href>-URL'ы вложений на {local_dir}/{filename}.

        Сопоставление — по filename'у (последний path-сегмент после
        /download/attachments/<id>/): Confluence в HTML и в _links.download
        кладёт одинаковые base-имена, но query-параметры могут отличаться
        (version=/modificationDate=), поэтому string-equality по URL
        ненадёжна. Sanitize filename'а при rewrite'е тот же, что при записи
        на диск — поэтому ссылка точно бьётся в файл.
        """
        if not attachments or not html:
            return html
        name_map = {
            att.title: ConfluenceDownloader._sanitize_component(att.title)
            for att in attachments
        }
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["img", "a"]):
            attr = "src" if tag.name == "img" else "href"
            url = tag.get(attr)
            if not isinstance(url, str) or not url:
                continue
            filename = ConfluenceDownloader._confluence_attachment_filename(url)
            if filename and filename in name_map:
                tag[attr] = f"{local_dir}/{name_map[filename]}"
        return str(soup)

    @staticmethod
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

    @staticmethod
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


@tool
def confluence_download(
    cfg: Annotated[ConfluenceDownloadConfig, FromConfig()],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    space_keys: Annotated[
        LLMStringList | None,
        Field(
            description=(
                "Mode A: скачать все страницы перечисленных Confluence-spaces. "
                'Передавай JSON-массив строк: `["DOCS", "ENG"]`.'
            ),
        ),
    ] = None,
    page_ids: Annotated[
        LLMStringList | None,
        Field(
            description=(
                "Mode B: скачать список page_id. Каждый id — строка из "
                "URL `viewpage.action?pageId=<id>`. Используй, если уже знаешь "
                "конкретные страницы. Взаимоисключающий с `space_keys`. "
                'Передавай JSON-массив строк: `["950276", "950278"]`.'
            ),
        ),
    ] = None,
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown (`markdownify`, "
                "ATX-заголовки) и пишет `.md` с YAML-frontmatter. По умолчанию "
                "сохраняется HTML с `<!--`-frontmatter."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Confluence-download: spaces / page_ids по выбору LLM.

    LLM заполняет ровно один из (space_keys, page_ids)

    Возвращает JSON {mode, <discriminator>, dest_dir, saved, total}.
    """
    modes = [
        ("space_keys", space_keys),
        ("page_ids", page_ids),
    ]
    chosen = [(name, val) for name, val in modes if val]
    if len(chosen) != 1:
        provided = [name for name, _ in chosen] or ["<none>"]
        msg = (
            "confluence_download: задай РОВНО ОДИН из (space_keys, page_ids); "
            f"получено {len(chosen)}: {provided}"
        )
        raise ValueError(msg)

    conn = ConfluenceConnection(profile=cfg.confluence, body_format=cfg.body_format)
    mode_name, mode_value = chosen[0]
    if mode_name == "space_keys":
        request_source = ConfluenceMultiSpaceRequestSource(
            conn=conn,
            space_keys=mode_value,
            body_format=conn.body_format,
        )
    else:  # page_ids
        request_source = ConfluencePagesRequestSource(
            base_url=conn.base_url,
            page_ids=mode_value,
            body_format=conn.body_format,
        )

    att_filter = AttachmentFilter.from_lists(
        media_types=cfg.attachment_media_types,
        titles=cfg.attachment_titles,
    )
    result = ConfluenceDownloader.run(
        shell=shell,
        request_source=request_source,
        conn=conn,
        dest_dir=cfg.dest_dir,
        as_markdown=as_markdown,
        attachment_filter=att_filter,
    )
    saved: list[dict[str, str]] = result["saved"]
    total: int = result["total"]
    limit = cfg.max_returned_files
    truncated = limit is not None and total > limit
    returned = saved[:limit] if truncated else saved
    return {
        "mode": mode_name,
        mode_name: mode_value,
        "dest_dir": result["dest_dir"],
        "total": total,
        "returned": len(returned),
        "truncated": truncated,
        "saved": returned,
    }
