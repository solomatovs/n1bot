"""Профили чата: модели секций конфига и правила выбора профиля под роли
субъекта.

Ошибки:
RefusalError — профиль чата не выбран или недоступен ролям пользователя.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, RootModel

from boba.access import ProfileGrant, RoleConfig
from boba.identity.errors import RefusalError
from boba.identity.signin import ProfileCatalog, SignInMetadata
from boba.llm.providers import ChatModelConfig
from boba.toolkit.types import StringList

__all__ = [
    "ChatProfileConfig",
    "ChatProfiles",
    "ChatSettings",
    "ModelLimits",
    "ProfileRefusal",
    "ProfilesSection",
    "ReasoningReturn",
    "RolesSection",
    "SelectedProfile",
]

logger = logging.getLogger(__name__)


class ChatSettings(ChatModelConfig):
    """Обращение к чат-модели с системным промптом: провайдер, модель, сэмплинг
    и промпт. Так описан профиль чата."""

    system_prompt: str = Field(
        default="",
        description="Системный промпт по умолчанию",
    )


class ReasoningReturn(StrEnum):
    """Возврат модели её рассуждений прошлых ответов: значения — ключ
    `reasoningReturn` профиля модели в файле настроек агента."""

    NEVER = "never"
    TURN = "turn"
    ALWAYS = "always"


class ModelLimits(BaseModel):
    """Пределы модели: окно контекста, потолок ответа и возврат рассуждений.
    Секция [model_limits.<имя>] конфига, общая у профилей одной модели:
    профиль берёт её целиком ссылкой `limits = "${model_limits.<имя>}"`.
    Профиль модели агента собирается из неё и профиля чата."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window: int = Field(gt=0, description="Окно контекста модели, токены.")
    max_output_tokens: int = Field(gt=0, description="Потолок ответа модели.")
    reasoning_return: ReasoningReturn = Field(
        description=(
            "Возвращать ли модели её рассуждения прошлых ответов: never, turn "
            "(в пределах хода — так требуют thinking-модели вроде deepseek при "
            "вызове инструмента), always."
        )
    )


class ChatProfileConfig(ChatSettings, ProfileGrant):
    """Секция [profiles.<name>]: режим работы бота, выбираемый пользователем."""

    limits: ModelLimits = Field(
        description="Пределы модели профиля ссылкой `${model_limits.<имя>}`."
    )

    display_name: Annotated[
        str,
        Field(description="Название профиля в интерфейсе выбора."),
    ]

    description: Annotated[
        str,
        Field(description="Описание профиля под названием; markdown."),
    ]

    icon: str = Field(
        default="",
        description="Путь к иконке профиля; пусто — интерфейс рисует без неё.",
    )

    default: bool = Field(
        default=False,
        description="Профиль, предвыбранный в интерфейсе; ровно один в конфиге.",
    )

    mcp: StringList = Field(
        default=[],
        description=(
            "MCP-серверы профиля: имена секций [mcp.servers.<имя>]. Они "
            "подключаются сессии при выборе профиля, и отключить их "
            "пользователь не может; набор инструментов решает сам сервер."
        ),
    )


class ProfilesSection(RootModel[dict[str, ChatProfileConfig]]):
    """Секция [profiles] верхнего уровня: профили чата по имени."""


class RolesSection(RootModel[dict[str, RoleConfig]]):
    """Секция [roles] верхнего уровня: права ролей по имени."""


class ProfileRefusal(StrEnum):
    """Виды отказа выбора профиля чата."""

    NO_PROFILE_ACCESS = "no_profile_access"
    PROFILE_NOT_ALLOWED = "profile_not_allowed"
    PROFILE_NOT_SELECTED = "profile_not_selected"


class SelectedProfile(BaseModel):
    """Профиль текущей сессии: имя секции и её настройки."""

    model_config = ConfigDict(frozen=True)

    name: str
    config: ChatProfileConfig


class ChatProfiles(ProfileCatalog):
    """Реестр профилей чата: набор, выданный входом, и выбор профиля сессии.
    Реализация ProfileCatalog для провайдеров профилей входа.

    Какие профили доступны, решают провайдеры входа: по ролям через
    granted_by_roles и, у proxy, по заголовку; итог лежит в metadata входа и
    в токене. Здесь набор из токена сверяется с конфигом и из него выбирается
    профиль сессии. Работа без профиля невозможна: не выбранный пользователем
    профиль назначается автоматически только когда доступный единственный.

    Ошибки:
    RefusalError — профиль не выбран, не выдан входу или входу не выдан
        ни один профиль.
    """

    def __init__(self, profiles: Mapping[str, ChatProfileConfig]) -> None:
        if not profiles:
            msg = "section [profiles]: at least one chat profile is required, got none"
            raise ValueError(msg)

        defaults = [name for name, p in profiles.items() if p.default]
        if len(defaults) != 1:
            msg = (
                "section [profiles]: exactly one profile must set default = true, "
                f"got {len(defaults)} of {list(profiles)}: {defaults}"
            )
            raise ValueError(msg)

        self._profiles = dict(profiles)

    @property
    def all(self) -> Mapping[str, ChatProfileConfig]:
        return self._profiles

    def granted_by_roles(self, user_roles: frozenset[str]) -> frozenset[str]:
        """Имена профилей, видимых ролям по [profiles.X].roles: провайдер входа."""
        names: set[str] = set()
        for name, profile in self._profiles.items():
            if profile.visible_for(user_roles):
                names.add(name)

        return frozenset(names)

    def known(self, name: str) -> bool:
        return name in self._profiles

    def visible_for(self, granted: frozenset[str]) -> Mapping[str, ChatProfileConfig]:
        """Профили из набора входа, которые есть в конфиге; чужие имена не видны."""
        visible: dict[str, ChatProfileConfig] = {}
        for name, profile in self._profiles.items():
            if name in granted:
                visible[name] = profile

        return visible

    def resolve(self, name: str | None, sign_in: SignInMetadata) -> SelectedProfile:
        """Профиль сессии: выбор пользователя, затем выбор входа из токена, затем
        единственный выданный, затем default конфига, если он выдан; иначе отказ.
        """
        visible = self.visible_for(sign_in.profiles)

        if not visible:
            msg = "no chat profile is granted to your sign-in"
            raise RefusalError(ProfileRefusal.NO_PROFILE_ACCESS, msg)

        if name is not None:
            return self._chosen(name, visible)

        if sign_in.profile:
            return self._chosen(sign_in.profile, visible)

        if len(visible) == 1:
            only_name = next(iter(visible))
            return SelectedProfile(name=only_name, config=visible[only_name])

        default = self._default_in(visible)
        if default is not None:
            return default

        msg = f"select a chat profile to start the chat, available: {sorted(visible)}"
        raise RefusalError(ProfileRefusal.PROFILE_NOT_SELECTED, msg)

    def resolve_or_default(
        self, name: str | None, sign_in: SignInMetadata
    ) -> SelectedProfile:
        """Как resolve, но вместо отказа берётся первый выданный: для API и страниц."""
        try:
            return self.resolve(name, sign_in)
        except RefusalError as exc:
            if exc.kind is not ProfileRefusal.PROFILE_NOT_SELECTED:
                raise

        visible = self.visible_for(sign_in.profiles)
        first = next(iter(visible))

        return SelectedProfile(name=first, config=visible[first])

    @staticmethod
    def _chosen(name: str, visible: Mapping[str, ChatProfileConfig]) -> SelectedProfile:
        if name not in visible:
            msg = (
                f"chat profile {name!r} is not granted to your sign-in, "
                f"available: {sorted(visible)}"
            )
            raise RefusalError(ProfileRefusal.PROFILE_NOT_ALLOWED, msg)

        return SelectedProfile(name=name, config=visible[name])

    @staticmethod
    def _default_in(
        visible: Mapping[str, ChatProfileConfig],
    ) -> SelectedProfile | None:
        for profile_name, profile in visible.items():
            if profile.default:
                return SelectedProfile(name=profile_name, config=profile)

        return None
