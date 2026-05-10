"""boba.html_as_markdown — композитные Reader'ы HTML→Markdown.

Этот пакет — **мост** между `boba-html` и `boba-markdown`:
HTML на входе, конвертация в markdown через `markdownify`
(опционально — с предварительной очисткой через `trafilatura`),
затем разбиение готовым `MarkdownReader` на Section[str] с markdown-контентом.

Содержимое:

- `HtmlMarkdownifyReader` — конвертирует HTML в Markdown через `markdownify`,
  затем делегирует резку `MarkdownReader`. На выходе Section[str] с
  markdown-контентом и heading-anchor'ами.

- `HtmlExtractedMarkdownifyReader` — `trafilatura` (отбросить boilerplate)
  + `markdownify` (HTML→MD) + `MarkdownReader`. Цепочка для произвольного
  веба со шумом.

Pure HTML reader'ы (`HtmlHeadingReader`, `HtmlPlainReader`,
`HtmlSemanticReader`, `HtmlReadabilityReader`) живут в `boba-html` —
там нет зависимости на markdown-инфраструктуру.

Зависимости:
- `boba-html` — для `_HtmlBase` (noise-фильтр + title-extraction).
- `boba-markdown` — `MarkdownReader` для разбиения.
- `markdownify` — обязательная (HTML→MD конвертер).
- `trafilatura` — опциональная, нужна только для `HtmlExtractedMarkdownifyReader`.
  Установка: `pip install boba-html-as-markdown[readability]`.
"""

from __future__ import annotations

from boba.html_as_markdown.reader import (
    HtmlExtractedMarkdownifyReader,
    HtmlMarkdownifyReader,
)

__all__ = [
    "HtmlExtractedMarkdownifyReader",
    "HtmlMarkdownifyReader",
]
