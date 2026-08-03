"""boba.html — HTML-формат для индексации.

Содержимое:

- parser.py   — HtmlSectionParser: lxml AST + sourceline -> поток
  типизированных Section-ов с line-precision offset-tracking.
- reader.py   — HtmlReader (default), HtmlPlainReader (один Section на
  весь body), HtmlReadabilityReader (trafilatura для очистки от boilerplate).
- sections.py — HTML-специфичные Section-типы:
  HtmlParagraphSection, HtmlCodeBlockSection, HtmlTableSection,
  HtmlListSection, HtmlBlockquoteSection, HtmlHorizontalRuleSection.
  Доменные HeadingSection живут в boba.indexing.
- keys.py     — HtmlKeys: typed MetadataKey-и для html-полей.

Каждая Html*Section реализует to_format_plan() — markdown-render тела +
per-unit raw + replicate-header (для таблиц / code-fence). Чтобы фактически
довести этот сигнал до vector store, используйте boba.text.StructuralChunker
(а не базовый SectionChunker) — он реплицирует header в каждый row-чанк,
ведёт стек heading-breadcrumbs per source_id и пишет полный путь в
SectionKeys.HEADING_PATH.

Зависимости (обязательные):
- boba-indexing — Section[T] + базовые типы.
- lxml>=5.0 — block-level AST + sourceline.

Опционально:
- trafilatura — для HtmlReadabilityReader (boilerplate removal).
  Установка: pip install boba-html[readability].
- markdownify — для inline-markdown рендера в HtmlParagraphSection/
  HtmlBlockquoteSection (bold/italic/links). Без него — fallback на
  plain-text. Установка: pip install boba-html[markdown].
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from boba.html.keys import HtmlKeys
from boba.html.sections import (
    HtmlBlockquoteSection,
    HtmlCodeBlockSection,
    HtmlHorizontalRuleSection,
    HtmlListSection,
    HtmlParagraphSection,
    HtmlTableSection,
)

if TYPE_CHECKING:  # статически символы видны, в рантайме приезжают лениво
    from boba.html.parser import HtmlSectionParser, slugify
    from boba.html.reader import (
        HtmlPlainReader,
        HtmlReadabilityReader,
        HtmlReader,
    )

_LAZY: dict[str, str] = {
    "HtmlSectionParser": "boba.html.parser",
    "slugify": "boba.html.parser",
    "HtmlReader": "boba.html.reader",
    "HtmlPlainReader": "boba.html.reader",
    "HtmlReadabilityReader": "boba.html.reader",
}
"""Символы, тянущие lxml: импортируются при первом обращении.

Потребителю, которому нужны только ключи метаданных или типы секций, парсер
в процесс не приезжает — это важно там, где разбор HTML вынесен в песочницу.
"""


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415 — ленивый импорт по обращению

    return getattr(importlib.import_module(module_name), name)


__all__ = [
    "HtmlBlockquoteSection",
    "HtmlCodeBlockSection",
    "HtmlHorizontalRuleSection",
    "HtmlKeys",
    "HtmlListSection",
    "HtmlParagraphSection",
    "HtmlPlainReader",
    "HtmlReadabilityReader",
    "HtmlReader",
    "HtmlSectionParser",
    "HtmlTableSection",
    "slugify",
]
