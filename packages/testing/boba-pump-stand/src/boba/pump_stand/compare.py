"""Сверка значений перекачки поколоночно: значения двух сторон приводятся к
одному виду (числа, float с NaN, моменты в UTC, UUID, байты, JSON, векторы) и
сравниваются, расхождения копятся по колонкам с примерами."""

from __future__ import annotations

import json
import math
import struct
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar

__all__ = [
    "BYTES",
    "DATETIME",
    "EXACT",
    "FLOAT",
    "FLOAT32",
    "FLOAT32_ULP",
    "FLOAT64_ULP",
    "JSON",
    "NUMBER",
    "UUID",
    "VECTOR",
    "Mismatch",
    "Report",
    "Values",
]

FLOAT32_ULP = 2e-7
FLOAT64_ULP = 4e-16


class Values:
    """Сверка значений колонки как есть: база для видов, которым нужно
    привести значение перед сравнением. Наследники переопределяют of и same."""

    def canon(self, value: Any) -> Any:
        if value is None:
            return None

        return self.of(value)

    def of(self, value: Any) -> Any:
        return value

    def same(self, left: Any, right: Any, tolerance: float) -> bool:
        return left == right


class Numbers(Values):
    """Число любого драйвера — Decimal по его тексту: float, int, bool и
    Decimal одного значения совпадают."""

    def of(self, value: Any) -> Any:
        if isinstance(value, bool):
            return Decimal(int(value))

        return Decimal(str(value))


class Floats(Values):
    """float с NaN и бесконечностями; tolerance — относительный допуск."""

    def of(self, value: Any) -> Any:
        return float(value)

    def same(self, left: Any, right: Any, tolerance: float) -> bool:
        if left is None or right is None:
            return left is right

        if math.isnan(left):
            return math.isnan(right)

        if left == right:
            return True

        if math.isinf(left):
            return False

        return abs(left - right) <= abs(left) * tolerance


class Floats32(Floats):
    """BINARY_FLOAT: обе стороны приводятся к ближайшему float32 — postgres
    печатает real кратчайшим текстом float32, а не его значением в double."""

    def of(self, value: Any) -> Any:
        packed = struct.pack("f", float(value))
        (single,) = struct.unpack("f", packed)

        return single


class Moments(Values):
    """Дата и время как наивное UTC: aware приводится к UTC, date — к полуночи."""

    def of(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return self._naive(value)

        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)

        raise AssertionError(f"not a date or datetime: {value!r}")

    def _naive(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value

        return value.astimezone(UTC).replace(tzinfo=None)


class Uuids(Values):
    """UUID из байтов RAW(16), объекта драйвера или текста."""

    def of(self, value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return value

        if isinstance(value, bytes):
            return uuid.UUID(bytes=value)

        return uuid.UUID(str(value))


class Hexes(Values):
    """Байты как шестнадцатеричный текст в нижнем регистре: bytes драйвера или
    hex() ClickHouse."""

    def of(self, value: Any) -> Any:
        if isinstance(value, bytes | bytearray | memoryview):
            return bytes(value).hex()

        return str(value).lower()


class Documents(Values):
    """JSON как разобранное значение: текст разбирается, dict драйвера — как есть."""

    def of(self, value: Any) -> Any:
        if isinstance(value, str):
            return json.loads(value)

        return value


class Vectors(Values):
    """VECTOR Oracle, real[] postgres и Array ClickHouse — список float."""

    def of(self, value: Any) -> Any:
        return [float(item) for item in value]


EXACT = Values()
NUMBER = Numbers()
FLOAT = Floats()
FLOAT32 = Floats32()
DATETIME = Moments()
UUID = Uuids()
BYTES = Hexes()
JSON = Documents()
VECTOR = Vectors()


@dataclass(frozen=True)
class Mismatch:
    """Расхождение колонки: сколько строк и первые примеры (id, Oracle, приёмник)."""

    rows: int
    samples: tuple[tuple[Any, Any, Any], ...]


@dataclass
class Report:
    """Расхождения по колонкам после сверки."""

    SAMPLES: ClassVar[int] = 3

    mismatches: dict[str, Mismatch] = field(default_factory=dict)

    def compare(  # noqa: PLR0913 — колонка, её вид, допуск и обе стороны
        self,
        name: str,
        compare: Values,
        tolerance: float,
        ids: Sequence[Any],
        oracle: Sequence[Any],
        target: Sequence[Any],
    ) -> None:
        count = 0
        samples: list[tuple[Any, Any, Any]] = []
        for key, left, right in zip(ids, oracle, target, strict=True):
            expected = compare.canon(left)
            landed = compare.canon(right)
            if compare.same(expected, landed, tolerance):
                continue

            count += 1
            if len(samples) < self.SAMPLES:
                samples.append((key, expected, landed))

        if count:
            self.mismatches[name] = Mismatch(count, tuple(samples))

    def render(self) -> str:
        lines: list[str] = []
        for name, mismatch in self.mismatches.items():
            lines.append(
                f"{name}: {mismatch.rows} rows differ, e.g. {mismatch.samples}"
            )

        return "\n".join(lines)
