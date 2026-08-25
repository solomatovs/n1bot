"""Строковый шаблон с именованными полями: поля, подстановка, обратный разбор.

Ошибки:
TemplateError — шаблон негоден, поле не то или текст не по шаблону.
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict

__all__ = ["FieldTemplate", "TemplateError"]


class TemplateError(ValueError):
    """Шаблон или текст под него негодны."""


class FieldTemplate(BaseModel):
    """Шаблон в синтаксисе str.format с одними именованными полями: `a{name}b`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str

    @classmethod
    def parse(cls, text: str) -> Self:
        """Шаблон с проверкой синтаксиса; позиционные поля и спецификаторы негодны."""
        template = cls(text=text)
        template._pieces()

        return template

    def fields(self) -> tuple[str, ...]:
        """Имена полей без повторов в порядке появления."""
        names: list[str] = []
        for _, name in self._pieces():
            if name is None:
                continue

            if name in names:
                continue

            names.append(name)

        return tuple(names)

    def only(self, known: Iterable[str]) -> Self:
        """Все поля шаблона — из известного набора."""
        unknown = sorted(set(self.fields()) - set(known))
        if unknown:
            msg = f"unknown variables {unknown} in template {self.text!r}"
            raise TemplateError(msg)

        return self

    def having(self, field: str) -> Self:
        """Поле присутствует в шаблоне."""
        if field not in self.fields():
            msg = f"template {self.text!r} has no {{{field}}}"
            raise TemplateError(msg)

        return self

    def single(self, field: str) -> Self:
        """Поле присутствует ровно один раз и других полей нет: годится для extract."""
        self._around(field)

        return self

    def render(self, values: Mapping[str, str]) -> str:
        try:
            return self.text.format_map(dict(values))
        except KeyError as exc:
            msg = f"template {self.text!r}: no value for {{{exc.args[0]}}}"
            raise TemplateError(msg) from exc

    def extract(self, text: str, field: str) -> str:
        """Значение единственного поля: текст обязан совпасть с шаблоном вокруг него."""
        head, tail = self._around(field)

        fits = text.startswith(head) and text.endswith(tail)
        if not fits:
            msg = f"text {text!r} does not match template {self.text!r}"
            raise TemplateError(msg)

        value = text[len(head) : len(text) - len(tail)]
        if not value:
            msg = f"text {text!r} has empty {{{field}}} for template {self.text!r}"
            raise TemplateError(msg)

        return value

    def _around(self, field: str) -> tuple[str, str]:
        """Литералы до и после поля; иные поля извлечению мешают."""
        head: list[str] = []
        tail: list[str] = []
        seen = False

        for literal, name in self._pieces():
            if seen:
                tail.append(literal)
            else:
                head.append(literal)

            if name is None:
                continue

            if name != field:
                msg = (
                    f"template {self.text!r}: field {{{name}}} "
                    f"blocks extracting {{{field}}}"
                )
                raise TemplateError(msg)

            if seen:
                msg = f"template {self.text!r}: field {{{field}}} repeats"
                raise TemplateError(msg)

            seen = True

        if not seen:
            msg = f"template {self.text!r} has no {{{field}}}"
            raise TemplateError(msg)

        return "".join(head), "".join(tail)

    def _pieces(self) -> tuple[tuple[str, str | None], ...]:
        """Пары (литерал, поле после него); у хвоста поля нет."""
        try:
            parsed = list(string.Formatter().parse(self.text))
        except ValueError as exc:
            msg = f"bad template {self.text!r}: {exc}"
            raise TemplateError(msg) from exc

        pieces: list[tuple[str, str | None]] = []
        for literal, name, spec, conversion in parsed:
            if name is None:
                pieces.append((literal, None))
                continue

            plain = name.isidentifier() and not spec and not conversion
            if not plain:
                msg = f"template {self.text!r}: only plain {{name}} fields are allowed"
                raise TemplateError(msg)

            pieces.append((literal, name))

        return tuple(pieces)
