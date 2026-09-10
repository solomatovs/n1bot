"""Сборка входов для стендов: профили стенда и SignInAssembly над живым
каталогом. Тест — точка bootstrap своего стенда, реализации провайдеров
собираются здесь, а не внутри входов.

Ошибки: свои не выпускает.
"""

from __future__ import annotations

from typing import ClassVar

from boba.chat.http import HttpConfig
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.chat.provider import OpenAiChatConfig
from boba.ldap import Ldap3Directory
from boba.runtime.signin import SignInAssembly

__all__ = ["SignInStand"]


class SignInStand:
    """Профили стенда и сборка входов по конфигу [auth] стенда."""

    PROFILE: ClassVar[str] = "general"
    """Единственный профиль стенда: виден всем ролям, выбирается по умолчанию."""

    @classmethod
    def profiles(cls, roles: list[str] | None = None) -> ChatProfiles:
        if roles is None:
            roles = ["*"]

        profile = ChatProfileConfig.model_validate(
            {
                "display_name": "Stand",
                "description": "stand profile",
                "default": True,
                "roles": roles,
                "tools": ["echo"],
                "provider": OpenAiChatConfig(
                    kind="openai",
                    http=HttpConfig(),
                    base_url="https://fake-llm/v1",
                    api_key="k",
                ),
                "model": "fake",
                "system_prompt": "stand",
            }
        )

        return ChatProfiles({cls.PROFILE: profile})

    @classmethod
    def assembly(cls, profiles: ChatProfiles | None = None) -> SignInAssembly:
        if profiles is None:
            profiles = cls.profiles()

        return SignInAssembly(Ldap3Directory(), profiles)
