"""FormatPlan / FormatBlock — план рендера Section в LLM-формат.

Контракт между Section и format-aware chunker'ом: Section отдаёт не
строку, а структуру (repeat_header, body-блоки, breadcrumb-info), и
chunker уже сам решает, как нарезать body splitter'ом и реплицировать
header в каждый чанк.

Чисто DTO; никакой логики рендера или нарезки тут нет — Section
наполняет план в to_format_plan(), chunker его потребляет.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.indexing.location import ChunkLocation

__all__ = ["FormatBlock", "FormatPlan"]


@dataclass(frozen=True)
class FormatBlock:
    """Одна семантическая единица body для chunker'а.

    - format_content — markdown/plain для embedder/LLM.
    - raw_content    — оригинальный кусок исходника (HTML/markdown) для
                          UI/citation. Никогда не пуст: при отсутствии
                          per-unit raw — fallback на content всей секции.
    - location       — offset в исходнике (line-precision).
    - is_atomic      — flag «не резать char-split'ом»: короткий heading,
                          одна row таблицы, одна <li>.
    """

    format_content: str
    raw_content: str
    location: ChunkLocation
    is_atomic: bool = False


@dataclass(frozen=True)
class FormatPlan:
    """План рендера Section в LLM-формат для chunker'а.

    - blocks            — body-units в document-order. Пустой blocks
                             -> chunker пропускает секцию (например, <hr>).
    - repeat_header     — строка, которую chunker prepend-ит к каждому
                             чанку этой секции (markdown table-header,
                             открывающая code-fence). Учитывается в budget.
    - repeat_footer     — аналог в конце (закрывающая code-fence ).
    - block_glue        — разделитель при склейке блоков для splitter'а.
                             Подсказка: для row/list-item — \\n, для
                             параграфов — \\n\\n.
    - breadcrumb_level  — для HeadingSection: 1..6. Chunker обновляет
                             свой стек заголовков (pop до уровня, push).
    - breadcrumb_text   — для HeadingSection: текст для breadcrumbs.
    """

    blocks: tuple[FormatBlock, ...] = ()
    repeat_header: str = ""
    repeat_footer: str = ""
    block_glue: str = "\n\n"
    breadcrumb_level: int | None = None
    breadcrumb_text: str | None = None
