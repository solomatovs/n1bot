"""Production-grade авто-чанкование HTML файлов по заголовкам в `Chunk[str]`.

Pipeline:

1. **Boilerplate strip**: вырезаем `<script>`, `<style>`, `<nav>`, `<footer>`,
   `<aside>`, `<header>` до парсинга секций — они шумят в embedding-
   пространстве и в FTS (особенно nav-меню с десятками ссылок).
2. **Canonical URL extraction**: ищем `<link rel="canonical">`,
   `<meta property="og:url">`, `<meta name="canonical">`. Если нашли —
   пишется в `source_url` каждого чанка → kb_search строит deep-link.
3. **Section parsing**: оставшийся HTML парсится через `HtmlSectionParser`
   (lxml-based, line-precision offsets).
4. **Group by headings**: секции группируются на границах заголовков
   `split_levels` (по умолчанию H1+H2). Sub-headings (не из split_levels)
   сохраняются внутри body как markdown-prefix `## Title` для иерархии.
5. **Heading path**: `heading_path = "H1 > H2 > H3"` — для каждого чанка
   полный путь, чтобы LLM видел контекст («Установка > Из репозитория»
   vs «Запуск > Из репозитория»). Path обновляется при каждом
   разделителе по правилам heading-level stack'а.
6. **Structured rendering**: paragraph → текст, code → fenced markdown
   с языком (`<code class="language-X">`), list → `- item\\n- item`,
   table → markdown с `|`, blockquote → `> текст`.
7. **Size cap**: если `format_content` превышает `max_chunk_chars`,
   чанк разбивается на под-чанки по границам block-уровня (paragraph/
   code/list/table). Sub-chunks наследуют metadata разделителя,
   chunk_index монотонно растёт.
8. **Min chunk filter**: чанки короче `min_chunk_chars` пропускаются —
   защита от пустых разделов с одним заголовком.

Метаданные чанка:
- `reader.doc_type = "html"`
- `reader.page_title` = текст первого H1 (fallback: `<title>` если есть)
- `reader.heading_path` = полный путь до разделителя через ` > `
- `anchor` / `chunk.anchor` = `id`-атрибут разделителя
- `source_url` = canonical URL документа (если есть)
- `chunk.location.start/end` = offset'ы разделителя в исходном документе
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, cast

from lxml import html as lxml_html

from boba.html import HtmlSectionParser
from boba.html.sections import (
    HtmlBlockquoteSection,
    HtmlCodeBlockSection,
    HtmlListSection,
    HtmlParagraphSection,
    HtmlTableSection,
)
from boba.indexing.chunks import Chunk, ChunkKeys, chunk_id_from_digest
from boba.indexing.key_encoder import KeyEncoder, Sha256TextEncoder
from boba.indexing.metadata import Metadata, MetadataKey, ReaderKeys
from boba.indexing.sections import (
    HeadingSection,
    Section,
    SectionKeys,
    SourceId,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HEADING_PATH_KEY",
    "SOURCE_URL_KEY",
    "HtmlChunkParser",
    "ParsedHtmlChunk",
]


HEADING_PATH_KEY: MetadataKey[str] = MetadataKey(
    name="reader.heading_path",
    decode=str,
    encode=str,
)
"""Полный путь heading'ов до чанка-разделителя через ` > `.

Не входит в `ChunkKeys` / `ReaderKeys` стандартные классы — это
postgres-плагин-специфичный ключ для read-side обогащения (LLM получает
иерархический контекст в metadata, kb_search его не использует напрямую,
но возвращает как часть `metadata` в hits).
"""

SOURCE_URL_KEY: MetadataKey[str] = MetadataKey(
    name="source_url",
    decode=str,
    encode=str,
)
"""`source_url` — каноничный URL документа, общий для всех чанков.

`kb_search` использует его для построения deep-link'а вместе с `anchor`.
Не путать с `reader.canonical` (стандартный ключ из ReaderKeys) — это
прямое плоское поле для backward-совместимости с md_chunk-форматом.
"""

# Тэги, которые тотально вырезаем перед парсингом секций. nav/footer/
# aside/header содержат меню/копирайты/сайдбары; script/style — JS+CSS
# (которые при text_content попадают как мусор в embedding); noscript —
# редко содержит полезный текст.
_BOILERPLATE_TAGS: frozenset[str] = frozenset(
    {"script", "style", "nav", "footer", "aside", "header", "noscript"},
)

# Регекс для нормализации whitespace в plain-text рендере. Pre-блоки
# обрабатываются отдельно (newlines сохраняются как часть кода).
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedHtmlChunk:
    """Одна группа секций между двумя «разделителями» (heading'ами split-уровня).

    `divider` — заголовок-разделитель.
    `body_sections` — всё между этим разделителем и следующим.
    `heading_path` — `H1 > ... > divider`, накопленный из стека при walk'е.
    """

    divider: HeadingSection
    body_sections: tuple[Section[str], ...]
    heading_path: tuple[str, ...] = field(default_factory=tuple)


class HtmlChunkParser:
    """Production-grade парсер `.html` / `.htm` в `Iterable[Chunk[str]]`.

    Реализует протокол `ChunkParser`.
    """

    extensions: ClassVar[tuple[str, ...]] = (".html", ".htm")

    _CHUNK_ID_PREFIX_LENGTH: ClassVar[int] = 16

    def __init__(  # noqa: PLR0913 — legitimate config knobs, kw-only
        self,
        *,
        split_levels: frozenset[int] = frozenset({1, 2}),
        min_chunk_chars: int = 0,
        max_chunk_chars: int = 0,
        extract_canonical_url: bool = True,
        encoder: KeyEncoder[str] | None = None,
        html_parser: HtmlSectionParser | None = None,
    ) -> None:
        if not split_levels:
            msg = "HtmlChunkParser: split_levels must be non-empty"
            raise ValueError(msg)
        if min_chunk_chars < 0:
            msg = "HtmlChunkParser: min_chunk_chars must be >= 0"
            raise ValueError(msg)
        if max_chunk_chars < 0:
            msg = "HtmlChunkParser: max_chunk_chars must be >= 0"
            raise ValueError(msg)
        self._split_levels = split_levels
        self._min_chunk_chars = min_chunk_chars
        self._max_chunk_chars = max_chunk_chars
        self._extract_canonical_url = extract_canonical_url
        self._encoder = encoder if encoder is not None else Sha256TextEncoder()
        self._html_parser = (
            html_parser if html_parser is not None else HtmlSectionParser()
        )

    def build_chunks_from_text(
        self,
        text: str,
        *,
        source_id: SourceId,
    ) -> Iterator[Chunk[str]]:
        """Парсит HTML и yield'ит чанки.

        Преамбула до первого разделителя пропускается (типичный wiki
        начинается с H1; контент до него — редкий случай, не покрывается).
        """
        if not text.strip():
            return

        source_url = (
            self._extract_canonical(text) if self._extract_canonical_url else ""
        )
        clean_html = self._strip_boilerplate(text)
        sections: list[Section[str]] = list(
            self._html_parser.parse(
                clean_html,
                source_id=SourceId(""),
                base_metadata=Metadata.empty(),
            ),
        )
        if not sections:
            return

        page_title = self._extract_page_title(sections, fallback_text=text)
        groups = self._group_by_headings(sections)
        source_digest = self._encoder.encode(str(source_id)).to_wire()

        # chunk_index монотонно растёт по всему файлу (включая sub-chunks
        # от size-cap split'а), чтобы chunk_id'ы оставались уникальными.
        chunk_index = 0
        for group in groups:
            heading_text = group.divider.text.strip()
            body_text = self._render_body(group.body_sections).strip()
            sub_chunks = self._size_split(heading_text, body_text)
            for body_part in sub_chunks:
                format_content = self._format_content(heading_text, body_part)
                if (
                    self._min_chunk_chars > 0
                    and len(format_content) < self._min_chunk_chars
                ):
                    logger.debug(
                        "skipping chunk under min_chunk_chars (%d < %d) "
                        "in %s heading=%r",
                        len(format_content),
                        self._min_chunk_chars,
                        source_id,
                        heading_text,
                    )
                    continue
                content_hash = self._encoder.encode(format_content)
                chunk_id = chunk_id_from_digest(
                    source_digest,
                    chunk_index,
                    self._CHUNK_ID_PREFIX_LENGTH,
                )
                yield Chunk(
                    chunk_id=chunk_id,
                    source_id=source_id,
                    format_content=format_content,
                    raw_content=format_content,
                    chunk_index=chunk_index,
                    content_hash=content_hash,
                    metadata=self._build_metadata(
                        page_title=page_title,
                        heading_path=group.heading_path,
                        divider=group.divider,
                        source_url=source_url,
                    ),
                    tags=frozenset(),
                )
                chunk_index += 1

    def build_chunks_from_file(
        self,
        file_path: Path,
        *,
        root: Path,
    ) -> Iterator[Chunk[str]]:
        text = file_path.read_text(encoding="utf-8")
        rel = file_path.resolve().relative_to(root.resolve()).as_posix()
        yield from self.build_chunks_from_text(text, source_id=SourceId(rel))

    # ------------------------------------------------------------------ #
    # Pre-parse: boilerplate strip + canonical URL                       #
    # ------------------------------------------------------------------ #

    @classmethod
    def _strip_boilerplate(cls, html_text: str) -> str:
        """Вырезать boilerplate-тэги (script/style/nav/footer/...).

        Парсит документ через `lxml.html.fromstring`, удаляет вхождения
        тегов из `_BOILERPLATE_TAGS`, возвращает сериализованный HTML.
        При ошибке парсинга — отдаёт исходный текст (best-effort).
        """
        try:
            root = lxml_html.fromstring(html_text)
        except Exception:
            return html_text
        for tag in _BOILERPLATE_TAGS:
            for el in root.iter(tag):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
        # `encoding="unicode"` гарантирует возврат str (lxml-stubs объявляют
        # `str | bytes` без unicode-narrow'а — cast'им явно).
        return cast(str, lxml_html.tostring(root, encoding="unicode"))

    @staticmethod
    def _extract_canonical(html_text: str) -> str:
        """Достать URL документа из `<link rel="canonical">` / OG-меты.

        Порядок:
          1. `<link rel="canonical" href="...">`
          2. `<meta property="og:url" content="...">`
          3. `<meta name="canonical" content="...">`

        Возвращает пустую строку, если ничего не нашлось.
        """
        try:
            root = lxml_html.fromstring(html_text)
        except Exception:
            return ""
        link = root.find('.//link[@rel="canonical"]')
        if link is not None:
            href = link.get("href") or ""
            if href:
                return href.strip()
        og = root.find('.//meta[@property="og:url"]')
        if og is not None:
            content = og.get("content") or ""
            if content:
                return content.strip()
        meta_canonical = root.find('.//meta[@name="canonical"]')
        if meta_canonical is not None:
            content = meta_canonical.get("content") or ""
            if content:
                return content.strip()
        return ""

    # ------------------------------------------------------------------ #
    # Section grouping + heading path                                    #
    # ------------------------------------------------------------------ #

    def _group_by_headings(
        self, sections: Sequence[Section[str]],
    ) -> list[ParsedHtmlChunk]:
        """Сгруппировать секции, накопив heading-path для каждой группы.

        Path-стек: ключ = уровень H, значение = текст. При встрече
        нового heading'а сбрасываем все ключи стека с level >= нового.
        Это соответствует стандартной семантике HTML-аутлайна.
        """
        groups: list[ParsedHtmlChunk] = []
        # Стек: словарь level → text, упорядоченный по level ASC при
        # рендере heading_path.
        path_stack: dict[int, str] = {}
        current_divider: HeadingSection | None = None
        current_body: list[Section[str]] = []
        current_path: tuple[str, ...] = ()

        for s in sections:
            if isinstance(s, HeadingSection):
                # Обновляем стек ДО решения о cut: новый heading вытесняет
                # все более глубокие уровни (или равный — это новый брат).
                self._update_path_stack(path_stack, s.level, s.text.strip())
                if s.level in self._split_levels:
                    # Cut: закрываем текущую группу, открываем новую.
                    if current_divider is not None:
                        groups.append(
                            ParsedHtmlChunk(
                                divider=current_divider,
                                body_sections=tuple(current_body),
                                heading_path=current_path,
                            ),
                        )
                    current_divider = s
                    current_body = []
                    current_path = self._render_path(path_stack)
                elif current_divider is not None:
                    # Sub-heading: остаётся внутри текущего чанка.
                    current_body.append(s)
            elif current_divider is not None:
                current_body.append(s)
            # Иначе — preamble до первого разделителя, пропускаем.

        if current_divider is not None:
            groups.append(
                ParsedHtmlChunk(
                    divider=current_divider,
                    body_sections=tuple(current_body),
                    heading_path=current_path,
                ),
            )
        return groups

    @staticmethod
    def _update_path_stack(
        stack: dict[int, str], level: int, text: str,
    ) -> None:
        """Затиргерить уровни >= level и записать новое значение."""
        for lvl in list(stack):
            if lvl >= level:
                del stack[lvl]
        stack[level] = text

    @staticmethod
    def _render_path(stack: dict[int, str]) -> tuple[str, ...]:
        """Path в порядке возрастания уровня (H1 > H2 > H3)."""
        return tuple(stack[lvl] for lvl in sorted(stack))

    # ------------------------------------------------------------------ #
    # Body rendering: code/lists/tables/blockquote/paragraph             #
    # ------------------------------------------------------------------ #

    @classmethod
    def _render_body(
        cls, body_sections: Sequence[Section[str]],
    ) -> str:
        """Тело чанка с сохранением структуры block-уровня.

        Каждая секция превращается в одну «логическую» часть:
        - paragraph → text
        - code → ```lang\\ncode\\n```
        - list → `- item\\n- item`
        - table → markdown table (или text-rows fallback)
        - blockquote → `> текст`
        - sub-heading → `## Title`
        """
        parts: list[str] = []
        for s in body_sections:
            rendered = cls._render_section(s)
            if rendered:
                parts.append(rendered)
        return "\n\n".join(parts)

    @classmethod
    def _render_section(cls, s: Section[str]) -> str:  # noqa: PLR0911 — type dispatch
        if isinstance(s, HeadingSection):
            level = min(s.level, 6)
            return f"{'#' * level} {s.text.strip()}"
        content = str(s.content)
        if isinstance(s, HtmlCodeBlockSection):
            return cls._render_code(content)
        if isinstance(s, HtmlListSection):
            return cls._render_list(content)
        if isinstance(s, HtmlTableSection):
            return cls._render_table(content)
        if isinstance(s, HtmlBlockquoteSection):
            return cls._render_blockquote(content)
        if isinstance(s, HtmlParagraphSection):
            return cls._to_plain_text(content)
        # HtmlHorizontalRuleSection и неизвестные подклассы — fallback в
        # plain text. HR без структурного смысла мы рендерим в `---`.
        text = cls._to_plain_text(content)
        return text or ""

    @classmethod
    def _render_code(cls, html_fragment: str) -> str:
        """`<pre><code class="language-X">code</code></pre>` → ```X\\ncode\\n```.

        Сохраняет переводы строк (в отличие от плоского text_content).
        Язык извлекается из `class="language-X"` / `class="lang-X"` на
        `<code>` или `<pre>`.
        """
        root = cls._fragment_root(html_fragment)
        if root is None:
            return html_fragment.strip()
        # Ищем code-элемент; если его нет (pre без code) — берём pre.
        code_el = root.find(".//code")
        target = code_el if code_el is not None else root.find(".//pre")
        if target is None:
            target = root
        lang = cls._extract_code_lang(target)
        text = target.text_content().rstrip("\n")
        # Pre сохраняет внутренние \n. Дополнительно убираем общий indent
        # если он есть (например, HTML pretty-print добавил 4 пробела).
        return f"```{lang}\n{text}\n```"

    @staticmethod
    def _extract_code_lang(el: lxml_html.HtmlElement) -> str:
        cls_attr = el.get("class") or ""
        for token in cls_attr.split():
            for prefix in ("language-", "lang-"):
                if token.startswith(prefix):
                    return token[len(prefix):]
        return ""

    @classmethod
    def _render_list(cls, html_fragment: str) -> str:
        """`<ul>/<ol>` → `- item\\n- item` (нумерация ol игнорируется)."""
        root = cls._fragment_root(html_fragment)
        if root is None:
            return cls._to_plain_text(html_fragment)
        items = root.findall(".//li")
        if not items:
            return cls._to_plain_text(html_fragment)
        lines = [
            f"- {cls._normalize_text(li.text_content())}"
            for li in items
            if li.text_content().strip()
        ]
        return "\n".join(lines)

    @classmethod
    def _render_table(cls, html_fragment: str) -> str:
        """Markdown-таблица; header-row из `<th>` если есть, иначе первая `<tr>`."""
        root = cls._fragment_root(html_fragment)
        if root is None:
            return cls._to_plain_text(html_fragment)
        rows: list[list[str]] = []
        for tr in root.findall(".//tr"):
            cells = [
                cls._normalize_text(c.text_content())
                for c in tr.findall("./th") + tr.findall("./td")
            ]
            if cells:
                rows.append(cells)
        if not rows:
            return cls._to_plain_text(html_fragment)
        # Header: первая строка, особенно если `<th>` присутствовал.
        header = rows[0]
        body = rows[1:]
        col_count = max(len(r) for r in rows)
        header = header + [""] * (col_count - len(header))
        lines = [
            "| " + " | ".join(header) + " |",
            "|" + "|".join(["---"] * col_count) + "|",
        ]
        for row in body:
            padded = row + [""] * (col_count - len(row))
            lines.append("| " + " | ".join(padded) + " |")
        return "\n".join(lines)

    @classmethod
    def _render_blockquote(cls, html_fragment: str) -> str:
        text = cls._to_plain_text(html_fragment)
        return "\n".join(f"> {line}" for line in text.splitlines()) or f"> {text}"

    @classmethod
    def _to_plain_text(cls, html_fragment: str) -> str:
        root = cls._fragment_root(html_fragment)
        if root is None:
            return html_fragment.strip()
        return cls._normalize_text(root.text_content())

    @staticmethod
    def _normalize_text(text: str) -> str:
        return _WHITESPACE_RE.sub(" ", text).strip()

    @staticmethod
    def _fragment_root(html_fragment: str) -> lxml_html.HtmlElement | None:
        fragment = html_fragment.strip()
        if not fragment:
            return None
        try:
            # `fragment_fromstring` с create_parent="div" обёрнёт любой
            # кусок в один root, неважно сколько корневых элементов.
            return lxml_html.fragment_fromstring(
                fragment,
                create_parent="div",  # pyright: ignore[reportArgumentType]
            )
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Size cap: разбивка слишком длинного body                           #
    # ------------------------------------------------------------------ #

    def _size_split(self, heading_text: str, body_text: str) -> list[str]:
        """Разбить body на куски ≤ `max_chunk_chars` по \\n\\n границам.

        Heading включается в каждый под-чанк (`_format_content` его
        префиксирует). Если `max_chunk_chars == 0` — без разбивки,
        один body.
        """
        if self._max_chunk_chars <= 0:
            return [body_text]
        # Бюджет на body: вычитаем длину заголовка + разделители.
        heading_prefix = f"# {heading_text}\n\n" if heading_text else ""
        budget = self._max_chunk_chars - len(heading_prefix)
        if budget <= 0 or len(body_text) <= budget:
            return [body_text]
        # Разбиваем по `\n\n` (`_render_body` ставит их между секциями).
        blocks = body_text.split("\n\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for block in blocks:
            # `+2` за разделитель `\n\n` который вернётся при join.
            extra = len(block) + (2 if current else 0)
            if current and current_len + extra > budget:
                chunks.append("\n\n".join(current))
                current = [block]
                current_len = len(block)
            else:
                current.append(block)
                current_len += extra
        if current:
            chunks.append("\n\n".join(current))
        return chunks or [body_text]

    # ------------------------------------------------------------------ #
    # Metadata + formatting                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_content(heading_text: str, body_text: str) -> str:
        if heading_text and body_text:
            return f"# {heading_text}\n\n{body_text}"
        if heading_text:
            return f"# {heading_text}"
        return body_text

    @classmethod
    def _extract_page_title(
        cls,
        sections: Sequence[Section[str]],
        *,
        fallback_text: str,
    ) -> str:
        """Первый H1 → fallback на `<title>` из исходного HTML."""
        for s in sections:
            if isinstance(s, HeadingSection) and s.level == 1:
                return s.text.strip()
        # Fallback: <title> в исходном HTML (даже после boilerplate strip
        # head обычно сохраняется — мы не вырезаем `<head>` или `<title>`).
        try:
            root = lxml_html.fromstring(fallback_text)
        except Exception:
            return ""
        title_el = root.find(".//title")
        if title_el is not None and title_el.text:
            return title_el.text.strip()
        return ""

    @staticmethod
    def _build_metadata(
        *,
        page_title: str,
        heading_path: tuple[str, ...],
        divider: HeadingSection,
        source_url: str,
    ) -> Metadata:
        wire: dict[str, str] = {
            ReaderKeys.DOC_TYPE.name: "html",
        }
        if page_title:
            wire[ReaderKeys.PAGE_TITLE.name] = page_title
        if heading_path:
            wire[HEADING_PATH_KEY.name] = " > ".join(heading_path)
        anchor = divider.metadata.get(SectionKeys.ANCHOR)
        if anchor:
            wire["anchor"] = anchor
            wire[ChunkKeys.ANCHOR.name] = anchor
        if source_url:
            wire[SOURCE_URL_KEY.name] = source_url
        start = divider.metadata.get(SectionKeys.LOCATION_START)
        if start is not None:
            wire[ChunkKeys.LOCATION_START.name] = str(start)
        end = divider.metadata.get(SectionKeys.LOCATION_END)
        if end is not None:
            wire[ChunkKeys.LOCATION_END.name] = str(end)
        return Metadata.from_wire(wire)
