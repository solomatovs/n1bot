"""Базовые DTO/Schema приложения: AppCoreConfig."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, Nullable, ParseBool, ParseString
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["AppCoreConfig"]


@dataclass(frozen=True)
class AppCoreConfig:
    """DTO AppCoreSection — кросс-слойные настройки приложения."""

    ssl_verify: bool
    log_level: str
    log_file: str | None

    SCHEMA: ClassVar[ObjectSchema[AppCoreConfig]]


AppCoreConfig.SCHEMA = ObjectSchema(
    description="Кросс-слойные настройки приложения: SSL/логирование.",
    fields=[
        FieldSpec(
            name="ssl_verify",
            coercer=ChainCoercer(Default(False), ParseBool()),
            description="Проверять ли TLS-сертификат у HTTPS-запросов.",
        ),
        FieldSpec(
            name="log_level",
            coercer=ChainCoercer(Default("INFO"), ParseString()),
            description="Уровень корневого логгера.",
        ),
        FieldSpec(
            name="log_file",
            coercer=Nullable(ParseString()),
            description="Путь к log-файлу. Пусто — логи в stderr.",
        ),
    ],
    factory=AppCoreConfig,
)
