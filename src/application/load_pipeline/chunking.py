"""Семантический чанкинг документов — генераторный стриминг."""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterator, List

import numpy as np

from langchain_core.documents import Document

from domain.chunking import (
    ChunkContentType,
    ChunkCreated,
    ChunkerEvent,
    Section,
    SectionProcessed,
)
from domain.embeddings import Embeddings
from domain.loading import ChunkingParams

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

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Markdown-парсинг и текстовые утилиты
# ---------------------------------------------------------------------------

_HEADING_PATTERN = re.compile(r"^(#+)\s+(.+)$")
_LIST_ITEM_PATTERN = re.compile(r"^[\d+\.\-\*\+]\s")
_LIST_SPLIT_PATTERN = re.compile(r"\n(?=[\d+\.\-\*\+]\s)")


def _is_heading(line: str) -> re.Match | None:
    return _HEADING_PATTERN.match(line)


def _is_list_item(line: str) -> bool:
    return _LIST_ITEM_PATTERN.match(line) is not None


def _is_code_line(line: str) -> bool:
    return line.startswith("    ") or "```" in line


def _is_table_line(line: str) -> bool:
    return "|" in line and len(line.split("|")) > 2


def _split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _split_list_items(text: str) -> List[str]:
    return _LIST_SPLIT_PATTERN.split(text)


def _count_matching_lines(lines: List[str], predicate) -> int:  # noqa: ANN001
    return sum(1 for line in lines if predicate(line))


def _line_ratio(matching: int, total: int) -> float:
    return matching / max(1, total)


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    a = np.asarray(vec_a)
    b = np.asarray(vec_b)
    norm_product = np.linalg.norm(a) * np.linalg.norm(b)
    if norm_product == 0:
        return 0.0
    return float(np.dot(a, b) / norm_product)


# ---------------------------------------------------------------------------
# AdvancedChunker — реализация ChunkingStrategy
# ---------------------------------------------------------------------------

class PassthroughChunker:
    """Пропускает документы без разбиения — каждый документ = один чанк."""

    def split_documents(self, docs: List[Document]) -> Iterator[ChunkerEvent]:
        for doc in docs:
            yield SectionProcessed(section_index=1, section_total=1, section_title="(без чанкинга)")
            yield ChunkCreated(chunk=doc)


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
        for doc in docs:
            yield from self._process_document(doc)

    def _process_document(self, doc: Document) -> Iterator[ChunkerEvent]:
        text = doc.page_content
        metadata = doc.metadata.copy()
        sections = _split_into_sections(text)
        total = len(sections)
        log.debug("Document has %d sections, %d chars", total, len(text))

        for idx, section in enumerate(sections, start=1):
            log.debug(
                "Section %d/%d '%s': %d chars, %d tokens",
                idx, total, section.title, len(section.content),
                self._count_tokens(section.content),
            )
            yield SectionProcessed(
                section_index=idx,
                section_total=total,
                section_title=section.title,
            )
            chunk_count = 0
            for chunk in self._chunk_section(section, metadata):
                chunk_count += 1
                yield ChunkCreated(chunk=chunk)
            log.debug("Section '%s' produced %d chunks", section.title, chunk_count)

    def _chunk_section(self, section: Section, metadata: Dict) -> Iterator[Document]:
        content_type = _detect_content_type(section.content, self._params)
        match content_type:
            case ChunkContentType.CODE:
                yield self._create_chunk([section.content], metadata, section, content_type)
            case ChunkContentType.TABLE:
                yield self._create_chunk([section.content], metadata, section, content_type)
            case ChunkContentType.LIST:
                yield from self._chunk_list(section.content, metadata, section)
            case ChunkContentType.TEXT:
                yield from self._chunk_text(section.content, metadata, section)

    def _chunk_text(self, text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        try:
            yield from self._chunk_text_semantic(text, metadata, section)
        except (ConnectionError, ValueError, RuntimeError) as e:
            log.warning("Semantic chunking failed, falling back to paragraphs: %s", e)
            yield from self._chunk_by_paragraphs(text, metadata, section)

    def _chunk_text_semantic(self, text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        paragraphs = _split_paragraphs(text)
        if not paragraphs:
            return

        # Если единственный параграф слишком длинный — фоллбэк на строки
        if len(paragraphs) == 1 and self._count_tokens(paragraphs[0]) > self._params.max_tokens:
            paragraphs = [line for line in text.split("\n") if line.strip()]

        log.debug("Semantic chunking: %d paragraphs", len(paragraphs))

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
                    yield self._create_chunk(current_chunk, metadata, section, ChunkContentType.TEXT)
                current_chunk = [para]
                current_tokens = para_tokens
                prev_vec = vec

        if current_chunk:
            yield self._create_chunk(current_chunk, metadata, section, ChunkContentType.TEXT)

    def _chunk_by_paragraphs(self, text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        paragraphs = _split_paragraphs(text)
        if not paragraphs:
            return

        # Если единственный параграф превышает max_tokens — дробим по строкам
        if len(paragraphs) == 1 and self._count_tokens(paragraphs[0]) > self._params.max_tokens:
            paragraphs = [line for line in text.split("\n") if line.strip()]

        yield from self._merge_into_chunks(paragraphs, metadata, section)

    def _merge_into_chunks(
        self, parts: List[str], metadata: Dict, section: Section,
    ) -> Iterator[Document]:
        """Объединять части в чанки, не превышая max_tokens."""
        current_chunk: List[str] = []
        current_tokens = 0

        for part in parts:
            part_tokens = self._count_tokens(part)
            if current_tokens + part_tokens > self._params.max_tokens and current_chunk:
                yield self._create_chunk(current_chunk, metadata, section, ChunkContentType.TEXT)
                current_chunk = []
                current_tokens = 0
            current_chunk.append(part)
            current_tokens += part_tokens

        if current_chunk:
            yield self._create_chunk(current_chunk, metadata, section, ChunkContentType.TEXT)

    def _chunk_list(self, list_text: str, metadata: Dict, section: Section) -> Iterator[Document]:
        items = _split_list_items(list_text)
        current_chunk: List[str] = []
        current_tokens = 0

        for item in items:
            item_tokens = self._count_tokens(item)
            if current_tokens + item_tokens > self._params.max_tokens and current_chunk:
                yield self._create_chunk(current_chunk, metadata, section, ChunkContentType.LIST)
                current_chunk = []
                current_tokens = 0
            current_chunk.append(item)
            current_tokens += item_tokens

        if current_chunk:
            yield self._create_chunk(current_chunk, metadata, section, ChunkContentType.LIST)

    def _create_chunk(
        self, content_parts: List[str], metadata: Dict, section: Section, chunk_type: ChunkContentType,
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


def _detect_content_type(text: str, params: ChunkingParams) -> ChunkContentType:
    lines = text.split("\n")
    total = len(lines)

    if _line_ratio(_count_matching_lines(lines, _is_code_line), total) > params.code_ratio_threshold:
        return ChunkContentType.CODE
    if _line_ratio(_count_matching_lines(lines, _is_table_line), total) > params.table_ratio_threshold:
        return ChunkContentType.TABLE
    if _line_ratio(_count_matching_lines(lines, _is_list_item), total) > params.list_ratio_threshold:
        return ChunkContentType.LIST
    return ChunkContentType.TEXT
