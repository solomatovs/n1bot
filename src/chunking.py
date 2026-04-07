"""Семантический чанкинг документов — генераторный стриминг."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterator, List, Protocol, Union

from langchain_core.documents import Document

from config import enc
from embeddings import LiteLLMEmbeddings
from load_pipeline.events import ChunkProduced, SectionChunked
from ui.state import ChunkingParams
from utils import (
    cosine_similarity,
    count_matching_lines,
    is_code_line,
    is_heading,
    is_list_item,
    is_table_line,
    line_ratio,
    split_list_items,
    split_paragraphs,
)

log = logging.getLogger(__name__)

ChunkEvent = Union[SectionChunked, ChunkProduced]


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

    def split_documents(self, docs: List[Document]) -> Iterator[ChunkEvent]: ...


# ---------------------------------------------------------------------------
# MarkdownChunkingStrategy (она же AdvancedChunker)
# ---------------------------------------------------------------------------

class AdvancedChunker:
    """Семантический чанкер документов.

    Yield-based: каждая секция и каждый чанк — отдельное событие.
    """

    def __init__(self, embeddings: LiteLLMEmbeddings, params: ChunkingParams) -> None:
        self._params = params
        self._embedding = embeddings
        self._tokenizer = enc

    def split_documents(self, docs: List[Document]) -> Iterator[ChunkEvent]:
        """Разбить документы на чанки, yielding события."""
        for doc in docs:
            yield from self._process_document(doc)

    # -- приватные методы ------------------------------------------------------

    def _process_document(self, doc: Document) -> Iterator[ChunkEvent]:
        text = doc.page_content
        metadata = doc.metadata.copy()
        sections = _split_into_sections(text)
        total = len(sections)

        for idx, section in enumerate(sections, start=1):
            yield SectionChunked(
                doc_index=0,
                doc_total=0,
                section_index=idx,
                section_total=total,
                section_title=section.title,
            )
            for chunk in self._chunk_section(section, metadata):
                yield ChunkProduced(chunk=chunk, cumulative_chunks=0)

    def _chunk_section(self, section: Section, metadata: Dict) -> Iterator[Document]:
        content_type = _detect_content_type(section.content)
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
        paragraphs = split_paragraphs(text)
        if not paragraphs:
            return

        vecs = self._embedding.embed_documents(paragraphs)
        current_chunk: List[str] = []
        current_tokens = 0
        prev_vec = None

        for para, vec in zip(paragraphs, vecs):
            para_tokens = self._count_tokens(para)
            sim = cosine_similarity(vec, prev_vec) if prev_vec is not None else 1.0

            if (
                prev_vec is not None
                and sim >= self._params.similarity_threshold
                and (current_tokens + para_tokens) <= self._params.max_tokens
            ):
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
        paragraphs = split_paragraphs(text)
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
        items = split_list_items(list_text)
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
        heading_match = is_heading(line)
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


CODE_RATIO_THRESHOLD = 0.3
TABLE_RATIO_THRESHOLD = 0.3
LIST_RATIO_THRESHOLD = 0.4


def _detect_content_type(text: str) -> ContentType:
    """Определить тип контента по доле характерных строк."""
    lines = text.split("\n")
    total = len(lines)

    if line_ratio(count_matching_lines(lines, is_code_line), total) > CODE_RATIO_THRESHOLD:
        return ContentType.CODE
    if line_ratio(count_matching_lines(lines, is_table_line), total) > TABLE_RATIO_THRESHOLD:
        return ContentType.TABLE
    if line_ratio(count_matching_lines(lines, is_list_item), total) > LIST_RATIO_THRESHOLD:
        return ContentType.LIST
    return ContentType.TEXT
