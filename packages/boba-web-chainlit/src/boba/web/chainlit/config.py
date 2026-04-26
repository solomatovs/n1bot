"""Конфиг chainlit-приложения как единая ConfigSection.

Все поля — server-параметры (host/port/root_path/auth_secret/headless),
рантайм-расположение (app_root), список моделей для UI ChatSettings и
UI-overrides для .chainlit/config.toml (title, telemetry, лимиты
загрузки) — лежат в одной секции с namespace ("chainlit",). DTO-имена
== TOML/env-имена: [chainlit] ui_name ↔ env BOBA_CHAINLIT_UI_NAME
↔ ChainlitConfig.ui_name. Никаких оператор-DTO mapping'ов.

Bootstrap (__main__) собирает бандл и через
bundle.section(ChainlitSection) получает значения, после чего:

1. через bridge_chainlit_env прокидывает server-поля в
   CHAINLIT_* env (chainlit-библиотека читает их при импорте);
2. через UIOverrideTomlConverter рендерит UI-поля в
   app_root/.chainlit/config.toml (chainlit смотрит TOML только
   при старте сервера).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.domain.core.config import (
    ConfigSection,
    FieldSpec,
    ObjectSchema,
)
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import (
    ChainConverter,
    Default,
    Nullable,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)

__all__ = ["ChainlitConfig", "ChainlitSection"]


@dataclass(frozen=True)
class ChainlitConfig:
    """Параметры chainlit-приложения.

    host/port/root_path/auth_secret/headless —
    server-параметры, прокидываются в одноимённые CHAINLIT_* env.
    Хранятся строкой — chainlit-библиотека парсит сама.

    app_root — директория chainlit runtime-state (.chainlit/,
    chainlit.md, public/, translations/). Прокидывается в
    CHAINLIT_APP_ROOT. Лежит вне исходников (по умолчанию —
    ./local/chainlit).

    models — список LLM-моделей для UI ChatSettings. Пустой список
    — UI не сможет выбрать модель и кинет ошибку при старте сессии.

    UI-overrides (ui_name, enable_telemetry,
    upload_max_size_mb/upload_max_files/upload_accept) —
    рендерятся в app_root/.chainlit/config.toml (chainlit для UI
    env не смотрит, только TOML). None означает «не трогать
    chainlit-дефолт».
    """

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
    """Секция chainlit-приложения. Регистрируется через entry-point
    boba.config_sections.
    """

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
                description="Опт-аут chainlit-телеметрии "
                "([project] enable_telemetry).",
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
