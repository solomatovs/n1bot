"""Chainlit-server конфиг как :class:`ConfigSection`.

:class:`ChainlitSection` объявляет поля декларативно (через
:class:`FieldSpec`) и собирает типизированный :class:`ChainlitConfig`.
Регистрируется в :class:`ConfigFactory` через entry-point group
``boba.config_sections`` (см. ``pyproject.toml``).

Bootstrap (:mod:`boba_chainlit.__main__`) собирает бандл и через
``bundle.section(ChainlitSection)`` получает значения, после чего
прокидывает их в ``CHAINLIT_*`` env для самой библиотеки chainlit
(она читает env при импорте).

Конвенция имён та же, что у app-секций:
``ConfigKey("chainlit","host")`` → env ``BOBA_CHAINLIT_HOST``,
TOML ``[chainlit] host``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.domain.core.config import (
    ChainedConfigResolver,
    ConfigKey,
    ConfigSection,
    FieldSpec,
)
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import (
    ChainConverter,
    Default,
    Nullable,
    ParseCsvList,
    ParseString,
)

__all__ = ["ChainlitConfig", "ChainlitSection"]


@dataclass(frozen=True)
class ChainlitConfig:
    """Параметры chainlit-сервера + список доступных в UI моделей.

    ``host``/``port``/``root_path``/``auth_secret``/``headless`` — bridge
    к одноимённым ``CHAINLIT_*`` env-переменным (значения хранятся
    строкой, потому что bridge пишет напрямую в env, а chainlit-библиотека
    парсит сама).

    ``models`` — список LLM-моделей для UI ChatSettings. Пустой список —
    UI не сможет выбрать модель и кинет ошибку при старте сессии.
    """

    host: str
    port: str
    root_path: str
    auth_secret: str | None
    headless: str
    models: list[str]


class ChainlitSection(ConfigSection[ChainlitConfig]):
    """Секция chainlit-server. Регистрируется через entry-point
    ``boba.config_sections``.
    """

    id: ClassVar[StrId] = StrId("chainlit")

    HOST: FieldSpec[str] = FieldSpec(
        key=ConfigKey("chainlit", "host"),
        converter=ChainConverter(Default("127.0.0.1"), ParseString()),
        description="Адрес, на котором слушает chainlit-сервер.",
    )
    PORT: FieldSpec[str] = FieldSpec(
        key=ConfigKey("chainlit", "port"),
        converter=ChainConverter(Default("8501"), ParseString()),
        description="Порт chainlit-сервера. Хранится строкой — bridge "
        "пишет напрямую в CHAINLIT_PORT env.",
    )
    ROOT_PATH: FieldSpec[str] = FieldSpec(
        key=ConfigKey("chainlit", "root_path"),
        converter=ChainConverter(Default(""), ParseString()),
        description="HTTP root path под reverse-proxy. Пусто — chainlit на корне.",
    )
    AUTH_SECRET: FieldSpec[str | None] = FieldSpec(
        key=ConfigKey("chainlit", "auth_secret"),
        converter=Nullable(ParseString()),
        description="Секрет для подписи user-session cookie. Если не "
        "задан — chainlit генерит сам.",
    )
    HEADLESS: FieldSpec[str] = FieldSpec(
        key=ConfigKey("chainlit", "headless"),
        converter=ChainConverter(Default("true"), ParseString()),
        description="``true`` — не пытаться открыть браузер при старте.",
    )
    MODELS: FieldSpec[list[str]] = FieldSpec(
        key=ConfigKey("chainlit", "models"),
        converter=ChainConverter(Default([]), ParseCsvList()),
        description="CSV/TOML-list LLM-моделей, выбираемых пользователем "
        "в ChatSettings.",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (
        HOST,
        PORT,
        ROOT_PATH,
        AUTH_SECRET,
        HEADLESS,
        MODELS,
    )

    def build(self, resolver: ChainedConfigResolver) -> ChainlitConfig:
        return ChainlitConfig(
            host=self.HOST.read(resolver),
            port=self.PORT.read(resolver),
            root_path=self.ROOT_PATH.read(resolver),
            auth_secret=self.AUTH_SECRET.read(resolver),
            headless=self.HEADLESS.read(resolver),
            models=self.MODELS.read(resolver),
        )
