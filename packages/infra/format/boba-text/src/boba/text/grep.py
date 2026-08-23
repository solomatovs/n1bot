"""Grep по строкам текста: компиляция шаблона, контекст, обрезка, показ.

Выдача собирается в GrepReport и рисуется в форме ripgrep: номер строки,
`:` у совпадения, `-` у контекста, `--` между несмежными группами.

Ошибки: своих не выпускает; битый regex деградирует в литеральный поиск.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import ClassVar

__all__ = ["GrepLimits", "GrepMatch", "GrepReport", "TextGrep"]


@dataclass(frozen=True)
class GrepLimits:
    """Лимиты grep-выдачи: контекст, строки и длина полей."""

    context: int
    limit: int
    clip_chars: int


@dataclass
class GrepMatch:
    """Одно совпадение: его строка и собранный вокруг контекст."""

    line: int
    content: str
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)

    NUMBER_WIDTH: ClassVar[int] = 6

    @property
    def first_line(self) -> int:
        """Номер первой показанной строки группы."""
        return self.line - len(self.before)

    @property
    def last_line(self) -> int:
        """Номер последней показанной строки группы."""
        return self.line + len(self.after)

    def clipped(self, limit: int) -> GrepMatch:
        before: list[str] = []
        for line in self.before:
            before.append(line[:limit])

        after: list[str] = []
        for line in self.after:
            after.append(line[:limit])

        return GrepMatch(
            line=self.line,
            content=self.content[:limit],
            before=before,
            after=after,
        )

    def numbered_lines(self) -> Iterator[tuple[int, str]]:
        """Строки группы по порядку с их номерами: контекст, совпадение, контекст."""
        number = self.first_line
        for line in self.before:
            yield number, line
            number += 1

        yield self.line, self.content

        number = self.line + 1
        for line in self.after:
            yield number, line
            number += 1

    @classmethod
    def render_line(cls, number: int, content: str, *, matched: bool) -> str:
        marker = "-"
        if matched:
            marker = ":"

        return f"{number:>{cls.NUMBER_WIDTH}}{marker} {content}"


@dataclass(frozen=True)
class GrepReport:
    """Совпадения одного поиска и сводка о нём."""

    matches: Sequence[GrepMatch]
    note: str

    SEPARATOR: ClassVar[str] = "--"

    LANG: ClassVar[str] = "text"
    """Язык markdown-блока показа: нумерованные строки не чужой формат."""

    def render(self) -> str:
        """Текст выдачи; группы через `--`, пересекающиеся склеиваются в одну.

        Строка печатается один раз даже когда попала в контекст соседа, а
        маркер `:` получают все совпавшие строки, а не только начавшие группу.
        """
        matched_numbers = self._matched_numbers()
        blocks: list[str] = []
        printed = 0

        for match in self.matches:
            if printed > 0 and match.first_line > printed + 1:
                blocks.append(self.SEPARATOR)

            for number, content in match.numbered_lines():
                if number <= printed:
                    continue

                matched = number in matched_numbers
                blocks.append(GrepMatch.render_line(number, content, matched=matched))
                printed = number

        return "\n".join(blocks)

    def _matched_numbers(self) -> set[int]:
        numbers: set[int] = set()
        for match in self.matches:
            numbers.add(match.line)

        return numbers


class TextGrep:
    """Поиск совпадений шаблона в строках текста."""

    @staticmethod
    def compile_pattern(
        pattern: str, *, fixed_string: bool, case_insensitive: bool
    ) -> re.Pattern[str]:
        flags = re.IGNORECASE if case_insensitive else 0
        if fixed_string:
            return re.compile(re.escape(pattern), flags)
        try:
            return re.compile(pattern, flags)
        except re.error:
            return re.compile(re.escape(pattern), flags)

    @staticmethod
    def iter_matches(
        text: str, compiled: re.Pattern[str], *, context: int
    ) -> Iterator[GrepMatch]:
        before: deque[str] = deque(maxlen=context if context > 0 else 0)
        after_needed: list[GrepMatch] = []
        for number, line in enumerate(text.splitlines(), start=1):
            for match in after_needed:
                if len(match.after) < context:
                    match.after.append(line)
            ready: list[GrepMatch] = []
            for match in after_needed:
                if len(match.after) >= context:
                    ready.append(match)
            for match in ready:
                after_needed.remove(match)
                yield match
            if compiled.search(line):
                match = GrepMatch(line=number, content=line, before=list(before))
                if context > 0:
                    after_needed.append(match)
                else:
                    yield match
            before.append(line)
        for match in after_needed:
            yield match

    @classmethod
    def report(
        cls,
        text: str,
        compiled: re.Pattern[str],
        limits: GrepLimits,
        source: str,
    ) -> GrepReport:
        """Совпадения под лимитом строк плюс note об источнике."""
        matches: list[GrepMatch] = []
        for match in cls.iter_matches(text, compiled, context=limits.context):
            if len(matches) >= limits.limit:
                break

            matches.append(match.clipped(limits.clip_chars))

        return GrepReport(matches=matches, note=cls.note(source, matches, limits.limit))

    @staticmethod
    def note(source: str, matches: Sequence[GrepMatch], limit: int) -> str:
        if not matches:
            return f"{source}: no matches found"
        parts = [source, f"matches: {len(matches)}"]
        if len(matches) >= limit:
            parts.append(f"showing first {len(matches)} (more found)")
        return "; ".join(parts)
