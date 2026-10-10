"""Метки кэша: какое сообщение запроса получает cache_control и с каким значением.

CacheMarks повторяет выбор X1t @209488154 над внутренним списком сообщений,
CacheStamp строит само значение метки (g2 @209583686). Выбором пользуется
WireSerializer, значением — сборка system, tools и messages тела.

Ошибки: модуль ошибок не выпускает.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Sequence

from boba.prompt_assembly.entries import ApiSystemEntry, AssistantEntry, Block, BlockKit, Entry, UserEntry, WireShape
from boba.prompt_assembly.records import BlockType, JsText, Json, Key



class CacheKind(StrEnum):
    """Значения полей метки кэша."""

    EPHEMERAL = "ephemeral"
    GLOBAL = "global"
    TTL_1H = "1h"


class CacheMarks:
    """X1t @209488154: какие сообщения получают метку кэша.

    Основная метка — на последнем помечаемом сообщении либо на хвостовом
    сообщении role:"system" с текстом. При skipCacheWrite (запрос
    суммаризации) она отступает на одно помечаемое сообщение. Когда запрос
    уходит без нити (thread), а флаг forkCachePin включён, вторая метка
    встаёт на помечаемое сообщение перед хвостовым.
    """

    def __init__(self, shape: WireShape) -> None:
        self._shape = shape
        self._blocks = BlockKit()
        self._js = JsText()

    def choose(self, entries: Sequence[Entry], skip_cache_write: bool, fork_pin: bool) -> list[int]:
        tail = self._step_back(entries, len(entries) - 1)
        if skip_cache_write:
            tail = self._step_back(entries, tail - 1)

        if tail < 0:
            return []

        main = tail
        last = len(entries) - 1
        if not skip_cache_write and self._markable_system(entries, last, tail):
            main = last

        if skip_cache_write or not fork_pin:
            return [main]

        pinned = self._step_back(entries, tail - 1)
        if pinned < 0:
            return [main]

        return [pinned, main]

    def stampable(self, block: Block) -> bool:
        """f$e."""
        if self._blocks.is_text(block):
            text = block.get(Key.TEXT)
            if not isinstance(text, str) or text == "":
                return False
            if not self._shape.echo_api_block_order:
                return True
            return not self._js.blank(text)

        if self._blocks.is_thinking(block):
            return False

        return self._blocks.kind(block) != BlockType.FALLBACK

    def _step_back(self, entries: Sequence[Entry], start: int) -> int:
        position = start

        while position >= 0 and self._skipped(entries[position]):
            position -= 1

        return position

    def _skipped(self, entry: Entry) -> bool:
        if isinstance(entry, ApiSystemEntry):
            return True

        if isinstance(entry, UserEntry):
            return entry.ephemeral

        if not isinstance(entry, AssistantEntry):
            return True

        if not entry.content:
            return True

        return not self.stampable(entry.content[-1])

    def _markable_system(self, entries: Sequence[Entry], position: int, tail: int) -> bool:
        """Ce: последний api_system с непустым текстом, без эфемерных user между ним и хвостом."""
        entry = entries[position]
        if not isinstance(entry, ApiSystemEntry) or entry.ephemeral:
            return False

        for between in entries[tail + 1 : position]:
            if isinstance(between, UserEntry) and between.ephemeral:
                return False

        return not self._js.blank(entry.content)


class CacheStamp:
    """Значение cache_control (g2): ephemeral, ttl при часовом кэше, scope у глобального блока."""

    def mark(self, ttl: str, scope: str) -> dict[str, Json]:
        value: dict[str, Json] = {Key.TYPE: CacheKind.EPHEMERAL}

        if ttl:
            value[Key.TTL] = ttl

        if scope:
            value[Key.SCOPE] = scope

        return value
