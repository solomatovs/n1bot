"""Reader для HTML-файлов (включая Confluence-экспорт).

Лениво импортируется из `readers.__init__._try_extras_reader` —
зависит от ``beautifulsoup4`` и ``lxml`` (extras: ``[html]``).

Из текста удаляются:

- ``<script>`` / ``<style>`` целиком — это исполняемый код и стили,
  не контент;
- Confluence-макросы вида ``<ac:structured-macro …>`` и связанные
  ``<ac:parameter>`` / ``<ri:*>``-теги — служебная разметка экспорта,
  не нужная для поиска по смыслу.

Текст собирается через ``soup.get_text(" ")`` с разделителем-пробелом,
затем нормализуется: повторяющиеся пробелы/пустые строки склеиваются.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from bs4 import BeautifulSoup

from boba.cli.vector_index.readers.base import Document

_WS = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


class HtmlReader:
    extensions: tuple[str, ...] = (".html", ".htm")

    def read(self, path: str) -> Iterable[Document]:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        # lxml терпит Confluence-экспорт лучше html.parser: парсит ac:/ri:
        # неймспейсы как обычные теги, без разнотолков о namespace
        # bindings.
        soup = BeautifulSoup(raw, "lxml")

        for tag in soup(["script", "style"]):
            tag.decompose()

        # Confluence-макросы — служебная разметка экспорта (anchors,
        # bookmarks, page-properties) — выбрасываем целиком вместе
        # с их телом.
        for tag in soup.find_all(re.compile(r"^(ac:|ri:)")):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        text = soup.get_text(" ")
        text = _WS.sub(" ", text)
        text = _BLANK_LINES.sub("\n\n", text).strip()

        if not text:
            return

        metadata: dict[str, str] = {"format": "html"}
        if title:
            metadata["title"] = title

        yield Document(
            source_path=str(Path(path).resolve()),
            text=text,
            metadata=metadata,
        )
