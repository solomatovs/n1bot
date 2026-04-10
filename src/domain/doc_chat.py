"""Доменные типы чата по документам — блоки истории и сериализация.

Вся история чата — последовательность типизированных блоков.
Один обмен (вопрос → ответ) = список блоков между разделителями.
Каждый этап pipeline может добавить свой блок.

Формат chat_history.md:

    ## User
    вопрос пользователя

    ## Search
    - file.md:5-12 (секция: Введение, score: 0.87)

    ## Context
    - file.md:1-32 (чанк: 5-12, секция: Введение, score: 0.87)

    ## Thinking
    размышления модели

    ## Assistant
    ответ модели

    ---
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Protocol, runtime_checkable


class BlockType(Enum):
    """Тип блока в истории чата.

    value — заголовок в markdown, используется для сериализации/парсинга.
    """
    USER = "User"
    SEARCH = "Search"
    CONTEXT = "Context"
    THINKING = "Thinking"
    ASSISTANT = "Assistant"


@dataclass(frozen=True)
class HistoryBlock:
    """Один блок в истории — тип + текстовое содержимое."""
    block_type: BlockType
    content: str


@dataclass(frozen=True)
class DocChatExchange:
    """Один обмен в чате: вопрос пользователя и все этапы ответа."""
    blocks: List[HistoryBlock] = field(default_factory=list)

    @property
    def question(self) -> str:
        return self._first_content(BlockType.USER)

    @property
    def answer(self) -> str:
        return self._first_content(BlockType.ASSISTANT)

    def _first_content(self, bt: BlockType) -> str:
        for b in self.blocks:
            if b.block_type == bt:
                return b.content
        return ""


@runtime_checkable
class ChatHistory(Protocol):
    """Протокол хранилища истории чата."""

    def load(self) -> List[DocChatExchange]: ...
    def append(self, exchange: DocChatExchange) -> None: ...


# ---------------------------------------------------------------------------
# Markdown-сериализация
# ---------------------------------------------------------------------------

_HEADER_PATTERN = re.compile(r"^##\s+(\w+)\s*$")
_SEPARATOR = "---"

_HEADER_TO_TYPE = {bt.value: bt for bt in BlockType}


def serialize_block(block: HistoryBlock) -> str:
    """Сериализовать один блок в markdown (append-friendly)."""
    return f"## {block.block_type.value}\n{block.content}\n\n"


def serialize_exchange(exchange: DocChatExchange) -> str:
    """Сериализовать полный обмен в markdown-блок."""
    parts = [serialize_block(b) for b in exchange.blocks]
    return "".join(parts) + "---\n\n"


def parse_exchanges(text: str) -> List[DocChatExchange]:
    """Разобрать markdown-текст в список обменов."""
    raw_exchanges = _split_into_raw_exchanges(text.splitlines())
    return [
        DocChatExchange(blocks=blocks)
        for blocks in raw_exchanges
        if blocks
    ]


def _split_into_raw_exchanges(lines: list[str]) -> list[list[HistoryBlock]]:
    """Разбить строки на обмены, каждый — список HistoryBlock."""
    exchanges: list[list[HistoryBlock]] = []
    current_type: BlockType | None = None
    current_lines: list[str] = []
    current_blocks: list[HistoryBlock] = []

    def _flush_block() -> None:
        nonlocal current_type, current_lines
        if current_type is not None:
            content = "\n".join(current_lines).strip()
            if content:
                current_blocks.append(HistoryBlock(current_type, content))
        current_type = None
        current_lines = []

    for line in lines:
        header_type = _match_header(line)
        if header_type is not None:
            _flush_block()
            current_type = header_type
        elif _is_separator(line):
            _flush_block()
            if current_blocks:
                exchanges.append(current_blocks)
                current_blocks = []
        elif current_type is not None:
            current_lines.append(line)

    _flush_block()
    if current_blocks:
        exchanges.append(current_blocks)

    return exchanges


def _match_header(line: str) -> BlockType | None:
    m = _HEADER_PATTERN.match(line.strip())
    if m is None:
        return None
    return _HEADER_TO_TYPE.get(m.group(1))


def _is_separator(line: str) -> bool:
    return line.strip() == _SEPARATOR
