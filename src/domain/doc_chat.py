"""Доменные типы чата по документам — сообщения и сериализация."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Protocol, runtime_checkable


class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class DocChatMessage:
    """Одно сообщение в чате по документам."""
    role: Role
    content: str


@runtime_checkable
class ChatHistory(Protocol):
    """Протокол хранилища истории чата."""

    def load(self) -> List[DocChatMessage]: ...
    def append(self, question: str, answer: str) -> None: ...


# ---------------------------------------------------------------------------
# Markdown-сериализация
# ---------------------------------------------------------------------------

_HEADER_PATTERN = re.compile(r"^##\s+(User|Assistant)\s*$")
_SEPARATOR = "---"

_ROLE_MAP = {
    "User": Role.USER,
    "Assistant": Role.ASSISTANT,
}


def serialize_exchange(question: str, answer: str) -> str:
    """Сериализовать пару вопрос-ответ в markdown-блок."""
    return f"## User\n{question}\n\n## Assistant\n{answer}\n\n---\n\n"


def parse_messages(text: str) -> List[DocChatMessage]:
    """Разобрать markdown-текст в список сообщений."""
    blocks = _split_into_blocks(text.splitlines())
    return [DocChatMessage(role=role, content=content) for role, content in blocks]


def _split_into_blocks(lines: list[str]) -> list[tuple[Role, str]]:
    """Разбить строки на блоки (role, content) по заголовкам и разделителям."""
    blocks: list[tuple[Role, str]] = []
    current_role: Role | None = None
    current_lines: list[str] = []

    for line in lines:
        header_role = _match_header(line)
        if header_role is not None:
            _flush_block(blocks, current_role, current_lines)
            current_role = header_role
            current_lines = []
        elif _is_separator(line):
            _flush_block(blocks, current_role, current_lines)
            current_role = None
            current_lines = []
        elif current_role is not None:
            current_lines.append(line)

    _flush_block(blocks, current_role, current_lines)
    return blocks


def _match_header(line: str) -> Role | None:
    """Попытаться распознать заголовок роли. Возвращает Role или None."""
    m = _HEADER_PATTERN.match(line.strip())
    if m is None:
        return None
    return _ROLE_MAP[m.group(1)]


def _is_separator(line: str) -> bool:
    """Проверить, является ли строка разделителем блоков."""
    return line.strip() == _SEPARATOR


def _flush_block(
    blocks: list[tuple[Role, str]],
    role: Role | None,
    lines: list[str],
) -> None:
    """Сбросить накопленный блок в список, если он непустой."""
    if role is None:
        return
    content = "\n".join(lines).strip()
    if content:
        blocks.append((role, content))
