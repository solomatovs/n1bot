from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import numpy as np
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings

from config import EMBEDDING_MODEL, enc

log = logging.getLogger(__name__)


class AdvancedChunker:
    def __init__(self, tokenizer=None, max_tokens: int = 512, overlap_tokens: int = 50):
        self.tokenizer = tokenizer
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    def split_into_chunks(self, docs: List[Document], ollama_api_url: str) -> List[Document]:
        all_chunks: List[Document] = []
        for doc in docs:
            chunks = self._process_document(doc, ollama_api_url)
            all_chunks.extend(chunks)
        return all_chunks

    def _process_document(self, doc: Document, ollama_api_url: str) -> List[Document]:
        text = doc.page_content
        metadata = doc.metadata.copy()
        sections = self._split_into_sections(text)
        chunks: List[Document] = []
        for section in sections:
            section_chunks = self._chunk_section(section, metadata, ollama_api_url)
            chunks.extend(section_chunks)
        return chunks

    def _split_into_sections(self, text: str) -> List[Dict]:
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

    def _chunk_section(self, section: Dict, metadata: Dict, ollama_api_url: str) -> List[Document]:
        content = section["content"]
        content_type = _detect_content_type(content)
        if content_type == "code":
            return self._chunk_code(content, metadata, section)
        elif content_type == "table":
            return self._chunk_table(content, metadata, section)
        elif content_type == "list":
            return self._chunk_list(content, metadata, section)
        else:
            return self._chunk_text(content, metadata, section, ollama_api_url)

    def _chunk_text(self, text: str, metadata: Dict, section: Dict, ollama_api_url: str) -> List[Document]:
        try:
            embedding = OllamaEmbeddings(model=EMBEDDING_MODEL, base_url=ollama_api_url)
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            if not paragraphs:
                return []

            vecs = embedding.embed_documents(paragraphs)
            chunks: List[Document] = []
            current_chunk: List[str] = []
            current_tokens = 0
            prev_vec = None

            for para, vec in zip(paragraphs, vecs):
                para_tokens = self._count_tokens(para)
                sim = 1.0
                if prev_vec is not None:
                    sim = float(np.dot(vec, prev_vec) / (np.linalg.norm(vec) * np.linalg.norm(prev_vec)))

                if prev_vec is not None and sim >= 0.7 and (current_tokens + para_tokens) <= self.max_tokens:
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

        except (ConnectionError, ValueError, RuntimeError) as e:
            log.warning("Семантический чанкинг не удался, фоллбэк на параграфы: %s", e)
            return self._chunk_by_sentences(text, metadata, section)

    def _chunk_by_sentences(self, text: str, metadata: Dict, section: Dict) -> List[Document]:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: List[Document] = []
        current_chunk: List[str] = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = self._count_tokens(para)
            if current_tokens + para_tokens > self.max_tokens and current_chunk:
                chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
                current_chunk = []
                current_tokens = 0
            current_chunk.append(para)
            current_tokens += para_tokens

        if current_chunk:
            chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
        return chunks

    def _chunk_code(self, code: str, metadata: Dict, section: Dict) -> List[Document]:
        return [self._create_chunk([code], metadata, section, "code")]

    def _chunk_table(self, table: str, metadata: Dict, section: Dict) -> List[Document]:
        return [self._create_chunk([table], metadata, section, "table")]

    def _chunk_list(self, list_text: str, metadata: Dict, section: Dict) -> List[Document]:
        items = re.split(r"\n(?=[\d+\.\-\*\+]\s)", list_text)
        chunks: List[Document] = []
        current_chunk: List[str] = []
        current_tokens = 0

        for item in items:
            item_tokens = self._count_tokens(item)
            if current_tokens + item_tokens > self.max_tokens and current_chunk:
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
        if self.tokenizer:
            return len(self.tokenizer.encode(text))
        return len(text) // 4


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

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


def _get_line_type(line: str) -> str:
    if re.match(r"^#+\s+", line):
        return "heading"
    if re.match(r"^\d+\.\s+", line):
        return "list"
    if re.match(r"^[-*+]\s+", line):
        return "list"
    if line.startswith("```") or line.startswith("    "):
        return "code"
    if "|" in line and len(line.split("|")) > 2:
        return "table"
    return "paragraph"


def _split_into_structural_blocks(text: str) -> List[Dict[str, str]]:
    blocks: List[Dict[str, str]] = []
    lines = text.split("\n")
    current_block: List[str] = []
    current_type: Optional[str] = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        line_type = _get_line_type(line)
        if current_type != line_type or line_type in ("heading", "list"):
            if current_block and current_type:
                blocks.append({"text": "\n".join(current_block), "type": current_type})
            current_block = [line]
            current_type = line_type
        else:
            current_block.append(line)

    if current_block and current_type:
        blocks.append({"text": "\n".join(current_block), "type": current_type})
    return blocks


# ---------------------------------------------------------------------------
# Public API (обёртка для совместимости)
# ---------------------------------------------------------------------------

def split_into_chunks_semantic(
    docs: List[Document],
    ollama_api_url: str,
    model: str = EMBEDDING_MODEL,
    threshold: float = 0.8,
    max_tokens: int = 500,
    tokenizer=None,
) -> List[Document]:
    chunker = AdvancedChunker(
        tokenizer=tokenizer or enc,
        max_tokens=max_tokens,
        overlap_tokens=int(max_tokens * 0.1),
    )
    return chunker.split_into_chunks(docs, ollama_api_url)
