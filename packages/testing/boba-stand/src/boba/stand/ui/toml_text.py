"""Запись конфига обратно в TOML: стенду нужен свой файл поверх рабочего.

В конфиге приложения после tomllib остаются только скаляры, строки и списки
строк, поэтому writer покрывает ровно их. Ошибки: TomlTypeError — значение
такого типа стенд писать не умеет.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

__all__ = ["TomlText", "TomlTypeError"]


class TomlTypeError(TypeError):
    """Значение не сериализуется в TOML этим writer'ом."""


class TomlText:
    """Сериализация Mapping в текст TOML."""

    @classmethod
    def dumps(cls, data: Mapping[str, object]) -> str:
        lines = list(cls._table(data, path=()))
        return "\n".join(lines) + "\n"

    @classmethod
    def _table(cls, data: Mapping[str, object], path: tuple[str, ...]) -> Iterator[str]:
        scalars: list[tuple[str, object]] = []
        tables: list[tuple[str, Mapping[str, object]]] = []
        for key, value in data.items():
            if isinstance(value, Mapping):
                tables.append((key, value))
                continue

            scalars.append((key, value))

        if path:
            yield f"[{'.'.join(cls._key(part) for part in path)}]"

        for key, value in scalars:
            yield f"{cls._key(key)} = {cls._value(value)}"

        if scalars or path:
            yield ""

        for key, table in tables:
            yield from cls._table(table, path=(*path, key))

    @classmethod
    def _value(cls, value: object) -> str:
        if isinstance(value, bool):
            if value:
                return "true"

            return "false"

        if isinstance(value, str):
            return cls._string(value)

        if isinstance(value, (int, float)):
            return repr(value)

        if isinstance(value, Sequence):
            items = [cls._value(item) for item in value]
            return f"[{', '.join(items)}]"

        raise TomlTypeError(f"unsupported value type: {type(value).__name__}")

    @staticmethod
    def _string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
        escaped = escaped.replace("\t", "\\t")
        return f'"{escaped}"'

    @staticmethod
    def _key(key: str) -> str:
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        )
        if key and set(key) <= allowed:
            return key

        escaped = key.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
