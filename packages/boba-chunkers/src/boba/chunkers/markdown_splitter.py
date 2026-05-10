"""MarkdownAwareSplitter — `OverlapCharSplitter` с markdown-aware separators.

`OverlapCharSplitter` режет рекурсивно по списку separator'ов. Этот класс
переопределяет `DEFAULT_SEPARATORS` так, чтобы первыми пробовались границы
markdown-структур (horizontal rules, paragraph break), и только если не
помогло — обычные `\\n` / ` ` / `""`.

Цель — резать markdown в местах, где это **наименее ломает структуру**:
- по горизонтальным линейкам (`---`, `***`, `___`) — markdown-разделители разделов;
- между параграфами (`\\n\\n`) — heading'и и code-fence'ы в обычном markdown
  обёрнуты `\\n\\n`, поэтому paragraph-break автоматически даёт границу
  раздела **и сохраняет fence/heading целыми внутри одной piece**.

**Почему heading-маркеры (`\\n## `) и code-fence (`\\n```\\n`) НЕ в списке**:
при split separator уходит вместе с границей (`OverlapCharSplitter._split_by_separator`
отбрасывает sep), то есть `## Title` потерял бы `## `, а из code-fence ушло
бы открывающее или закрывающее ` ``` `. Paragraph-break даёт ту же точку
резки **с сохранением** маркеров в чанке.

**Не даёт строгой защиты code-fence**: для очень больших code-блоков с
пустыми строками внутри fence, не влезающих в `chunk_size`, fallback на
`\\n` всё равно режет внутри (иначе пришлось бы выдавать чанк больше
`chunk_size`). Это редкий случай, для практических markdown-документов
данная стратегия покрывает большинство сценариев.
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import OverlapCharSplitter

__all__ = ["MarkdownAwareSplitter"]


class MarkdownAwareSplitter(OverlapCharSplitter):
    """
    Markdown-aware рекурсивный splitter: horizontal rules → paragraph → fallback.

    **Схема приоритета separator'ов**:
    ```
    1. "\\n***\\n"          ┐
    2. "\\n---\\n"          │  horizontal rules — границы разделов
    3. "\\n___\\n"          ┘
    4. "\\n\\n"             ← paragraph break (вокруг heading'ов и code-fence)
    5. "\\n"                ← line
    6. " "                  ← word
    7. ""                   ← char (последний fallback)
    ```

    Heading-маркеры (`# `, `## `) и code-fence (` ``` `) **намеренно не
    используются** как separator — при split они исчезают из вывода.
    Paragraph-break даёт ту же точку резки **с сохранением** маркеров в
    чанке.

    Контракт `Splitter[str]` не изменён: на выходе `Iterable[SplitPiece[str]]`
    с offset-tracking в исходном тексте; для соседних pieces одного уровня
    выполнено `value[start:end] == content`.

    **Когда применять**:
    - Markdown-документы с code-блоками, списками, таблицами, heading'ами.
    - Pipeline `MarkdownReader` или `HtmlMarkdownifyReader` → этот splitter.
    - Любая резка markdown'а где важна читабельность чанков (LLM на вход).

    **Когда НЕ нужно**:
    - Plain text без markdown-разметки — `OverlapCharSplitter` достаточен.
    - Очень короткий контент, влезающий в один chunk_size.

    **Пример** (показывает: code-fence целый, heading-маркеры сохранены,
    разделение по `---`):
    ```python
    splitter = MarkdownAwareSplitter(chunk_size=70, chunk_overlap=0)

    md = '''# Intro

    intro paragraph.

    ```python
    def f():
        return 1
    ```

    middle paragraph.

    ---

    bottom paragraph.'''
    # len(md) == 105

    list(splitter.split(md)) == [
        SplitPiece(
            content=(                                       # piece #0
                "# Intro\\n\\n"                              # heading marker сохранён
                "intro paragraph.\\n\\n"
                "```python\\ndef f():\\n    return 1\\n```"  # code-fence ЦЕЛЫЙ
            ),
            location=ChunkLocation(start=0, end=62),
        ),
        SplitPiece(                                          # piece #1
            content="middle paragraph.\\n",                  # отрезано по `\\n---\\n`
            location=ChunkLocation(start=64, end=82),        # offset в md
        ),
        SplitPiece(                                          # piece #2
            content="\\nbottom paragraph.",                  # после horizontal rule
            location=ChunkLocation(start=87, end=105),
        ),
    ]
    ```

    **Что показывает пример**:
    - Splitter сначала разрезал по `\\n---\\n` (horizontal rule) — это дало
      два больших куска ("до ---" и "после ---").
    - Большой кусок до `---` рекурсивно дорезался по `\\n\\n` —
      открывающие и закрывающие ` ``` ` остались в одной piece, потому что
      внутри code-fence нет `\\n\\n`.
    - Heading `# Intro` сохранил свой маркер `# ` (paragraph-break после
      него — естественная граница, маркер не теряется).
    """  # noqa: E501

    DEFAULT_SEPARATORS: ClassVar[tuple[str, ...]] = (
        "\n***\n",
        "\n---\n",
        "\n___\n",
        "\n\n",
        "\n",
        " ",
        "",
    )
