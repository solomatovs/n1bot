"""Семантический чанкинг документов — генераторный стриминг."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterator, List, Protocol, Union

import numpy as np

from langchain_core.documents import Document

from domain.embeddings import Embeddings

# ---------------------------------------------------------------------------
# Tiktoken encoder (офлайн-безопасно)
# ---------------------------------------------------------------------------

try:
    import tiktoken  # type: ignore

    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:

    class _ApproxEncoder:
        def encode(self, s: str) -> list[int]:
            return [0] * ((len(s.encode("utf-8")) + 3) // 4)

    _enc = _ApproxEncoder()
from domain.loading import ChunkingParams

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Markdown-парсинг и текстовые утилиты
# ---------------------------------------------------------------------------

_HEADING_PATTERN = re.compile(r"^(#+)\s+(.+)$")
_LIST_ITEM_PATTERN = re.compile(r"^[\d+\.\-\*\+]\s")
_LIST_SPLIT_PATTERN = re.compile(r"\n(?=[\d+\.\-\*\+]\s)")


def _is_heading(line: str) -> re.Match | None:
    """Проверить, является ли строка markdown-заголовком."""
    return _HEADING_PATTERN.match(line)


def _is_list_item(line: str) -> bool:
    return _LIST_ITEM_PATTERN.match(line) is not None


def _is_code_line(line: str) -> bool:
    return line.startswith("    ") or "```" in line


def _is_table_line(line: str) -> bool:
    return "|" in line and len(line.split("|")) > 2


def _split_paragraphs(text: str) -> List[str]:
    """Разделить текст на непустые параграфы по двойному переносу строки."""
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _split_list_items(text: str) -> List[str]:
    """Разделить текст на элементы markdown-списка."""
    return _LIST_SPLIT_PATTERN.split(text)


def _count_matching_lines(lines: List[str], predicate) -> int:  # noqa: ANN001
    return sum(1 for line in lines if predicate(line))


def _line_ratio(matching: int, total: int) -> float:
    return matching / max(1, total)


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Вычислить косинусное сходство между двумя векторами."""
    a = np.asarray(vec_a)
    b = np.asarray(vec_b)
    norm_product = np.linalg.norm(a) * np.linalg.norm(b)
    if norm_product == 0:
        return 0.0
    return float(np.dot(a, b) / norm_product)


# ---------------------------------------------------------------------------
# Внутренние события чанкера (без doc_index/doc_total/cumulative_chunks)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SectionProcessed:
    """Секция документа обработана чанкером."""
    section_index: int
    section_total: int
    section_title: str


@dataclass(frozen=True)
class ChunkCreated:
    """Чанк создан чанкером."""
    chunk: Document


ChunkerEvent = Union[SectionProcessed, ChunkCreated]


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

class ContentType(Enum):
    """Тип контента секции документа."""
    CODE = "code"
    TABLE = "table"
    LIST = "list"
    TEXT = "text"


@dataclass(frozen=True)
class Section:
    """Секция markdown-документа."""
    title: str
    level: int
    content: str


# ---------------------------------------------------------------------------
# Protocol — точка расширения для будущих стратегий чанкинга
# ---------------------------------------------------------------------------

class ChunkingStrategy(Protocol):
    """Стратегия разбиения документов на чанки (генераторная)."""

    def split_documents(self, docs: List[Document]) -> Iterator[ChunkerEvent]: ...


# ---------------------------------------------------------------------------
# MarkdownChunkingStrategy (она же AdvancedChunker)
# ---------------------------------------------------------------------------

class AdvancedChunker:
    """Семантический чанкер документов.

    Yield-based: каждая секция и каждый чанк — отдельное событие.
    Не знает про doc_index/doc_total — это ответственность вызывающего кода.
    """

    def __init__(self, embeddings: Embeddings, params: ChunkingParams) -> None:
        self._params = params
        self._embedding = embeddings
        self._tokenizer = _enc

    def split_documents(self, docs: List[Document]) -> Iterator[ChunkerEvent]:
        """Разбить документы на чанки, yielding события."""
        for doc in docs:
            yield from self._process_document(doc)

    # -- приватные методы ------------------------------------------------------

    def _process_document(self, doc: Document) -> Iterator[ChunkerEvent]:
        text = doc.page_content
        metadata = doc.metadata.copy()
        sections = _split_into_sections(text)
        total = len(sections)

        for idx, section in enumerate(sections, start=1):
            yield SectionProcessed(
                section_index=idx,
                section_total=total,
                section_title=section.title,
            )
            for chunk in self._chunk_section(section, metadata):
                yield ChunkCreated(chunk=chunk)

    def _chunk_section(self, section: Section, metadata: Dict) -> Iterator[Document]:
        content_type = _detect_content_type(section.content, self._params)
        match content_type:
            case ContentType.CODE:
                yield self._create_chunk([section.content], metadata, section, content_type)
            case ContentType.TABLE:
                yield self._create_chunk([section.content], metadata, section, content_type)
            case ContentType.LIST:
                yield from self._chunk_list(section.content, metadata, section)
            case ContentType.TEXT:
                yield from self._chunk_text(section.content, metadata, section)

    def _chunk_text(self, text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        """Семантический чанкинг текста с фоллбэком на параграфы."""
        try:
            yield from self._chunk_text_semantic(text, metadata, section)
        except (ConnectionError, ValueError, RuntimeError) as e:
            log.warning("Semantic chunking failed, falling back to paragraphs: %s", e)
            yield from self._chunk_by_paragraphs(text, metadata, section)

    def _chunk_text_semantic(self, text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        paragraphs = _split_paragraphs(text)
        if not paragraphs:
            return

        vecs = self._embedding.embed_documents(paragraphs)
        current_chunk: List[str] = []
        current_tokens = 0
        prev_vec = None

        for para, vec in zip(paragraphs, vecs):
            para_tokens = self._count_tokens(para)

            if self._should_merge_paragraph(prev_vec, vec, current_tokens, para_tokens):
                current_chunk.append(para)
                current_tokens += para_tokens
            else:
                if current_chunk:
                    yield self._create_chunk(current_chunk, metadata, section, ContentType.TEXT)
                current_chunk = [para]
                current_tokens = para_tokens
                prev_vec = vec

        if current_chunk:
            yield self._create_chunk(current_chunk, metadata, section, ContentType.TEXT)

    def _chunk_by_paragraphs(self, text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        paragraphs = _split_paragraphs(text)
        current_chunk: List[str] = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = self._count_tokens(para)
            if current_tokens + para_tokens > self._params.max_tokens and current_chunk:
                yield self._create_chunk(current_chunk, metadata, section, ContentType.TEXT)
                current_chunk = []
                current_tokens = 0
            current_chunk.append(para)
            current_tokens += para_tokens

        if current_chunk:
            yield self._create_chunk(current_chunk, metadata, section, ContentType.TEXT)

    def _chunk_list(self, list_text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        items = _split_list_items(list_text)
        current_chunk: List[str] = []
        current_tokens = 0

        for item in items:
            item_tokens = self._count_tokens(item)
            if current_tokens + item_tokens > self._params.max_tokens and current_chunk:
                yield self._create_chunk(current_chunk, metadata, section, ContentType.LIST)
                current_chunk = []
                current_tokens = 0
            current_chunk.append(item)
            current_tokens += item_tokens

        if current_chunk:
            yield self._create_chunk(current_chunk, metadata, section, ContentType.LIST)

    def _create_chunk(
        self, content_parts: List[str], metadata: Dict, section: Section, chunk_type: ContentType,
    ) -> Document:
        content = "\n\n".join(content_parts)
        chunk_metadata = {
            **metadata,
            "chunk_type": chunk_type.value,
            "section_title": section.title,
            "section_level": section.level,
            "token_count": self._count_tokens(content),
        }
        return Document(page_content=content, metadata=chunk_metadata)

    def _should_merge_paragraph(
        self,
        prev_vec: List[float] | None,
        current_vec: List[float],
        current_tokens: int,
        para_tokens: int,
    ) -> bool:
        """Проверить, нужно ли присоединить параграф к текущему чанку."""
        if prev_vec is None:
            return False
        is_similar = _cosine_similarity(current_vec, prev_vec) >= self._params.similarity_threshold
        fits_in_budget = (current_tokens + para_tokens) <= self._params.max_tokens
        return is_similar and fits_in_budget

    def _count_tokens(self, text: str) -> int:
        return len(self._tokenizer.encode(text))


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _split_into_sections(text: str) -> List[Section]:
    lines = text.split("\n")
    sections: List[Section] = []
    current_lines: List[str] = []
    current_title = "Без названия"
    current_level = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        heading_match = _is_heading(line)
        if heading_match:
            if current_lines:
                sections.append(Section(
                    title=current_title, level=current_level, content="\n".join(current_lines),
                ))
            current_level = len(heading_match.group(1))
            current_title = heading_match.group(2)
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append(Section(
            title=current_title, level=current_level, content="\n".join(current_lines),
        ))
    return sections


def _detect_content_type(text: str, params: ChunkingParams) -> ContentType:
    """Определить тип контента по доле характерных строк."""
    lines = text.split("\n")
    total = len(lines)

    if _line_ratio(_count_matching_lines(lines, _is_code_line), total) > params.code_ratio_threshold:
        return ContentType.CODE
    if _line_ratio(_count_matching_lines(lines, _is_table_line), total) > params.table_ratio_threshold:
        return ContentType.TABLE
    if _line_ratio(_count_matching_lines(lines, _is_list_item), total) > params.list_ratio_threshold:
        return ContentType.LIST
    return ContentType.TEXT
