"""
Parsing pre-chunked .md files into `Chunk[str]`.

Каждый .md файл — это **один** заранее подготовленный оператором чанк.
Сплиттинга нет. Тело файла идёт в embedding как есть (с префиксом заголовка).

Формат файла (H1 + структурные блоки):

    # Заголовок чанка

    **tags:** tag1, tag2, tag3
    **source:** https://wiki.example.com/page
    **anchor:** optional-anchor-id

    ---

    Тело чанка — это и есть текст, который будет проиндексирован.

    Любой валидный markdown: параграфы, code-блоки, списки, таблицы.

Правила:

- `# Заголовок` обязателен — первая Section потока должна быть
  `HeadingSection(level=1)`. Структурный markdown-парсинг — через
  `boba.markdown.MarkdownSectionParser` (markdown-it-py).
- Блок метаданных (Section'ы между H1 и `MarkdownHorizontalRuleSection`)
  опционален. Извлекается из inline-`strong` markdown-узлов:
  `**key:** value` → key/value пары.
- Узнаваемые ключи: `tags`, `source`, `anchor`. Остальные кладутся в
  metadata под префиксом `md.{key}`.
- `tags` парсятся как comma-separated, оборачиваются в `Chunk.tags`.
- `source` пишется в metadata как `source_url` (без префикса) — kb_search
  использует его для построения deep-link'ов.
- `anchor` пишется и как `anchor` (для kb_search), и как `chunk.anchor`
  (для типизированного read-back через ChunkKeys).
- Если `---` отсутствует, тело = всё после H1, метаданных нет.

NB: модуль backend-agnostic (работает на сырых `MarkdownSectionParser` +
`KeyEncoder` без какой-либо postgres-завязки). В будущем стоит вынести
в общий пакет (boba-md-chunk), сейчас живёт здесь ради независимости
плагина.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkKeys
from boba.indexing.key_encoder import KeyEncoder, Sha256TextEncoder
from boba.indexing.metadata import Metadata, MetadataKey, ReaderKeys
from boba.indexing.sections import (
    HeadingSection,
    Section,
    SectionKeys,
    SourceId,
)
from boba.markdown import MarkdownSectionParser
from boba.markdown.sections import MarkdownHorizontalRuleSection

__all__ = [
    "MdChunkParser",
    "ParsedMdChunk",
]


@dataclass(frozen=True)
class ParsedMdChunk:
    """Результат разбора одного .md файла-чанка."""

    title: str
    body: str
    tags: frozenset[str]
    metadata: dict[str, str]


class MdChunkParser:
    """Парсер pre-chunked `.md` файлов в `Chunk[str]`.

    Реализует протокол `ChunkParser` — выдаёт ровно один чанк на файл
    (формат «один .md = один чанк» с обязательным `# H1` + опциональным
    блоком метаданных до `---`).
    """

    extensions: ClassVar[tuple[str, ...]] = (".md",)

    _INLINE_META_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"\*\*(?P<key>[\w][\w.-]*?)\s*:\s*\*\*\s*(?P<value>[^\n]+)",
    )

    _RECOGNIZED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"tags", "source", "anchor"},
    )
    _CHUNK_ID_PREFIX_LENGTH: ClassVar[int] = 16

    def __init__(
        self,
        encoder: KeyEncoder[str] | None = None,
        markdown_parser: MarkdownSectionParser | None = None,
    ) -> None:
        self._encoder = encoder if encoder is not None else Sha256TextEncoder()
        self._md_parser = (
            markdown_parser
            if markdown_parser is not None
            else MarkdownSectionParser()
        )

    def parse(self, text: str) -> ParsedMdChunk:
        if not text.strip():
            msg = "file is empty"
            raise ValueError(msg)

        sections: list[Section[str]] = list(
            self._md_parser.parse(
                text,
                source_id=SourceId(""),
                base_metadata=Metadata.empty(),
            ),
        )
        if not sections:
            msg = "no markdown sections parsed"
            raise ValueError(msg)

        title = self._extract_title(sections)
        hr_index = self._find_hr_section(sections)
        if hr_index is None:
            return ParsedMdChunk(
                title=title,
                body=self._slice_body_after_section(text, sections[0]),
                tags=frozenset(),
                metadata={},
            )

        tags, metadata = self._parse_metadata_sections(
            sections[1:hr_index],
        )
        return ParsedMdChunk(
            title=title,
            body=self._slice_body_after_section(text, sections[hr_index]),
            tags=tags,
            metadata=metadata,
        )

    def build_chunk_from_text(
        self,
        text: str,
        *,
        source_id: SourceId,
    ) -> Chunk[str]:
        parsed = self.parse(text)
        format_content = f"# {parsed.title}\n\n{parsed.body}".strip()
        content_hash = self._encoder.encode(format_content)
        source_digest = self._encoder.encode(str(source_id)).to_wire()
        chunk_id = ChunkId(
            f"{source_digest[: self._CHUNK_ID_PREFIX_LENGTH]}:0",
        )
        return Chunk(
            chunk_id=chunk_id,
            source_id=source_id,
            format_content=format_content,
            raw_content=text,
            chunk_index=0,
            content_hash=content_hash,
            metadata=self._build_metadata(parsed),
            tags=parsed.tags,
        )

    def build_chunk_from_file(
        self,
        file_path: Path,
        *,
        root: Path,
    ) -> Chunk[str]:
        """Один чанк из одного файла (legacy, отдельная не-iterable форма).

        Используется кодом, который ожидает строгий `Chunk[str]`. Новые
        вызовы через `FolderIndexer` идут через `build_chunks_from_file`
        (yields один и тот же чанк).
        """
        text = file_path.read_text(encoding="utf-8")
        rel = file_path.resolve().relative_to(root.resolve()).as_posix()
        return self.build_chunk_from_text(text, source_id=SourceId(rel))

    def build_chunks_from_file(
        self,
        file_path: Path,
        *,
        root: Path,
    ) -> Iterator[Chunk[str]]:
        """Реализация `ChunkParser.build_chunks_from_file` — yields единственный чанк."""
        yield self.build_chunk_from_file(file_path, root=root)

    @staticmethod
    def _extract_title(sections: Sequence[Section[str]]) -> str:
        h1 = sections[0]
        if not isinstance(h1, HeadingSection) or h1.level != 1:
            msg = (
                "first markdown section must be H1 ('# Title'); "
                f"got {type(h1).__name__} "
                f"level={getattr(h1, 'level', None)!r}"
            )
            raise ValueError(msg)
        title = h1.text.strip()
        if not title:
            msg = "H1 title is empty"
            raise ValueError(msg)
        return title

    @staticmethod
    def _find_hr_section(sections: Sequence[Section[str]]) -> int | None:
        for i, s in enumerate(sections):
            if isinstance(s, MarkdownHorizontalRuleSection):
                return i
        return None

    @staticmethod
    def _slice_body_after_section(text: str, section: Section[str]) -> str:
        end = section.metadata.get(SectionKeys.LOCATION_END)
        if end is None:
            return ""
        return text[end:].strip()

    @classmethod
    def _parse_metadata_sections(
        cls,
        sections: Sequence[Section[str]],
    ) -> tuple[frozenset[str], dict[str, str]]:
        tags: frozenset[str] = frozenset()
        metadata: dict[str, str] = {}
        for s in sections:
            for m in cls._INLINE_META_RE.finditer(str(s.content)):
                key = m.group("key").lower()
                value = m.group("value").strip()
                if key == "tags":
                    tags = frozenset(
                        t.strip() for t in value.split(",") if t.strip()
                    )
                else:
                    metadata[key] = value
        return tags, metadata

    @classmethod
    def _build_metadata(cls, parsed: ParsedMdChunk) -> Metadata:
        wire: dict[str, str] = {
            ReaderKeys.DOC_TYPE.name: "markdown",
            ReaderKeys.PAGE_TITLE.name: parsed.title,
        }
        source = parsed.metadata.get("source")
        if source:
            wire["source_url"] = source
        anchor = parsed.metadata.get("anchor")
        if anchor:
            wire["anchor"] = anchor
            wire[ChunkKeys.ANCHOR.name] = anchor
        for key, value in parsed.metadata.items():
            if key in cls._RECOGNIZED_KEYS:
                continue
            custom_key = MetadataKey(
                name=f"md.{key}", decode=str, encode=str,
            )
            wire[custom_key.name] = value
        return Metadata.from_wire(wire)
