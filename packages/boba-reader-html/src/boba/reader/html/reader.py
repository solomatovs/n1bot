"""HtmlReader: HTML → heading-aware Section[str].

Структурно как ConfluenceReader, но без Confluence-специфики (`ac:*`).
Для каждого `<h1>..<h6>` создаёт свою Section с anchor'ом из html `id`
(или fallback `idx:N`). Текст Section'и — текст самого heading'а +
всё содержимое до следующего heading'а.

Если в документе нет heading'ов — fallback одной Section со всем
текстом body + title (если есть `<title>`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)
from boba.reader.html.keys import HtmlKeys
from boba.reader.html.parser import (
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    text_between,
)

__all__ = ["HtmlReader"]


_NOISE_TAGS = ("script", "style")


class HtmlReader(Reader[str]):
    """
    `Reader[str]` для HTML: режет документ на Section'и по `<h1>..<h6>`.

    **Схема**:
    ```html
    <html>
      <head><title>Doc</title></head>
      <body>
        <h1 id="intro">Intro</h1>
        <p>intro body</p>

        <h2 id="api">API</h2>
        <p>api body</p>
      </body>
    </html>
    ```
    ```python
    Section(
        content="Intro\\n\\nintro body",
        anchor="intro",
        order=0,
        metadata={
            DOC_TYPE: "html",
            HEADING_LEVEL: 1,
            HEADING_TEXT: "Intro",
            PAGE_TITLE: "Doc"
        }
    )
    Section(
        content="API\\n\\napi body",
        anchor="api",
        order=1,
        metadata={
            DOC_TYPE: "html",
            HEADING_LEVEL: 2,
            HEADING_TEXT: "API",
            PAGE_TITLE: "Doc"
        }
    )
    ```

    **Поведение**:
    - `<script>`/`<style>` декомпозятся (текст не попадает в Section'и).
    - `anchor` берётся из html `id` heading, но если нет то fallback — `"idx:N"` (порядковый номер)
    - Heading с пустым текстом пропускаются как неинформативные.
    - Без heading'ов — одна Section со всем `body` + `<title>` (если есть);
      `anchor=None`, `order=0`.
    - Если документ пуст — итератор пуст.

    **Пример**:
    ```python
    reader = HtmlReader()
    raw = RawDocument(
        handle=BytesIO(
            b"<html><head><title>Doc</title></head><body>"
            b"<h1 id='intro'>Intro</h1><p>intro body</p>"
            b"<h2 id='api'>API</h2><p>api body</p>"
            b"</body></html>"
        ),
        source_id=SourceId("doc1"),
    )

    # Документ с 2 heading'ами → 2 Section'и; <title> кладётся в metadata.PAGE_TITLE.
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("doc1"),         # pass из RawDocument
            content="Intro\\n\\nintro body",     # новое: heading + текст до следующего heading'а
            anchor="intro",                     # новое: html id="intro" → anchor
            order=1,                            # новое: порядок heading'а в документе
            metadata=(                          # merge: + DOC_TYPE / HEADING_* / PAGE_TITLE
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "html")
                .set(HtmlKeys.HEADING_LEVEL, 1)
                .set(HtmlKeys.HEADING_TEXT, "Intro")
                .set(ReaderKeys.PAGE_TITLE, "Doc")
            ),
        ),
        Section(
            source_id=SourceId("doc1"),
            content="API\\n\\napi body",
            anchor="api",
            order=2,
            metadata=(
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "html")
                .set(HtmlKeys.HEADING_LEVEL, 2)
                .set(HtmlKeys.HEADING_TEXT, "API")
                .set(ReaderKeys.PAGE_TITLE, "Doc")
            ),
        ),
    ]
    ```
    """  # noqa: E501

    DOC_TYPE: ClassVar[str] = "html"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html")

    def name(self) -> str:
        return "HtmlReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = parse_html(payload)
        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        body = soup.body or soup
        # Heading'и без текста — неинформативные, пропускаем.
        headings = [h for h in collect_headings(soup) if h.text.strip()]

        if not headings:
            yield from self._fallback_section(value, body, title)
            return

        for i, h in enumerate(headings):
            next_tag = headings[i + 1].tag if i + 1 < len(headings) else None
            between = text_between(h.tag, next_tag)
            text = h.text + (("\n\n" + between) if between else "")
            meta = (
                value.metadata
                .set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
                .set(HtmlKeys.HEADING_LEVEL, h.level)
                .set(HtmlKeys.HEADING_TEXT, h.text)
            )
            if title:
                meta = meta.set(ReaderKeys.PAGE_TITLE, title)
            yield Section(
                source_id=value.source_id,
                content=text.strip(),
                anchor=anchor_for(h),
                order=h.index,
                metadata=meta,
            )

    def _fallback_section(
        self, value: RawDocument, body, title: str
    ) -> Iterable[Section[str]]:
        """HTML без heading'ов: одна Section с title + полным body-текстом."""
        text = plain_text(body)
        if not text and not title:
            return
        composed = f"{title}\n\n{text}".strip() if title else text
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)
        yield Section(
            source_id=value.source_id,
            content=composed,
            anchor=None,
            order=0,
            metadata=meta,
        )
