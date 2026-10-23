"""Раскрытие SecretStr в json-дампе модели: конфиг едет каналом в тот же тип.

Ошибки: не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, SecretStr

__all__ = [
    "SecretReveal",
    "SecretRevealing",
]


class SecretReveal:
    """Раскрытие SecretStr в готовом json-дампе модели обходом её значений.

    Pydantic дампит SecretStr звёздочками и контекст не смотрит, поэтому
    после дампа значения модели обходятся параллельно с дампом, и каждое
    голое SecretStr на любой глубине заменяется открытой строкой. Поле с
    явным field_serializer не трогается: у него своя политика (пароль
    kerberos не уезжает никогда, keytab роняет дамп).
    """

    REVEAL_CONTEXT: ClassVar[str] = "reveal_secrets"
    """Ключ контекста дампа; его же читают field_serializer инфра-профилей."""

    @classmethod
    def dumped(cls, model: BaseModel) -> dict[str, object]:
        """JSON-дамп модели с раскрытыми секретами: контекст плюс обход значений.

        Работает с любой моделью, а не только с наследником SecretRevealing:
        профили соединений приходят из пакетов-владельцев и своей базы не имеют.
        """
        dump = model.model_dump(mode="json", context={cls.REVEAL_CONTEXT: True})
        cls.apply(model, dump)

        return dump

    @classmethod
    def apply(cls, model: BaseModel, dumped: object) -> None:
        if not isinstance(dumped, dict):
            return

        explicit = cls._explicit_fields(type(model))

        for name in type(model).model_fields:
            if name in explicit:
                continue

            if name not in dumped:
                continue

            cls._reveal(getattr(model, name), dumped, name)

    @classmethod
    def _reveal(cls, value: object, parent: Any, key: object) -> None:
        if isinstance(value, SecretStr):
            parent[key] = value.get_secret_value()
            return

        if isinstance(value, BaseModel):
            cls.apply(value, parent[key])
            return

        if isinstance(value, Mapping):
            cls._reveal_mapping(value, parent[key])
            return

        if isinstance(value, list | tuple):
            cls._reveal_sequence(value, parent[key])

    @classmethod
    def _reveal_mapping(cls, value: Mapping[object, object], target: object) -> None:
        if not isinstance(target, dict):
            return

        for key, nested in value.items():
            if key not in target:
                continue

            cls._reveal(nested, target, key)

    @classmethod
    def _reveal_sequence(cls, value: Sequence[object], target: object) -> None:
        if not isinstance(target, list):
            return

        if len(target) != len(value):
            return

        for index, nested in enumerate(value):
            cls._reveal(nested, target, index)

    @staticmethod
    def _explicit_fields(klass: type[BaseModel]) -> frozenset[str]:
        names: set[str] = set()
        for decorator in klass.__pydantic_decorators__.field_serializers.values():
            names.update(decorator.info.fields)

        return frozenset(names)


class SecretRevealing(BaseModel):
    """Модель, умеющая дамп с раскрытыми секретами.

    Дамп едет только закрытым каналом (injected песочного вызова, файл
    настроек процесса) и обязан собираться обратно в тот же тип: SecretStr
    оживает из открытой строки. Голые SecretStr раскрывает SecretReveal;
    поля с явным field_serializer читают тот же ключ контекста и решают сами.
    """

    REVEAL_CONTEXT: ClassVar[str] = SecretReveal.REVEAL_CONTEXT

    def revealed(self) -> dict[str, object]:
        return SecretReveal.dumped(self)
