"""Конфиг chainlit-приложения: транспорт (chainlit-сервер) + профиль агента.

ChainlitConfig — единый конфиг chainlit-агента:
- chainlit-specific поля (host, port, auth_secret, app_root, …);
- nested profile: AgentProfile — профиль агента (логи, workspace, LLM,
  system prompt, model), подключается ссылкой profile = "${agent.<name>}".

Грузится в bootstrap (composition.py): build_app_config() -> bind(config,
"chainlit", ChainlitConfig). Секцию задаёт call-site, модель про путь не знает.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from boba.agent import AgentProfile

# переиспользуем конфиги авторизации из boba-chainlit2
from boba.chainlit2.chat.auth import (
    FixAuthConfig,
    KerberosAuthConfig,
    LdapAuthConfig,
)

__all__ = ["AuthConfig", "ChainlitConfig"]


AuthConfig = Annotated[
    FixAuthConfig | KerberosAuthConfig | LdapAuthConfig,
    Field(discriminator="type"),
]


class ChainlitConfig(BaseModel):
    """Параметры chainlit-приложения: server + профиль агента."""

    model_config = ConfigDict(extra="forbid")

    profile: AgentProfile = Field(description="agent profile")

    host: str = Field(
        default="127.0.0.1",
        description="Адрес, на котором слушает chainlit-сервер.",
    )
    port: int = Field(
        default=8501,
        description="Порт chainlit-сервера.",
    )
    url_prefix: str = Field(
        default="",
        description=("URL-prefix для HTTP-роутинга под reverse-proxy"),
    )
    auth_secret: str = Field(
        description=(
            "Секрет для подписи user-session cookie. Обязателен. "
            "Сгенерировать: `openssl rand -hex 32`"
        ),
        min_length=1,
    )
    headless: bool = Field(
        default=True,
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
    chat_session_pool_capacity: int = Field(
        default=32,
        description="Сколько ChatSession держать в RAM одновременно (LRU eviction).",
    )
    auth: list[AuthConfig] = Field(
        default_factory=list,
        description="Доступные способы авторизации (fix/ldap/kerberos).",
    )
