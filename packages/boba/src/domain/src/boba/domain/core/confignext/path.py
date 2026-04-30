"""ConfigPath: JsonPath-like адресация ($a.b[0].c) для плоского конфиг-пространства.

Сегменты — полиморфные: каждый подкласс Segment знает, как себя:
  - распознать в строке (match_at),
  - отрисовать (render),
  - какой разделитель ставить перед ним внутри пути (separator).

Добавление нового типа сегмента сводится к описанию нового подкласса
Segment и регистрации его в SEGMENT_TYPES — никаких if/elif в ConfigPath.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Generic, Protocol, Self, TypeVar, overload

from boba.domain.core.confignext.value import ConfigValue

__all__ = [
    "ConfigPath",
    "ConfigPathParseError",
    "IndexSegment",
    "NameSegment",
    "Segment",
]


class ConfigPathParseError(ValueError):
    """Не удалось распарсить строку как ConfigPath."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"invalid config path {source!r}: {reason}")
        self.source = source
        self.reason = reason


class Segment(ABC):
    """Один сегмент пути; знает про свой синтаксис, рендер и роли.

    Роли (`mapping_key`, `list_index`) — фасеты, через которые потребитель
    решает, годится ли сегмент для конкретного контекста (ключ словаря,
    индекс списка). По умолчанию все фасеты возвращают None — подклассы
    переопределяют только те, которые реально про них.
    """

    @abstractmethod
    def render(self) -> str:
        """Каноническая строка самого сегмента (без разделителя)."""

    @abstractmethod
    def separator(self) -> str:
        """Разделитель, который ставится ПЕРЕД сегментом внутри пути.

        Используется только если сегмент не первый. Подкласс с самобытным
        синтаксисом (например, `[N]`) возвращает пустую строку.
        """

    @classmethod
    @abstractmethod
    def match_at(cls, body: str, pos: int) -> tuple[Segment, int] | None:
        """Попытаться сматчить сегмент в `body` начиная с `pos`.

        Возвращает (segment, end_pos) при удаче либо None.
        """

    def mapping_key(self) -> str | None:
        """Ключ Mapping, если сегмент годится; иначе None."""
        return None

    def list_index(self) -> int | None:
        """Индекс List, если сегмент годится; иначе None."""
        return None


@dataclass(frozen=True)
class NameSegment(Segment):
    """Именованный сегмент: `.name` внутри пути."""

    name: str
    _NAME_BODY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    _NAME_TOKEN_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigPathParseError(self.name, "empty name segment")
        if not self._NAME_BODY_RE.fullmatch(self.name):
            raise ConfigPathParseError(
                self.name, f"name {self.name!r} must match [A-Za-z_][A-Za-z0-9_]*"
            )

    def render(self) -> str:
        return self.name

    def separator(self) -> str:
        return "."

    def mapping_key(self) -> str | None:
        return self.name

    @classmethod
    def match_at(cls, body: str, pos: int) -> tuple[Segment, int] | None:
        match = cls._NAME_TOKEN_RE.match(body, pos)
        if match is None:
            return None
        return cls(match.group(1)), match.end()


@dataclass(frozen=True)
class IndexSegment(Segment):
    """Индексный сегмент: `[N]` внутри пути."""

    _MIN_INDEX: ClassVar[int] = 0
    _INDEX_TOKEN_RE = re.compile(r"\[(\d+)\]")

    index: int

    def __post_init__(self) -> None:
        if self.index < self._MIN_INDEX:
            raise ConfigPathParseError(
                str(self.index),
                f"index must be >= {self._MIN_INDEX}, got {self.index}",
            )

    def render(self) -> str:
        return f"[{self.index}]"

    def separator(self) -> str:
        return ""

    def list_index(self) -> int | None:
        return self.index

    @classmethod
    def match_at(cls, body: str, pos: int) -> tuple[Segment, int] | None:
        match = cls._INDEX_TOKEN_RE.match(body, pos)
        if match is None:
            return None
        return cls(int(match.group(1))), match.end()


@dataclass(frozen=True)
class ConfigPath:
    """Иерархический адрес значения в плоском конфиг-пространстве.

    Сегменты инкапсулированы (`_segments`); снаружи доступ только через
    методы класса (`__iter__`, `__len__`, `__bool__`, `__getitem__`,
    `last`, `parent`, `startswith`, `relative_to`, `join`).
    """

    _segments: tuple[Segment, ...]

    @classmethod
    def root(cls) -> Self:
        return cls(())

    @classmethod
    def of(cls, *segments: Segment) -> Self:
        return cls(tuple(segments))

    @classmethod
    def parse(cls, source: str) -> Self:
        if not source.startswith("$"):
            raise ConfigPathParseError(source, "must start with '$'")
        body = source[1:]
        if not body:
            return cls.root()
        # Первый именованный сегмент допускается без ведущей точки —
        # нормализуем единый синтаксис, чтобы дальше работал общий парсер.
        if not body.startswith((".", "[")):
            body = "." + body
        return cls(tuple(cls._tokenize(source, body)))

    @classmethod
    def _tokenize(cls, source: str, body: str) -> list[Segment]:
        """Прогнать body через зарегистрированные типы сегментов."""
        segments: list[Segment] = []
        pos = 0
        while pos < len(body):
            matched: tuple[Segment, int] | None = None
            for seg_cls in (NameSegment, IndexSegment):
                attempt = seg_cls.match_at(body, pos)
                if attempt is not None:
                    matched = attempt
                    break
            if matched is None:
                raise ConfigPathParseError(
                    source,
                    f"unexpected character at offset {pos + 1}: {body[pos]!r}",
                )
            seg, pos = matched
            segments.append(seg)
        return segments

    def __iter__(self) -> Iterator[Segment]:
        return iter(self._segments)

    def __len__(self) -> int:
        return len(self._segments)

    def __bool__(self) -> bool:
        return bool(self._segments)

    @overload
    def __getitem__(self, index: int) -> Segment: ...
    @overload
    def __getitem__(self, index: slice) -> ConfigPath: ...
    def __getitem__(self, index: int | slice) -> Segment | ConfigPath:
        if isinstance(index, slice):
            return ConfigPath(self._segments[index])
        return self._segments[index]

    def render(self) -> str:
        parts = ["$"]
        for i, seg in enumerate(self._segments):
            if i > 0:
                parts.append(seg.separator())
            parts.append(seg.render())
        return "".join(parts)

    def join(self, *suffix: Segment) -> ConfigPath:
        return ConfigPath(self._segments + tuple(suffix))

    def parent(self) -> ConfigPath:
        if not self._segments:
            raise ValueError("root path has no parent")
        return ConfigPath(self._segments[:-1])

    def last(self) -> Segment:
        if not self._segments:
            raise ValueError("root path has no last segment")
        return self._segments[-1]

    def startswith(self, prefix: ConfigPath) -> bool:
        if len(prefix) > len(self):
            return False
        return self._segments[: len(prefix)] == prefix._segments

    def relative_to(self, prefix: ConfigPath) -> ConfigPath:
        if not self.startswith(prefix):
            raise ValueError(
                f"path {self.render()!r} is not under prefix {prefix.render()!r}"
            )
        return ConfigPath(self._segments[len(prefix) :])

    def is_root(self) -> bool:
        return not self._segments

    def __str__(self) -> str:
        return self.render()


class ConfigSource(ABC):
    """Источник конфига: имя, приоритет, плоский снимок."""

    @abstractmethod
    def name(self) -> str:
        """Уникальное имя источника — для origin-трассировки."""
        ...

    @abstractmethod
    def priority(self) -> int:
        """Приоритет при мерже; больше = важнее (last-wins)."""
        ...

    @abstractmethod
    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        """Eager-snapshot всех известных источнику значений."""
        ...

    def describe(self, path: ConfigPath) -> str:
        """Operator-readable hint, как задать значение через этот источник."""
        del path
        return ""


T = TypeVar("T")
U = TypeVar("U")


class ConfigLookup(ABC, Generic[T]):
    """Результат поиска: Found(value) или NotFound."""

    @abstractmethod
    def is_found(self) -> bool: ...

    @abstractmethod
    def value(self) -> T:
        """Достать значение; raises LookupError если NotFound."""
        ...

    @abstractmethod
    def or_else(self, default: T) -> T:
        """Значение либо default."""
        ...


@dataclass(frozen=True)
class Found(ConfigLookup[T], Generic[T]):
    """Значение найдено (в т.ч. может быть NullValue)."""

    found: T

    def is_found(self) -> bool:
        return True

    def value(self) -> T:
        return self.found

    def or_else(self, default: T) -> T:
        return self.found


class NotFound(ConfigLookup[T], Generic[T]):
    """Значение отсутствует. Используй sentinel NOT_FOUND, а не создавай напрямую."""

    __slots__ = ()

    def is_found(self) -> bool:
        return False

    def value(self) -> T:
        raise LookupError("value not found")

    def or_else(self, default: T) -> T:
        return default

    def __repr__(self) -> str:
        return "NotFound()"


class ConfigSpace(Protocol):
    """Минимальный контракт, нужный полям ObjectSchema для чтения значений."""

    def lookup(self, path: ConfigPath) -> ConfigLookup[ConfigValue]:
        """Найти значение в указанном пути."""
        ...

    def child_segments(self, prefix: ConfigPath) -> Sequence[Segment]:
        """Уникальные первые сегменты непосредственно под prefix."""
        ...
