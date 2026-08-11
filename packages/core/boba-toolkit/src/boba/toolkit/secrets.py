"""Единственная точка раскрытия секретов: json-дамп запроса в канал tool_args.

Профиль соединения раскрывает пароль по ключу контекста REVEAL, обычная модель —
полями SecretStr; обход рекурсивный, поэтому вложенный профиль (auth транспорта,
connection индексации) раскрывается тем же вызовом.

Ошибок наружу не выпускает: сериализация модели своих отказов не имеет.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, SecretStr

__all__ = ["SecretDump"]


class SecretDump:
    """JSON-дамп модели с раскрытыми секретами; зовётся только из field_serializer.

    tool_args песочницы — доверенный канал (pipe не виден ни в ps, ни в /proc),
    и раскрытие живёт здесь; любой другой дамп оставляет секрет замаскированным.
    """

    REVEAL: ClassVar[str] = "reveal_secrets"
    """Ключ контекста сериализации: профиль соединения отдаёт пароль только с ним."""

    @classmethod
    def of(cls, model: BaseModel) -> dict[str, Any]:
        dumped = model.model_dump(mode="json", context={cls.REVEAL: True})

        return cls._revealed(model, dumped)

    @classmethod
    def _revealed(cls, model: BaseModel, dumped: dict[str, Any]) -> dict[str, Any]:
        for name in type(model).model_fields:
            if name not in dumped:
                continue

            value = getattr(model, name)

            if isinstance(value, SecretStr):
                dumped[name] = value.get_secret_value()
                continue

            if not isinstance(value, BaseModel):
                continue

            nested = dumped[name]
            if not isinstance(nested, dict):
                continue

            dumped[name] = cls._revealed(value, nested)

        return dumped
