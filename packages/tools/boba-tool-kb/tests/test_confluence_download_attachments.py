"""`download_pages` + helpers: offline-clone страницы со вложениями.

Три блока:
1. `_confluence_attachment_filename` — извлечение filename'а из URL'ов разных
   форматов (относительный/абсолютный, attachments/thumbnails, с query).
2. `_rewrite_attachment_urls` — переписывание `<img src>` и `<a href>` на
   локальные `{local_dir}/{filename}`, с пропуском URL'ов не из этого
   списка вложений.
3. `_stream_records` — full-loop download'а на герметичном iter'е
   `RawDocument`'ов (page + attachments), проверка layout'а файлов,
   рерайта HTML и записей в `saved`.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from boba.agent.workspace_fs.shell import FsWorkspaceShell
from boba.indexing import Metadata, RawDocument, SourceId, TransportKeys
from boba.tool.kb.confluence.download import ConfluenceDownloader
from boba.tool.kb.confluence.models import AttachmentInfo, ConfluenceKeys
from boba.workspace.contract import WorkspaceShell

# Короткие алиасы на staticmethod'ы downloader'а — драйвим их напрямую.
_confluence_attachment_filename = ConfluenceDownloader._confluence_attachment_filename
_rewrite_attachment_urls = ConfluenceDownloader._rewrite_attachment_urls
_stream_records = ConfluenceDownloader._stream_records


def _mk_shell(root: Path) -> WorkspaceShell:
    """`WorkspaceShell` поверх `root` для теста.

    `_stream_records`/`_write_page`/`_write_attachment` ожидают именно
    `WorkspaceShell`-абстракцию (mkdir / atomic_write_binary), а не
    голый `pathlib.Path`.
    """
    return FsWorkspaceShell(workspace_id="test", root=root)  # type: ignore[arg-type]

# -------- _confluence_attachment_filename --------

def test_filename_from_relative_url() -> None:
    assert (
        _confluence_attachment_filename(
            "/download/attachments/42/diagram.png?version=1&modificationDate=x",
        )
        == "diagram.png"
    )


def test_filename_from_absolute_url() -> None:
    assert (
        _confluence_attachment_filename(
            "https://confl.example.com/wiki/download/attachments/42/spec.pdf?v=1",
        )
        == "spec.pdf"
    )


def test_filename_from_thumbnails_url() -> None:
    """Confluence иногда рендерит inline-картинки через `/download/thumbnails/`."""
    assert (
        _confluence_attachment_filename("/download/thumbnails/42/diagram.png")
        == "diagram.png"
    )


def test_filename_returns_none_for_non_attachment_url() -> None:
    assert _confluence_attachment_filename("https://example.com/foo/bar.png") is None
    assert _confluence_attachment_filename("/pages/viewpage.action?pageId=42") is None


def test_filename_url_decoded() -> None:
    assert (
        _confluence_attachment_filename(
            "/download/attachments/42/my%20diagram.png?version=1",
        )
        == "my diagram.png"
    )


# -------- _rewrite_attachment_urls --------

_ATT = AttachmentInfo(
    id="att-1",
    title="diagram.png",
    media_type="image/png",
    file_size=100,
    download_path="/download/attachments/42/diagram.png?version=1",
    version=1,
)
_ATT_PDF = AttachmentInfo(
    id="att-2",
    title="spec.pdf",
    media_type="application/pdf",
    file_size=200,
    download_path="/download/attachments/42/spec.pdf?version=1",
    version=1,
)


def test_rewrite_img_src() -> None:
    html = (
        '<p>see <img src="/download/attachments/42/diagram.png?version=1&amp;'
        'modificationDate=99" alt="x"/></p>'
    )
    out = _rewrite_attachment_urls(html=html, attachments=(_ATT,), local_dir="42_files")
    assert 'src="42_files/diagram.png"' in out
    assert "/download/attachments/" not in out


def test_rewrite_a_href() -> None:
    html = '<p><a href="/download/attachments/42/spec.pdf?version=1">spec</a></p>'
    out = _rewrite_attachment_urls(
        html=html, attachments=(_ATT_PDF,), local_dir="42_files",
    )
    assert 'href="42_files/spec.pdf"' in out


def test_rewrite_leaves_unrelated_urls_alone() -> None:
    """Внешние ссылки и ссылки на чужие attachment'ы не должны переписываться."""
    html = (
        '<p>'
        '<a href="https://example.com/foo">ext</a>'
        '<img src="/download/attachments/42/other.png"/>'
        '<a href="/pages/viewpage.action?pageId=99">other-page</a>'
        '</p>'
    )
    out = _rewrite_attachment_urls(html=html, attachments=(_ATT,), local_dir="42_files")
    assert 'href="https://example.com/foo"' in out
    assert "/download/attachments/42/other.png" in out
    assert "viewpage.action?pageId=99" in out


def test_rewrite_handles_thumbnails_too() -> None:
    """`<img src="/download/thumbnails/...">` (preview inline) — тоже переписывается."""
    html = '<img src="/download/thumbnails/42/diagram.png"/>'
    out = _rewrite_attachment_urls(html=html, attachments=(_ATT,), local_dir="42_files")
    assert 'src="42_files/diagram.png"' in out


def test_rewrite_uses_sanitized_local_filename() -> None:
    """Filename с запрещёнными символами — sanitize применяется к локальному пути.

    Confluence-side имя в URL приходит URL-encoded (`%3A` для `:`, `%3F` для `?`);
    `_confluence_attachment_filename` декодирует обратно и сравнивает по
    оригинальному `att.title`. Локальная ссылка идёт на sanitized-вариант,
    который точно совпадёт с именем файла, который запишет `_write_attachment`.
    """
    att = AttachmentInfo(
        id="att-x",
        title="weird: name?.png",
        media_type="image/png",
        file_size=10,
        download_path="/download/attachments/42/weird%3A%20name%3F.png",
        version=1,
    )
    html = '<img src="/download/attachments/42/weird%3A%20name%3F.png"/>'
    out = _rewrite_attachment_urls(html=html, attachments=(att,), local_dir="42_files")
    assert "weird_ name_.png" in out
    assert "weird%3A" not in out


def test_rewrite_noop_on_no_attachments() -> None:
    html = '<img src="/download/attachments/42/x.png"/>'
    out = _rewrite_attachment_urls(html=html, attachments=(), local_dir="42_files")
    assert out == html


# -------- _stream_records --------

def _page_doc(  # noqa: PLR0913 — test fixture builder, явный набор metadata-полей
    *, page_id: str, space: str | None, ancestors: tuple[str, ...],
    title: str, html: str, attachments: tuple[AttachmentInfo, ...],
) -> RawDocument:
    meta = (
        Metadata.empty()
        .set(ConfluenceKeys.PAGE_ID, page_id)
        .set(TransportKeys.CONTENT_TYPE, "text/html")
    )
    if space:
        meta = meta.set(ConfluenceKeys.SPACE_KEY, space)
    if ancestors:
        meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ancestors)
    if title:
        from boba.indexing import ReaderKeys
        meta = meta.set(ReaderKeys.PAGE_TITLE, title)
    if attachments:
        meta = meta.set(ConfluenceKeys.ATTACHMENTS, attachments)
    return RawDocument(
        handle=BytesIO(html.encode("utf-8")),
        source_id=SourceId(f"https://x/pages/viewpage.action?pageId={page_id}"),
        metadata=meta,
    )


def _att_doc(
    *, page_id: str, space: str | None, ancestors: tuple[str, ...],
    att: AttachmentInfo, payload: bytes,
) -> RawDocument:
    meta = (
        Metadata.empty()
        .set(ConfluenceKeys.PAGE_ID, page_id)
        .set(ConfluenceKeys.ATTACHMENT_INFO, att)
        .set(TransportKeys.CONTENT_TYPE, att.media_type)
    )
    if space:
        meta = meta.set(ConfluenceKeys.SPACE_KEY, space)
    if ancestors:
        meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ancestors)
    return RawDocument(
        handle=BytesIO(payload),
        source_id=SourceId(f"https://x{att.download_path}"),
        metadata=meta,
    )


def test_stream_records_writes_page_html_with_rewritten_links(tmp_path: Path) -> None:
    page = _page_doc(
        page_id="42",
        space="DEMO",
        ancestors=("Root", "Parent"),
        title="Demo",
        html='<p><img src="/download/attachments/42/diagram.png?version=1"/></p>',
        attachments=(_ATT,),
    )
    att = _att_doc(
        page_id="42",
        space="DEMO",
        ancestors=("Root", "Parent"),
        att=_ATT,
        payload=b"\x89PNG\r\n...",
    )
    shell = _mk_shell(tmp_path)
    records = list(_stream_records([page, att], shell, "", as_markdown=False))

    page_path = tmp_path / "DEMO" / "Root" / "Parent" / "42.html"
    att_path = tmp_path / "DEMO" / "Root" / "Parent" / "42_files" / "diagram.png"
    assert page_path.exists()
    assert att_path.exists()
    # HTML переписан: src указывает на локальный файл
    page_text = page_path.read_text(encoding="utf-8")
    assert 'src="42_files/diagram.png"' in page_text
    assert "/download/attachments/" not in page_text
    # Binary записан байт-в-байт
    assert att_path.read_bytes() == b"\x89PNG\r\n..."
    # saved-list: 2 записи с правильными discriminator'ами
    kinds = [r["kind"] for r in records]
    assert kinds == ["page", "attachment"]
    assert records[1]["attachment_id"] == "att-1"
    assert records[1]["filename"] == "diagram.png"


def test_stream_records_markdown_keeps_local_paths(tmp_path: Path) -> None:
    """`as_markdown=True` — markdownify сохраняет local-paths из rewritten HTML."""
    page = _page_doc(
        page_id="42",
        space=None,
        ancestors=(),
        title="Demo",
        html='<p><img src="/download/attachments/42/diagram.png?v=1" alt="d"/></p>',
        attachments=(_ATT,),
    )
    shell = _mk_shell(tmp_path)
    list(_stream_records([page], shell, "", as_markdown=True))
    md = (tmp_path / "42.md").read_text(encoding="utf-8")
    assert "42_files/diagram.png" in md
    assert "/download/attachments/" not in md


def test_stream_records_attachment_without_space_falls_back_to_dest_root(
    tmp_path: Path,
) -> None:
    """Page/attachment без SPACE_KEY/ANCESTORS — пишутся в корень `dest_path`."""
    page = _page_doc(
        page_id="99",
        space=None,
        ancestors=(),
        title="Lone",
        html='<p>x</p>',
        attachments=(_ATT,),
    )
    att = _att_doc(
        page_id="99",
        space=None,
        ancestors=(),
        att=_ATT,
        payload=b"img-bytes",
    )
    shell = _mk_shell(tmp_path)
    list(_stream_records([page, att], shell, "", as_markdown=False))
    assert (tmp_path / "99.html").exists()
    assert (tmp_path / "99_files" / "diagram.png").exists()


def test_stream_records_yields_lazily(tmp_path: Path) -> None:
    """Один pull = один write. Второй документ не трогается, пока его не пуллят."""
    written: list[str] = []
    page = _page_doc(
        page_id="42",
        space=None,
        ancestors=(),
        title="P",
        html="<p>x</p>",
        attachments=(),
    )
    att = _att_doc(
        page_id="42",
        space=None,
        ancestors=(),
        att=_ATT,
        payload=b"png",
    )

    def trace(docs):
        for d in docs:
            written.append(
                "att" if d.metadata.has(ConfluenceKeys.ATTACHMENT_INFO) else "page"
            )
            yield d

    shell = _mk_shell(tmp_path)
    gen = _stream_records(trace([page, att]), shell, "", as_markdown=False)
    next(gen)
    assert written == ["page"]
    next(gen)
    assert written == ["page", "att"]
