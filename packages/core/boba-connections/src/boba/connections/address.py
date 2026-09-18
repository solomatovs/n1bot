"""Адрес объекта внешней системы: тонкий контракт и реестр семейств.

Строка url — для модели и показа, разобранные поля — для jsonb и поиска;
оба представления строит и разбирает только модель адреса. Грамматика
строки — знание системы и живёт в её пакете (PgAddress, ChAddress,
ConfluenceAddress, ...); здесь только то, что нужно реестру, чтобы выбрать
класс по виду объекта и форме строки. Семейства находятся по entry points
группы boba.addresses установленных пакетов, как типы соединений: ни
потребитель, ни ядро их не перечисляют.

Ошибки:
AddressError — строка не является адресом класса или семейства, либо вид
    объекта не известен ни одному семейству; текст называет, что не так.
AddressFamiliesError — entry point группы boba.addresses не является
    семейством адресов или не описывает себя.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from importlib.metadata import entry_points
from typing import ClassVar, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

__all__ = [
    "Address",
    "AddressError",
    "AddressFamilies",
    "AddressFamiliesError",
    "AddressFamily",
]


class AddressError(Exception):
    """Строка не является адресом этого класса или семейства."""


class AddressFamiliesError(Exception):
    """Entry point группы boba.addresses не годится семейством."""


class Address(BaseModel, ABC):
    """База адреса: класс знает вид объекта (KIND), строит и разбирает url.

    accepts() — дешёвая структурная проба «эта ли форма», по ней реестр
    выбирает класс среди кандидатов одного вида; parse() — полный разбор с
    точной ошибкой; shape() — форма для подсказки модели.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    KIND: ClassVar[str]

    def to_json(self) -> dict[str, object]:
        """Разобранные поля по alias: это и есть jsonb колонки."""
        return self.model_dump(mode="json", by_alias=True)

    @abstractmethod
    def render(self) -> str: ...

    @classmethod
    @abstractmethod
    def parse(cls, text: str) -> Self: ...

    @classmethod
    @abstractmethod
    def accepts(cls, text: str) -> bool: ...

    @classmethod
    @abstractmethod
    def shape(cls) -> str: ...


class AddressFamily:
    """Реестр адресов одной системы: классы по виду объекта и выбор класса
    по форме строки. Наследник в пакете системы перечисляет MODELS и
    описывает себя: SYSTEM — имя для модели, SCHEMES — схемы url,
    EXAMPLE — шаблон строки. kind берётся с класса, поэтому один вид может
    иметь несколько форм (колонка таблицы, view и matview — один pg_column)."""

    SYSTEM: ClassVar[str] = ""
    SCHEMES: ClassVar[frozenset[str]] = frozenset()
    EXAMPLE: ClassVar[str] = ""
    MODELS: ClassVar[Sequence[type[Address]]] = ()

    @classmethod
    def kinds(cls) -> Sequence[str]:
        """Виды объектов семейства в порядке первого появления в MODELS."""
        return tuple(cls._distinct_kinds())

    @classmethod
    def _distinct_kinds(cls) -> Iterator[str]:
        seen: set[str] = set()
        for model in cls.MODELS:
            if model.KIND in seen:
                continue

            seen.add(model.KIND)
            yield model.KIND

    @classmethod
    def models_of(cls, kind: str) -> Sequence[type[Address]]:
        return tuple(cls._models_of(kind))

    @classmethod
    def _models_of(cls, kind: str) -> Iterator[type[Address]]:
        for model in cls.MODELS:
            if kind == model.KIND:
                yield model

    @classmethod
    def parse(cls, kind: str, text: str) -> Address:
        """Строка → адрес заявленного вида: класс по форме среди классов вида."""
        models = cls.models_of(kind)
        if not models:
            msg = (
                f"{cls.__name__}: unknown kind {kind!r}, "
                f"expected one of {list(cls.kinds())}"
            )
            raise AddressError(msg)

        for model in models:
            if model.accepts(text):
                return model.parse(text)

        shapes: list[str] = []
        for model in models:
            shapes.append(model.shape())

        msg = f"{kind}: address {text!r} matches none of its shapes: {shapes}"
        raise AddressError(msg)

    @classmethod
    def parse_any(cls, text: str) -> Address:
        """Строка → адрес любого вида семейства; вид узнаётся по форме."""
        for model in cls.MODELS:
            if model.accepts(text):
                return model.parse(text)

        msg = (
            f"{cls.__name__}: address {text!r} matches no object shape; "
            f"known kinds: {list(cls.kinds())}"
        )
        raise AddressError(msg)

    @classmethod
    def prompt(cls) -> str:
        """Формы по видам для описания аргумента: по строке на вид."""
        return "\n".join(cls._prompt_lines())

    @classmethod
    def _prompt_lines(cls) -> Iterator[str]:
        for kind in cls.kinds():
            shapes: list[str] = []
            for model in cls.models_of(kind):
                shapes.append(model.shape())

            yield f"{kind}: " + " | ".join(shapes)

    @classmethod
    def describe(cls) -> str:
        """Семейство целиком для подсказки модели: система, пример, формы."""
        schemes = ", ".join(sorted(cls.SCHEMES))
        head = f"{cls.SYSTEM} ({schemes}): {cls.EXAMPLE}"

        return head + "\n" + cls.prompt()


class AddressFamilies:
    """Все установленные семейства: по схеме url и по виду объекта."""

    GROUP: ClassVar[str] = "boba.addresses"

    def __init__(self, families: Sequence[type[AddressFamily]]) -> None:
        self._families = tuple(families)

    @classmethod
    def discover(cls) -> AddressFamilies:
        """Семейства из entry points установленных пакетов, по имени точки."""
        found: list[tuple[str, type[AddressFamily]]] = []
        for entry in entry_points(group=cls.GROUP):
            family = entry.load()
            if not isinstance(family, type) or not issubclass(family, AddressFamily):
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected an AddressFamily subclass, "
                    f"got {family!r}"
                )
                raise AddressFamiliesError(msg)

            if not family.SYSTEM or not family.SCHEMES or not family.MODELS:
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): family must set SYSTEM, SCHEMES and MODELS"
                )
                raise AddressFamiliesError(msg)

            found.append((entry.name, family))

        found.sort(key=lambda pair: pair[0])

        families: list[type[AddressFamily]] = []
        for _, family in found:
            families.append(family)

        return cls(families)

    def families(self) -> Sequence[type[AddressFamily]]:
        return self._families

    def kinds(self) -> Sequence[str]:
        kinds: list[str] = []
        for family in self._families:
            kinds.extend(family.kinds())

        return tuple(kinds)

    def parse(self, kind: str, text: str) -> Address:
        """Строка → адрес заявленного вида; семейство — по виду."""
        return self.of_kind(kind).parse(kind, text)

    def parse_any(self, text: str) -> Address:
        """Строка → адрес; семейство по схеме url, вид по форме."""
        return self.of_scheme(text).parse_any(text)

    def of_kind(self, kind: str) -> type[AddressFamily]:
        for family in self._families:
            if kind in family.kinds():
                return family

        msg = f"address kind {kind!r} is unknown, expected one of {list(self.kinds())}"
        raise AddressError(msg)

    def of_scheme(self, text: str) -> type[AddressFamily]:
        scheme = urlsplit(text).scheme
        for family in self._families:
            if scheme in family.SCHEMES:
                return family

        msg = (
            f"address {text!r}: unknown scheme {scheme!r}, "
            f"expected one of {sorted(self.schemes())}"
        )
        raise AddressError(msg)

    def schemes(self) -> Sequence[str]:
        schemes: set[str] = set()
        for family in self._families:
            schemes.update(family.SCHEMES)

        return tuple(sorted(schemes))

    def prompt(self) -> str:
        """Все семейства для описания аргумента адреса."""
        blocks: list[str] = []
        for family in self._families:
            blocks.append(family.describe())

        return "\n\n".join(blocks)

    def kinds_prompt(self) -> str:
        """Виды объектов по системам для описания аргумента kind."""
        lines: list[str] = []
        for family in self._families:
            lines.append(f"{family.SYSTEM}: " + ", ".join(family.kinds()))

        return "\n".join(lines)
