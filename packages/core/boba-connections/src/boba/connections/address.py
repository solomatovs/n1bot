"""Адрес объекта внешней системы: тонкий контракт для реестров.

Строка url — для модели и показа, разобранные поля — для jsonb и поиска;
оба представления строит и разбирает только модель адреса. Грамматика
строки — знание системы и живёт в её пакете (PgAddress, ChAddress,
ConfluenceAddress, ...); здесь только то, что нужно реестру, чтобы выбрать
класс по виду объекта и форме строки.

Ошибки:
AddressError — строка не является адресом класса или семейства; текст
    называет, что именно не так.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict

__all__ = ["Address", "AddressError", "AddressFamily"]


class AddressError(Exception):
    """Строка не является адресом этого класса или семейства."""


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
    по форме строки. Наследник в пакете системы перечисляет MODELS; kind
    берётся с класса, поэтому один вид может иметь несколько форм (колонка
    таблицы, view и matview — один pg_column)."""

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
