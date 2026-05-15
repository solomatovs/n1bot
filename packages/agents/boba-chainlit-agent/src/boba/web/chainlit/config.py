"""DTO chainlit-приложения: ChainlitConfig.

Источники (priority high → low):
  1. init-kwargs (`ChainlitConfig(model="...")`).
  2. env: `BOBA_CHAINLIT__<FIELD>` — поля плоские, sub-моделей нет, поэтому
     redistribute-валидатор работает как no-op.
  3. TOML: секция `[chainlit]` в файле `$BOBA_CONFIG_PATH`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["ChainlitConfig"]


def _csv_to_list(v: Any) -> Any:
    """CSV-строка → list[str]; list/None — без изменений."""
    if isinstance(v, str):
        return [item.strip() for item in v.split(",") if item.strip()]
    return v


StringList = Annotated[list[str], BeforeValidator(_csv_to_list)]


class ChainlitConfig(BobaFlatSettings):
    """Параметры chainlit-приложения: server, runtime-root, LLM-модель, UI-overrides.

    UI-поля = `None` означает «использовать chainlit-дефолт» (не пишется в overlay).
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_CHAINLIT__",
        boba_toml_section="chainlit",
    )

    model: str = Field(
        description="Конкретная LLM-модель для всех запросов из UI.",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Адрес, на котором слушает chainlit-сервер.",
    )
    port: str = Field(
        default="8501",
        description=(
            "Порт chainlit-сервера. Хранится строкой — bridge пишет "
            "напрямую в CHAINLIT_PORT env."
        ),
    )
    root_path: str = Field(
        default="",
        description="HTTP root path под reverse-proxy. Пусто — chainlit на корне.",
    )
    auth_secret: str | None = Field(
        default=None,
        description=(
            "Секрет для подписи user-session cookie. Если не задан — "
            "bootstrap сгенерирует случайный и сохранит в local/.auth_secret."
        ),
    )
    auth_username: str = Field(
        default="admin",
        description=(
            "Имя единственного пользователя "
            "(multi-user будет в отдельной [auth.users])."
        ),
    )
    auth_password: str = Field(
        default="admin",
        description="Пароль единственного пользователя.",
    )
    headless: str = Field(
        default="true",
        description="true — не пытаться открыть браузер при старте.",
    )
    app_root: str = Field(
        default="./local/chainlit",
        description=(
            "Директория chainlit runtime-state: .chainlit/config.toml, "
            "chainlit.md, public/, translations/. Не лежит в исходниках — "
            "вынесена в local/ (gitignored). Bridge ставит её в CHAINLIT_APP_ROOT."
        ),
    )
    ui_name: str | None = Field(
        default=None,
        description="Заголовок чата в UI (chainlit [UI] name).",
    )
    enable_telemetry: bool | None = Field(
        default=None,
        description="Опт-аут chainlit-телеметрии ([project] enable_telemetry).",
    )
    upload_max_size_mb: int | None = Field(
        default=None,
        description=(
            "Лимит размера загружаемого файла, MB "
            "([features.spontaneous_file_upload] max_size_mb)."
        ),
    )
    upload_max_files: int | None = Field(
        default=None,
        description=(
            "Максимум файлов в одном сообщении "
            "([features.spontaneous_file_upload] max_files)."
        ),
    )
    upload_accept: StringList | None = Field(
        default=None,
        description=(
            "MIME-типы/расширения, разрешённые к загрузке "
            "([features.spontaneous_file_upload] accept)."
        ),
    )
    chat_session_pool_capacity: int = Field(
        default=32,
        description="Сколько ChatSession держать в RAM одновременно (LRU eviction).",
    )
