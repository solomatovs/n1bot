"""Типы соединений как плагины: манифест пакета-владельца и реестр по entry points.

Пакет типа декларирует ConnectionTypeManifest в entry points группы
"boba.connections"; реестр собирается один раз на старте и отвечает за разбор
профилей из jsonb, схему форм и пробы.

Ошибки:
ConnectionTypesError — манифест плагина не соответствует контракту.
UnknownConnectionKind — в реестре нет типа с таким kind (пакет не установлен).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol

from pydantic import ValidationError

from boba.connections.base import ConnectionProfileBase

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
    """В реестре нет типа с таким kind: пакет-владелец не установлен."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"connection type {kind!r} is not installed")
        self.kind = kind


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
                msg = f"entry point {entry.name!r}: not a ConnectionTypeManifest"
                raise ConnectionTypesError(msg)

            if manifest.kind != entry.name:
                msg = (
                    f"entry point {entry.name!r} declares kind "
                    f"{manifest.kind!r}: names must match"
                )
                raise ConnectionTypesError(msg)

            table[manifest.kind] = manifest

        return cls(table)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))

    def manifest_of(self, kind: str) -> ConnectionTypeManifest:
        found = self._table.get(kind)
        if found is None:
            raise UnknownConnectionKindError(kind)

        return found

    def parse(self, raw: Mapping[str, Any]) -> ConnectionProfileBase:
        """Профиль из jsonb строки: модель выбирается по полю kind."""
        kind = raw.get("kind")
        if not isinstance(kind, str):
            msg = f"connection profile has no kind: {sorted(raw)}"
            raise ConnectionTypesError(msg)

        manifest = self.manifest_of(kind)
        try:
            return manifest.profile.model_validate(raw)
        except ValidationError as exc:
            msg = f"connection profile of kind {kind!r} does not validate"
            raise ConnectionTypesError(msg) from exc

    def json_schema(self) -> dict[str, Any]:
        """Схема форм: oneOf по установленным типам с дискриминатором kind.

        Форма совпадает со схемой pydantic discriminated union: варианты
        ссылками в общий $defs плюс discriminator.mapping kind -> ссылка.
        """
        defs: dict[str, Any] = {}
        variants: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}

        for kind in self.kinds():
            model = self._table[kind].profile
            schema = model.model_json_schema(ref_template="#/$defs/{model}")
            defs.update(schema.pop("$defs", {}))

            name = model.__name__
            defs[name] = schema
            reference = f"#/$defs/{name}"
            variants.append({"$ref": reference})
            mapping[kind] = reference

        return {
            "oneOf": variants,
            "discriminator": {"propertyName": "kind", "mapping": mapping},
            "$defs": defs,
        }
