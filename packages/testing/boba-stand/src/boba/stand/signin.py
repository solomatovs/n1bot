"""Сборка входов для стендов: профили стенда и SignInAssembly над живым
каталогом. Тест — точка bootstrap своего стенда, реализации провайдеров
собираются здесь, а не внутри входов.

Ошибки: свои не выпускает.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import SecretStr

from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.ldap import Ldap3Directory
from boba.llm.http.openai import OpenAiProvider
from boba.runtime.signin import SignInAssembly
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import BearerAuth, HttpConnection

__all__ = ["SignInStand"]


class SignInStand:
    """Профили стенда и сборка входов по конфигу [auth] стенда."""

    PROFILE: ClassVar[str] = "general"
    """Единственный профиль стенда: виден всем ролям, выбирается по умолчанию."""

    FAKE_HOST: ClassVar[str] = "fake-llm"
    """Хост провайдера стенда: запросы к нему не уходят, профилю нужен адрес."""

    @classmethod
    def provider(cls) -> OpenAiProvider:
        """Провайдер профиля стенда: openai-совместимый endpoint фейкового хоста."""
        return OpenAiProvider(
            kind="openai",
            connection=HttpConnection(
                host=cls.FAKE_HOST,
                path="/v1",
                auth=BearerAuth(method="bearer", token=SecretStr("k")),
            ),
            transport=HttpTransportConfig(),
        )

    @classmethod
    def profile(
        cls, *, default: bool = True, roles: list[str] | None = None
    ) -> ChatProfileConfig:
        if roles is None:
            roles = ["*"]

        return ChatProfileConfig.model_validate(
            {
                "display_name": "Stand",
                "description": "stand profile",
                "default": default,
                "roles": roles,
                "tools": ["echo"],
                "provider": cls.provider(),
                "model": "fake",
                "system_prompt": "stand",
            }
        )

    @classmethod
    def profiles(cls, roles: list[str] | None = None) -> ChatProfiles:
        return ChatProfiles({cls.PROFILE: cls.profile(roles=roles)})

    @classmethod
    def assembly(cls, profiles: ChatProfiles | None = None) -> SignInAssembly:
        if profiles is None:
            profiles = cls.profiles()

        return SignInAssembly(Ldap3Directory(), profiles)
