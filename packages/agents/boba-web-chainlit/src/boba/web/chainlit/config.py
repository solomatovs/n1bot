"""DTO chainlit-приложения: ChainlitConfig + SCHEMA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    Nullable,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["ChainlitConfig"]


@dataclass(frozen=True)
class ChainlitConfig:
    """DTO chainlit-приложения; UI-поля = None означает chainlit-дефолт."""

    host: str
    port: str
    root_path: str
    auth_secret: str | None
    headless: str
    app_root: str
    model: str

    ui_name: str | None
    enable_telemetry: bool | None
    upload_max_size_mb: int | None
    upload_max_files: int | None
    upload_accept: list[str] | None

    SCHEMA: ClassVar[ObjectSchema[ChainlitConfig]]


ChainlitConfig.SCHEMA = ObjectSchema(
    description=(
        "Параметры chainlit-приложения: server, runtime-root, "
        "LLM-модель, UI-overrides."
    ),
    fields=[
        FieldSpec(
            name="host",
            coercer=ChainCoercer(Default("127.0.0.1"), ParseString()),
            description="Адрес, на котором слушает chainlit-сервер.",
        ),
        FieldSpec(
            name="port",
            coercer=ChainCoercer(Default("8501"), ParseString()),
            description=(
                "Порт chainlit-сервера. Хранится строкой — bridge "
                "пишет напрямую в CHAINLIT_PORT env."
            ),
        ),
        FieldSpec(
            name="root_path",
            coercer=ChainCoercer(Default(""), ParseString()),
            description=(
                "HTTP root path под reverse-proxy. Пусто — chainlit на корне."
            ),
        ),
        FieldSpec(
            name="auth_secret",
            coercer=Nullable(ParseString()),
            description=(
                "Секрет для подписи user-session cookie. Если не задан — "
                "chainlit генерит сам."
            ),
        ),
        FieldSpec(
            name="headless",
            coercer=ChainCoercer(Default("true"), ParseString()),
            description="true — не пытаться открыть браузер при старте.",
        ),
        FieldSpec(
            name="app_root",
            coercer=ChainCoercer(Default("./local/chainlit"), ParseString()),
            description=(
                "Директория chainlit runtime-state: .chainlit/config.toml, "
                "chainlit.md, public/, translations/. Не лежит в исходниках — "
                "вынесена в local/ (gitignored). Bridge ставит её в "
                "CHAINLIT_APP_ROOT."
            ),
        ),
        FieldSpec(
            name="model",
            coercer=ParseString(),
            description="Конкретная LLM-модель для всех запросов из UI.",
        ),
        FieldSpec(
            name="ui_name",
            coercer=Nullable(ParseString()),
            description="Заголовок чата в UI (chainlit [UI] name).",
        ),
        FieldSpec(
            name="enable_telemetry",
            coercer=Nullable(ParseBool()),
            description=(
                "Опт-аут chainlit-телеметрии ([project] enable_telemetry)."
            ),
        ),
        FieldSpec(
            name="upload_max_size_mb",
            coercer=Nullable(ParseInt()),
            description=(
                "Лимит размера загружаемого файла, MB "
                "([features.spontaneous_file_upload] max_size_mb)."
            ),
        ),
        FieldSpec(
            name="upload_max_files",
            coercer=Nullable(ParseInt()),
            description=(
                "Максимум файлов в одном сообщении "
                "([features.spontaneous_file_upload] max_files)."
            ),
        ),
        FieldSpec(
            name="upload_accept",
            coercer=Nullable(ParseCsvList()),
            description=(
                "MIME-типы/расширения, разрешённые к загрузке "
                "([features.spontaneous_file_upload] accept)."
            ),
        ),
    ],
    factory=ChainlitConfig,
)
