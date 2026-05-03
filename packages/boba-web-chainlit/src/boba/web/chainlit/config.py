"""Конфиг chainlit-приложения как единая ConfigSection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.config import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.validators import (
    ChainConverter,
    Default,
    Nullable,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)

from boba.patterns import StrId

__all__ = ["ChainlitConfig", "ChainlitSection"]


@dataclass(frozen=True)
class ChainlitConfig:
    """DTO chainlit-приложения; UI-поля = None означает chainlit-дефолт."""

    host: str
    port: str
    root_path: str
    auth_secret: str | None
    headless: str
    app_root: str
    models: list[str]

    ui_name: str | None
    enable_telemetry: bool | None
    upload_max_size_mb: int | None
    upload_max_files: int | None
    upload_accept: list[str] | None


class ChainlitSection(ConfigSection[ChainlitConfig]):
    """Секция конфига chainlit-приложения."""

    id: ClassVar[StrId] = StrId("chainlit")
    namespace: ClassVar[tuple[str, ...]] = ("chainlit",)

    schema: ClassVar[ObjectSchema[ChainlitConfig]] = ObjectSchema(
        description="Параметры chainlit-приложения: server, runtime-root, "
        "список моделей, UI-overrides.",
        fields=[
            # ── server ─────────────────────────────────────────────
            FieldSpec(
                name="host",
                converter=ChainConverter(Default("127.0.0.1"), ParseString()),
                description="Адрес, на котором слушает chainlit-сервер.",
            ),
            FieldSpec(
                name="port",
                converter=ChainConverter(Default("8501"), ParseString()),
                description="Порт chainlit-сервера. Хранится строкой — bridge "
                "пишет напрямую в CHAINLIT_PORT env.",
            ),
            FieldSpec(
                name="root_path",
                converter=ChainConverter(Default(""), ParseString()),
                description="HTTP root path под reverse-proxy. Пусто — "
                "chainlit на корне.",
            ),
            FieldSpec(
                name="auth_secret",
                converter=Nullable(ParseString()),
                description="Секрет для подписи user-session cookie. "
                "Если не задан — chainlit генерит сам.",
            ),
            FieldSpec(
                name="headless",
                converter=ChainConverter(Default("true"), ParseString()),
                description="true — не пытаться открыть браузер при старте.",
            ),
            FieldSpec(
                name="app_root",
                converter=ChainConverter(Default("./local/chainlit"), ParseString()),
                description="Директория chainlit runtime-state: "
                ".chainlit/config.toml, chainlit.md, public/, "
                "translations/. Не лежит в исходниках — вынесена в "
                "local/ (gitignored). Bridge ставит её в "
                "CHAINLIT_APP_ROOT.",
            ),
            FieldSpec(
                name="models",
                converter=ChainConverter(Default([]), ParseCsvList()),
                description="CSV/TOML-list LLM-моделей, выбираемых "
                "пользователем в ChatSettings.",
            ),
            # ── UI overrides (None = не трогать chainlit-дефолт) ───
            FieldSpec(
                name="ui_name",
                converter=Nullable(ParseString()),
                description="Заголовок чата в UI (chainlit [UI] name).",
            ),
            FieldSpec(
                name="enable_telemetry",
                converter=Nullable(ParseBool()),
                description="Опт-аут chainlit-телеметрии ([project] enable_telemetry).",
            ),
            FieldSpec(
                name="upload_max_size_mb",
                converter=Nullable(ParseInt()),
                description="Лимит размера загружаемого файла, MB "
                "([features.spontaneous_file_upload] max_size_mb).",
            ),
            FieldSpec(
                name="upload_max_files",
                converter=Nullable(ParseInt()),
                description="Максимум файлов в одном сообщении "
                "([features.spontaneous_file_upload] max_files).",
            ),
            FieldSpec(
                name="upload_accept",
                converter=Nullable(ParseCsvList()),
                description="MIME-типы/расширения, разрешённые к загрузке "
                "([features.spontaneous_file_upload] accept).",
            ),
        ],
        factory=ChainlitConfig,
    )
