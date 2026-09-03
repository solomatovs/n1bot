"""Переиспользуемые pydantic-типы boba: конфиг-вход (CSV) и LLM-вход инструментов."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, SecretStr

__all__ = [
    "LLMStringList",
    "SecretReveal",
    "SecretRevealing",
    "StringList",
    "StringLists",
]


class SecretReveal:
    """Раскрытие SecretStr в готовом json-дампе модели обходом её значений.

    Pydantic дампит SecretStr звёздочками и контекст не смотрит, поэтому
    после дампа значения модели обходятся параллельно с дампом, и каждое
    голое SecretStr на любой глубине заменяется открытой строкой. Поле с
    явным field_serializer не трогается: у него своя политика (пароль
    kerberos не уезжает никогда, keytab роняет дамп).
    """

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
    """Конфиг инструмента, умеющий дамп с раскрытыми секретами.

    Дамп едет только каналом injected песочного вызова и обязан собираться
    обратно в тот же тип: SecretStr оживает из открытой строки. Голые
    SecretStr раскрывает SecretReveal; поля с явным field_serializer читают
    тот же ключ контекста и решают сами.
    """

    REVEAL_CONTEXT: ClassVar[str] = "reveal_secrets"

    def revealed(self) -> dict[str, object]:
        dumped = self.model_dump(mode="json", context={self.REVEAL_CONTEXT: True})
        SecretReveal.apply(self, dumped)

        return dumped


class StringLists:
    """Списки строк из конфига (CSV) и из ответа LLM (json-список, CSV, одно значение).

    CSV читается модулем csv: значение с запятой берётся в кавычки, а не рвётся.
    """

    JSON_START: ClassVar[str] = "["

    @classmethod
    def of_csv(cls, value: Any) -> Any:
        """CSV-строка -> list[str]; не строка — без изменений."""
        if not isinstance(value, str):
            return value

        return cls._csv(value)

    @classmethod
    def of_llm(cls, value: Any) -> Any:
        """LLM-вход: ["a","b"], '["a","b"]', "a,b", "a" -> list[str]."""
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text.startswith(cls.JSON_START):
            return cls._csv(text)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value

        if not isinstance(parsed, list):
            return value

        items: list[str] = []
        for item in parsed:
            items.append(str(item))

        return items

    @classmethod
    def _csv(cls, text: str) -> list[str]:
        items: list[str] = []
        for row in csv.reader(text.splitlines(), skipinitialspace=True):
            for item in row:
                stripped = item.strip()
                if not stripped:
                    continue

                items.append(stripped)

        return items


StringList = Annotated[list[str], BeforeValidator(StringLists.of_csv)]
"""list[str] с CSV-парсингом строкового входа ("a,b,c" -> ["a","b","c"])."""


LLMStringList = Annotated[list[str], BeforeValidator(StringLists.of_llm)]
"""list[str] с LLM-нормализацией входа: ["a","b"], '["a","b"]', "a,b", "a"."""
