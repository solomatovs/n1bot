"""Доменные типы чата по документам — блоки истории и сериализация.

Вся история чата — последовательность типизированных блоков.
Один обмен (вопрос → ответ) = список блоков между разделителями.
Каждый этап pipeline может добавить свой блок.

Взаимодействие с историей — только через Reader / Writer / Tail:

    Запись:
        writer = MarkdownBlockWriter(path)
        writer.write_block(block)
        writer.write_separator()

    Чтение (пакетное):
        reader = MarkdownBlockReader()
        exchanges = reader.read_exchanges(text)

    Чтение (инкрементальное, tail -f):
        tail = HistoryTail(path)
        new_blocks = tail.read_new()
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Protocol, Union, runtime_checkable


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
# Константы разметки
# ---------------------------------------------------------------------------

_HEADER_PATTERN = re.compile(r"^##\s+(\w+)\s*$")
_SEPARATOR = "---"
_DETAILS_OPEN = re.compile(r"^\s*<details(?:\s+open)?>\s*$")
_DETAILS_SUMMARY = re.compile(r"^\s*<summary>.*</summary>\s*$")
_DETAILS_CLOSE = re.compile(r"^\s*</details>\s*$")

_HEADER_TO_TYPE = {bt.value: bt for bt in BlockType}

DETAIL_LABELS: dict[BlockType, str] = {
    BlockType.SEARCH: "🔍 Найденные фрагменты",
    BlockType.CONTEXT: "📚 Контекст из документов",
    BlockType.THINKING: "💭 Размышления",
}


# ---------------------------------------------------------------------------
# Markdown-сериализация (запись)
# ---------------------------------------------------------------------------

def serialize_block(block: HistoryBlock) -> str:
    """Сериализовать один блок в markdown (append-friendly).

    User → blockquote, Search/Context/Thinking → <details>.
    """
    header = f"## {block.block_type.value}\n"
    match block.block_type:
        case BlockType.USER:
            quoted = "\n".join(
                f"> {line}" if line.strip() else ">"
                for line in block.content.splitlines()
            )
            return f"{header}{quoted}\n\n"
        case bt if bt in DETAIL_LABELS:
            label = DETAIL_LABELS[bt]
            return (
                f"{header}"
                f"<details>\n<summary>{label}</summary>\n\n"
                f"{block.content}\n\n"
                f"</details>\n\n"
            )
        case _:
            return f"{header}{block.content}\n\n"


def serialize_exchange(exchange: DocChatExchange) -> str:
    """Сериализовать полный обмен в markdown-блок."""
    parts = [serialize_block(b) for b in exchange.blocks]
    return "".join(parts) + "---\n\n"


# ---------------------------------------------------------------------------
# MarkdownBlockReader — потоковый reader (чтение)
# ---------------------------------------------------------------------------

_Separator = type("_Separator", (), {"__repr__": lambda self: "---"})
"""Внутренний маркер разделителя обменов."""

_Item = Union[HistoryBlock, _Separator]


class MarkdownBlockReader:
    """Потоковый reader для блоков markdown-истории чата.

    Буферизует входные строки и выдаёт завершённые HistoryBlock.

    Два режима работы:
        Инкрементальный (для tail):
            blocks = reader.feed(chunk)   # порция текста → готовые блоки
            last   = reader.flush()       # финализация остатка буфера

        Пакетный (для загрузки истории):
            exchanges = reader.read_exchanges(full_text)
            blocks    = reader.read_blocks(full_text)

    Состояние сохраняется между вызовами feed() — если блок
    разрезан между двумя порциями, он дочитается при следующем feed().
    """

    def __init__(self) -> None:
        self._current_type: BlockType | None = None
        self._current_lines: list[str] = []

    # -- Инкрементальный API ---------------------------------------------------

    def feed(self, text: str) -> List[HistoryBlock]:
        """Подать порцию текста, вернуть завершённые блоки.

        Незавершённый блок остаётся в буфере до следующего feed() или flush().
        Разделители (---) не возвращаются.
        """
        items = self._feed_lines(text.splitlines())
        return [it for it in items if isinstance(it, HistoryBlock)]

    def flush(self) -> HistoryBlock | None:
        """Финализировать буфер (EOF). Вернуть блок если есть незавершённый."""
        return self._flush_block()

    # -- Пакетный API ----------------------------------------------------------

    def read_exchanges(self, text: str) -> List[DocChatExchange]:
        """Прочитать полный текст в список обменов (batch)."""
        items = self._feed_lines(text.splitlines())
        last = self._flush_block()
        if last is not None:
            items.append(last)
        return self._group_exchanges(items)

    def read_blocks(self, text: str) -> List[HistoryBlock]:
        """Прочитать полный текст в плоский список блоков (batch)."""
        items = self._feed_lines(text.splitlines())
        last = self._flush_block()
        if last is not None:
            items.append(last)
        return [it for it in items if isinstance(it, HistoryBlock)]

    # -- Внутренняя логика -----------------------------------------------------

    def _feed_lines(self, lines: list[str]) -> list[_Item]:
        """Обработать строки, вернуть готовые элементы (блоки + разделители)."""
        items: list[_Item] = []
        for line in lines:
            header = _match_header(line)
            if header is not None:
                block = self._flush_block()
                if block is not None:
                    items.append(block)
                self._current_type = header
            elif _is_separator(line):
                block = self._flush_block()
                if block is not None:
                    items.append(block)
                items.append(_Separator())
            elif self._current_type is not None:
                self._current_lines.append(line)
        return items

    def _flush_block(self) -> HistoryBlock | None:
        """Собрать текущий буфер в HistoryBlock и сбросить состояние."""
        if self._current_type is None:
            return None
        content = "\n".join(self._current_lines).strip()
        content = _unwrap_block(self._current_type, content)
        bt = self._current_type
        self._current_type = None
        self._current_lines = []
        if not content:
            return None
        return HistoryBlock(bt, content)

    @staticmethod
    def _group_exchanges(items: list[_Item]) -> List[DocChatExchange]:
        """Сгруппировать элементы в обмены по разделителям."""
        exchanges: list[DocChatExchange] = []
        current: list[HistoryBlock] = []
        for item in items:
            if isinstance(item, _Separator):
                if current:
                    exchanges.append(DocChatExchange(blocks=current))
                    current = []
            elif isinstance(item, HistoryBlock):
                current.append(item)
        if current:
            exchanges.append(DocChatExchange(blocks=current))
        return exchanges


# ---------------------------------------------------------------------------
# Convenience-функции (тонкие обёртки над reader)
# ---------------------------------------------------------------------------

def parse_exchanges(text: str) -> List[DocChatExchange]:
    """Разобрать markdown-текст в список обменов."""
    return MarkdownBlockReader().read_exchanges(text)


def parse_blocks(text: str) -> List[HistoryBlock]:
    """Разобрать markdown-фрагмент в плоский список блоков."""
    return MarkdownBlockReader().read_blocks(text)


# ---------------------------------------------------------------------------
# Вспомогательные функции разметки
# ---------------------------------------------------------------------------

def _unwrap_block(bt: BlockType, content: str) -> str:
    """Снять markdown-обёртки (blockquote / <details>) при парсинге."""
    if bt == BlockType.USER:
        lines = content.splitlines()
        stripped = [re.sub(r"^>\s?", "", line) for line in lines]
        return "\n".join(stripped).strip()
    if bt in DETAIL_LABELS:
        lines = content.splitlines()
        cleaned = [
            line for line in lines
            if not (_DETAILS_OPEN.match(line)
                    or _DETAILS_SUMMARY.match(line)
                    or _DETAILS_CLOSE.match(line))
        ]
        return "\n".join(cleaned).strip()
    return content


def _match_header(line: str) -> BlockType | None:
    m = _HEADER_PATTERN.match(line.strip())
    if m is None:
        return None
    return _HEADER_TO_TYPE.get(m.group(1))


def _is_separator(line: str) -> bool:
    return line.strip() == _SEPARATOR


# ---------------------------------------------------------------------------
# MarkdownBlockWriter — потоковый writer (запись)
# ---------------------------------------------------------------------------

class MarkdownBlockWriter:
    """Append-only writer для блоков markdown-истории чата.

    Единственная точка записи в файл истории::

        writer = MarkdownBlockWriter(path)
        writer.write_block(block)        # один блок
        writer.write_separator()         # разделитель обмена (---)
        writer.write_exchange(exchange)  # полный обмен + разделитель
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def write_block(self, block: HistoryBlock) -> None:
        """Дописать один блок в файл (append)."""
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(serialize_block(block))

    def write_separator(self) -> None:
        """Дописать разделитель обмена (---)."""
        with open(self._path, "a", encoding="utf-8") as f:
            f.write("\n---\n\n")

    def write_exchange(self, exchange: DocChatExchange) -> None:
        """Записать полный обмен (все блоки + разделитель)."""
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(serialize_exchange(exchange))


# ---------------------------------------------------------------------------
# HistoryTail — инкрементальное чтение (tail -f)
# ---------------------------------------------------------------------------

class HistoryTail:
    """Отслеживает новые записи в chat_history.md (аналог tail -f).

    Запоминает байтовую позицию и использует MarkdownBlockReader
    для буферизации — если блок разрезан между двумя чтениями,
    он дочитается при следующем read_new()::

        tail = HistoryTail(path)
        new_blocks = tail.read_new()   # только новые завершённые блоки
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._byte_pos = path.stat().st_size if path.exists() else 0
        self._reader = MarkdownBlockReader()

    def read_new(self) -> List[HistoryBlock]:
        """Прочитать блоки, появившиеся после последнего чтения."""
        if not self._path.exists():
            return []
        with open(self._path, "rb") as f:
            f.seek(self._byte_pos)
            new_bytes = f.read()
        if not new_bytes:
            return []
        self._byte_pos += len(new_bytes)
        return self._reader.feed(new_bytes.decode("utf-8"))
