from __future__ import annotations

import logging
import re
from typing import Dict, List

import numpy as np
from langchain_core.documents import Document

from config import EMBEDDING_MODEL, enc
from embeddings import LiteLLMEmbeddings
from ui.state import AppConfig, ChunkingParams

log = logging.getLogger(__name__)


class AdvancedChunker:
    """Семантический чанкер документов.

    Принимает конфигурацию приложения (для эмбеддингов) и параметры чанкинга.
    """

    def __init__(self, cfg: AppConfig, params: ChunkingParams) -> None:
        self._params = params
        self._embedding = LiteLLMEmbeddings(
            model=EMBEDDING_MODEL,
            base_url=cfg.litellm_url.replace("/v1", "").rstrip("/"),
            api_key=cfg.litellm_api_key,
        )
        self._tokenizer = enc

    def split_documents(self, docs: List[Document]) -> List[Document]:
        """Разбить список документов на чанки."""
        all_chunks: List[Document] = []
        for doc in docs:
            chunks = self._process_document(doc)
            all_chunks.extend(chunks)
        return all_chunks

    # -- приватные методы ------------------------------------------------------

    def _process_document(self, doc: Document) -> List[Document]:
        text = doc.page_content
        metadata = doc.metadata.copy()
        sections = _split_into_sections(text)
        chunks: List[Document] = []
        for section in sections:
            section_chunks = self._chunk_section(section, metadata)
            chunks.extend(section_chunks)
        return chunks

    def _chunk_section(self, section: Dict, metadata: Dict) -> List[Document]:
        content = section["content"]
        content_type = _detect_content_type(content)
        match content_type:
            case "code":
                return [self._create_chunk([content], metadata, section, "code")]
            case "table":
                return [self._create_chunk([content], metadata, section, "table")]
            case "list":
                return self._chunk_list(content, metadata, section)
            case _:
                return self._chunk_text(content, metadata, section)

    def _chunk_text(self, text: str, metadata: Dict, section: Dict) -> List[Document]:
        """Семантический чанкинг текста с фоллбэком на параграфы."""
        try:
            return self._chunk_text_semantic(text, metadata, section)
        except (ConnectionError, ValueError, RuntimeError) as e:
            log.warning("Семантический чанкинг не удался, фоллбэк на параграфы: %s", e)
            return self._chunk_by_paragraphs(text, metadata, section)

    def _chunk_text_semantic(self, text: str, metadata: Dict, section: Dict) -> List[Document]:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        if not paragraphs:
            return []

        vecs = self._embedding.embed_documents(paragraphs)
        chunks: List[Document] = []
        current_chunk: List[str] = []
        current_tokens = 0
        prev_vec = None

        for para, vec in zip(paragraphs, vecs):
            para_tokens = self._count_tokens(para)
            sim = 1.0
            if prev_vec is not None:
                sim = float(np.dot(vec, prev_vec) / (np.linalg.norm(vec) * np.linalg.norm(prev_vec)))

            if (
                prev_vec is not None
                and sim >= self._params.similarity_threshold
                and (current_tokens + para_tokens) <= self._params.max_tokens
            ):
                current_chunk.append(para)
                current_tokens += para_tokens
            else:
                if current_chunk:
                    chunks.append(self._create_chunk(current_chunk, metadata, section, "semantic"))
                current_chunk = [para]
                current_tokens = para_tokens
                prev_vec = vec

        if current_chunk:
            chunks.append(self._create_chunk(current_chunk, metadata, section, "semantic"))
        return chunks

    def _chunk_by_paragraphs(self, text: str, metadata: Dict, section: Dict) -> List[Document]:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: List[Document] = []
        current_chunk: List[str] = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = self._count_tokens(para)
            if current_tokens + para_tokens > self._params.max_tokens and current_chunk:
                chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
                current_chunk = []
                current_tokens = 0
            current_chunk.append(para)
            current_tokens += para_tokens

        if current_chunk:
            chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
        return chunks

    def _chunk_list(self, list_text: str, metadata: Dict, section: Dict) -> List[Document]:
        items = re.split(r"\n(?=[\d+\.\-\*\+]\s)", list_text)
        chunks: List[Document] = []
        current_chunk: List[str] = []
        current_tokens = 0

        for item in items:
            item_tokens = self._count_tokens(item)
            if current_tokens + item_tokens > self._params.max_tokens and current_chunk:
                chunks.append(self._create_chunk(current_chunk, metadata, section, "list"))
                current_chunk = []
                current_tokens = 0
            current_chunk.append(item)
            current_tokens += item_tokens

        if current_chunk:
            chunks.append(self._create_chunk(current_chunk, metadata, section, "list"))
        return chunks

    def _create_chunk(self, content_parts: List[str], metadata: Dict, section: Dict, chunk_type: str) -> Document:
        content = "\n\n".join(content_parts)
        chunk_metadata = {
            **metadata,
            "chunk_type": chunk_type,
            "section_title": section.get("title", ""),
            "section_level": section.get("level", 0),
            "token_count": self._count_tokens(content),
        }
        return Document(page_content=content, metadata=chunk_metadata)

    def _count_tokens(self, text: str) -> int:
        return len(self._tokenizer.encode(text))


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _split_into_sections(text: str) -> List[Dict]:
    lines = text.split("\n")
    sections: List[Dict] = []
    current_section: List[str] = []
    current_title = "Без названия"
    current_level = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        heading_match = re.match(r"^(#+)\s+(.+)$", line)
        if heading_match:
            if current_section:
                sections.append(
                    {"title": current_title, "level": current_level, "content": "\n".join(current_section)}
                )
            current_level = len(heading_match.group(1))
            current_title = heading_match.group(2)
            current_section = []
        else:
            current_section.append(line)

    if current_section:
        sections.append({"title": current_title, "level": current_level, "content": "\n".join(current_section)})
    return sections


def _detect_content_type(text: str) -> str:
    lines = text.split("\n")
    code_lines = sum(1 for line in lines if line.startswith("    ") or "```" in line)
    if code_lines / max(1, len(lines)) > 0.3:
        return "code"
    table_lines = sum(1 for line in lines if "|" in line and len(line.split("|")) > 2)
    if table_lines / max(1, len(lines)) > 0.3:
        return "table"
    list_lines = sum(1 for line in lines if re.match(r"^[\d+\.\-\*\+]\s", line))
    if list_lines / max(1, len(lines)) > 0.4:
        return "list"
    return "text"
