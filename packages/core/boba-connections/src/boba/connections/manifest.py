"""Типы соединений как плагины: манифест пакета-владельца и реестр по entry points.

Пакет типа декларирует ConnectionTypeManifest в entry points группы
"boba.connections"; реестр собирается один раз на старте и отвечает за разбор
профилей из jsonb, схему форм и пробы.

Ошибки:
ConnectionTypesError — манифест плагина не соответствует контракту.
UnknownConnectionKind — в реестре нет типа с таким kind (пакет не установлен).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, ClassVar, Literal, Protocol

from pydantic import ValidationError
from pydantic.json_schema import models_json_schema

from boba.connections.base import ConnectionProfileBase
from boba.toolkit.failure import ValidationText

__all__ = [
    "ConnectionTypeManifest",
    "ConnectionTypes",
    "ConnectionTypesError",
    "ProbeHook",
    "UnknownConnectionKindError",
]


class ConnectionTypesError(Exception):
    """Манифест плагина не соответствует контракту."""


class UnknownConnectionKindError(ConnectionTypesError):
    """В реестре нет такого типа: пакет-владелец не установлен.

    Именуется видом соединения (строка kind) либо именем модели профиля —
    смотря что было на руках у вызывающего.
    """

    def __init__(self, kind: str, installed: Sequence[str]) -> None:
        msg = (
            f"connection type {kind!r} is not installed, "
            f"installed types: {list(installed)}"
        )
        super().__init__(msg)
        self.kind = kind
        self.installed = tuple(installed)


class ProbeHook(Protocol):
    """Проверка живого соединения по профилю с билетом вызова: текст об успехе.

    Ошибки реализация выпускает свои — границу к ProbeResult держит вызывающий.
    """

    async def __call__(self, profile: ConnectionProfileBase) -> str: ...


@dataclass(frozen=True)
class ConnectionTypeManifest:
    """Пакет-владелец описывает тип соединения целиком."""

    kind: str
    profile: type[ConnectionProfileBase]
    probe: ProbeHook


class ConnectionTypes:
    """Реестр установленных типов соединений: kind -> манифест."""

    GROUP: ClassVar[str] = "boba.connections"

    def __init__(self, table: Mapping[str, ConnectionTypeManifest]) -> None:
        self._table = dict(table)

    @classmethod
    def discover(cls) -> ConnectionTypes:
        """Реестр из entry points установленных пакетов."""
        table: dict[str, ConnectionTypeManifest] = {}
        for entry in entry_points(group=cls.GROUP):
            manifest = entry.load()
            if not isinstance(manifest, ConnectionTypeManifest):
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected a ConnectionTypeManifest, "
                    f"got {type(manifest).__name__}"
                )
                raise ConnectionTypesError(msg)

            if manifest.kind != entry.name:
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}) declares kind {manifest.kind!r}: "
                    "the entry point name must equal the kind"
                )
                raise ConnectionTypesError(msg)

            table[manifest.kind] = manifest

        return cls(table)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))

    def kind_of(self, profile: type[ConnectionProfileBase]) -> str:
        """Вид соединения по модели профиля: так его объявляет параметр тула.

        Ошибки:
        UnknownConnectionKindError — пакет-владелец этой модели не установлен.
        """
        for kind, manifest in self._table.items():
            if manifest.profile is profile:
                return kind

        raise UnknownConnectionKindError(profile.__name__, self.kinds())

    def manifest_of(self, kind: str) -> ConnectionTypeManifest:
        found = self._table.get(kind)
        if found is None:
            raise UnknownConnectionKindError(kind, self.kinds())

        return found

    def parse(self, raw: Mapping[str, Any]) -> ConnectionProfileBase:
        """Профиль из jsonb строки: модель выбирается по полю kind."""
        kind = raw.get("kind")
        if not isinstance(kind, str):
            msg = (
                "connection profile: expected a string field kind, "
                f"got kind={kind!r} among keys {sorted(raw)}"
            )
            raise ConnectionTypesError(msg)

        manifest = self.manifest_of(kind)
        try:
            return manifest.profile.model_validate(raw)
        except ValidationError as exc:
            # from None: в input_value разобранной строки ездят секреты,
            # наружу идёт только безопасный текст ошибок
            details = ValidationText.of(exc)
            msg = f"connection profile of kind {kind!r}: {details}"
            raise ConnectionTypesError(msg) from None

    def json_schema(self) -> dict[str, Any]:
        """Схема форм: oneOf по установленным типам с дискриминатором kind.

        Форма совпадает со схемой pydantic discriminated union: варианты
        ссылками в общий $defs плюс discriminator.mapping kind -> ссылка.
        Схемы моделей генерируются одним вызовом — одноимённые вложенные
        модели разных пакетов не перетирают друг друга.
        """
        ordered = self.kinds()
        inputs: list[tuple[type[ConnectionProfileBase], Literal["validation"]]] = []
        for kind in ordered:
            inputs.append((self._table[kind].profile, "validation"))

        refs, document = models_json_schema(inputs, ref_template="#/$defs/{model}")

        variants: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}
        for kind, entry in zip(ordered, inputs, strict=True):
            reference = str(refs[entry]["$ref"])
            variants.append({"$ref": reference})
            mapping[kind] = reference

        return {
            "oneOf": variants,
            "discriminator": {"propertyName": "kind", "mapping": mapping},
            "$defs": dict(document.get("$defs", {})),
        }
