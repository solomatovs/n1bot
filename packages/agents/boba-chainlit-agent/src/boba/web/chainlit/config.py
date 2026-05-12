"""DTO chainlit-приложения: ChainlitConfig."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.schema.coercion import (
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)

__all__ = ["ChainlitConfig"]


@dataclass(frozen=True)
class ChainlitConfig:
    """Параметры chainlit-приложения: server, runtime-root, LLM-модель, UI-overrides.

    UI-поля = None означает chainlit-дефолт (значение не пишется в overlay).
    """

    model: Annotated[
        str,
        "Конкретная LLM-модель для всех запросов из UI.",
        ParseString(),
    ]
    host: Annotated[
        str,
        "Адрес, на котором слушает chainlit-сервер.",
        ParseString(),
    ] = "127.0.0.1"
    port: Annotated[
        str,
        "Порт chainlit-сервера." \
        "Хранится строкой — bridge пишет напрямую в CHAINLIT_PORT env.",
        ParseString(),
    ] = "8501"
    root_path: Annotated[
        str,
        "HTTP root path под reverse-proxy. Пусто — chainlit на корне.",
        ParseString(),
    ] = ""
    auth_secret: Annotated[
        str | None,
        "Секрет для подписи user-session cookie. Если не задан — bootstrap "
        "сгенерирует случайный и сохранит в local/.auth_secret.",
        ParseString(),
    ] = None
    auth_username: Annotated[
        str,
        "Имя единственного пользователя (multi-user будет в отдельной [auth.users]).",
        ParseString(),
    ] = "admin"
    auth_password: Annotated[
        str,
        "Пароль единственного пользователя.",
        ParseString(),
    ] = "admin"  # noqa: S105
    headless: Annotated[
        str,
        "true — не пытаться открыть браузер при старте.",
        ParseString(),
    ] = "true"
    app_root: Annotated[
        str,
        "Директория chainlit runtime-state: .chainlit/config.toml, chainlit.md, "
        "public/, translations/. Не лежит в исходниках — вынесена в local/ "
        "(gitignored). Bridge ставит её в CHAINLIT_APP_ROOT.",
        ParseString(),
    ] = "./local/chainlit"

    ui_name: Annotated[
        str | None,
        "Заголовок чата в UI (chainlit [UI] name).",
        ParseString(),
    ] = None
    enable_telemetry: Annotated[
        bool | None,
        "Опт-аут chainlit-телеметрии ([project] enable_telemetry).",
        ParseBool(),
    ] = None
    upload_max_size_mb: Annotated[
        int | None,
        "Лимит размера загружаемого файла, MB "
        "([features.spontaneous_file_upload] max_size_mb).",
        ParseInt(),
    ] = None
    upload_max_files: Annotated[
        int | None,
        "Максимум файлов в одном сообщении "
        "([features.spontaneous_file_upload] max_files).",
        ParseInt(),
    ] = None
    upload_accept: Annotated[
        list[str] | None,
        "MIME-типы/расширения, разрешённые к загрузке "
        "([features.spontaneous_file_upload] accept).",
        ParseCsvList(),
    ] = None
    chat_session_pool_capacity: Annotated[
        int,
        "Сколько ChatSession держать в RAM одновременно (LRU eviction).",
        ParseInt(),
    ] = 32
