"""Профили чата и пользовательские настройки LLM: модели секций конфига и
правила выбора профиля под роли субъекта.

Ошибки:
RefusalError — профиль чата не выбран или недоступен ролям пользователя.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    model_validator,
)

from boba.access import ProfileGrant, RoleConfig, ToolGrant
from boba.chat.generation import GenerationConfig
from boba.chat.provider import ChatBackendConfig
from boba.identity.errors import RefusalError
from boba.toolkit.types import StringList

__all__ = [
    "AgentSettings",
    "ChatProfileConfig",
    "ChatProfiles",
    "FlowKind",
    "LlmSettings",
    "NumberBounds",
    "PlainFlowConfig",
    "PrefetchFlowConfig",
    "ProfileRefusal",
    "ProfilesSection",
    "RolesSection",
    "SelectedProfile",
    "SettingsBounds",
    "SettingsView",
    "UserLlmOverrides",
    "UserMeta",
    "UserSetting",
]

logger = logging.getLogger(__name__)


class LlmSettings(BaseModel):
    """Обращение к LLM: бэкенд провайдера, модель, промпт и сэмплинг."""

    model_config = ConfigDict(extra="ignore")

    provider: Annotated[
        ChatBackendConfig,
        Field(
            description=(
                "Чат-провайдер профиля: kind = 'openai' | 'ollama' с "
                "транспортом http = ${http.<name>} либо kind = 'local' "
                "с каталогом модели."
            ),
        ),
    ]

    model: Annotated[
        str,
        Field(description="Имя LLM-модели у выбранного провайдера."),
    ]

    system_prompt: str = Field(
        default="",
        description="Системный промпт по умолчанию",
    )

    sampling: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Параметры запроса к провайдеру как есть: имена и значения уходят "
            "в тело запроса без проверок и переименований. Что провайдер "
            "принимает — решает администратор профиля."
        ),
    )

    def chat_sampling(self) -> dict[str, Any]:
        """Сэмплинг запроса к провайдеру: копия админской таблицы."""
        return dict(self.sampling)



class AgentSettings(LlmSettings):
    """Настройки хода агента: параметры LLM плюс окно истории."""

    history_messages: int = Field(
        default=30,
        ge=1,
        description=(
            "Сколько последних сообщений истории уходит в LLM. Считаются "
            "только реплики: вызовы инструментов и их результаты из прошлых "
            "ходов вырезаются, текущий ход передаётся целиком."
        ),
    )


class FlowKind(StrEnum):
    """Виды агентского flow профиля чата."""

    PLAIN = "plain"
    PREFETCH = "prefetch"


class PlainFlowConfig(BaseModel):
    """Flow без подготовки: обычный цикл модель-инструменты."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal[FlowKind.PLAIN] = FlowKind.PLAIN


class PrefetchFlowConfig(BaseModel):
    """Flow с подготовкой контекста: поиск идёт до обращения к основной модели."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal[FlowKind.PREFETCH]

    tools: Annotated[
        StringList,
        Field(
            min_length=1,
            description="Инструменты, вызываемые с каждым поисковым запросом.",
        ),
    ]

    rephraser: GenerationConfig | None = Field(
        default=None,
        description=(
            "Модель, переписывающая запрос пользователя в поисковые: локальный "
            "ONNX-инференс либо openai-совместимый провайдер; секция не задана "
            "— в инструменты уходит исходный запрос."
        ),
    )

    @staticmethod
    def client_key(profile: str) -> str:
        """Ключ httpx-клиента переформулировщика в реестре клиентов профилей."""
        return f"{profile}:flow"


class UserSetting(StrEnum):
    """Настройки LLM, которые профиль может открыть пользователю.

    Значения совпадают с полями UserLlmOverrides и id виджетов панели;
    подписи и описания живут в переводах chainlit (chat.settings.llm).
    """

    HISTORY_MESSAGES = "history_messages"
    USER_PROMPT = "user_prompt"


class ChatProfileConfig(AgentSettings, ProfileGrant):
    """Секция [profiles.<name>]: режим работы бота, выбираемый пользователем."""

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

    settings: StringList = Field(
        default=[],
        description=(
            "Настройки LLM, которые пользователь может переопределять; "
            "имена полей UserLlmOverrides, '*' — все. Пустой список — "
            "панель настроек профилю не показывается."
        ),
    )

    flow: Annotated[
        PlainFlowConfig | PrefetchFlowConfig,
        Field(
            discriminator="kind",
            default_factory=PlainFlowConfig,
            description="Агентский flow профиля; секция не задана — обычный цикл.",
        ),
    ]

    def setting_allowed(self, name: str) -> bool:
        if ToolGrant.WILDCARD in self.settings:
            return True

        return name in self.settings

    @model_validator(mode="after")
    def _validate_settings(self) -> Self:
        known = {setting.value for setting in UserSetting}

        unknown: list[str] = []
        for name in self.settings:
            if name == ToolGrant.WILDCARD:
                continue

            if name in known:
                continue

            unknown.append(name)

        if unknown:
            msg = f"profile: unknown settings {unknown}"
            raise ValueError(msg)

        return self

    @model_validator(mode="after")
    def _validate_flow(self) -> Self:
        """Инструменты flow обязаны входить в набор инструментов профиля."""
        flow = self.flow
        if not isinstance(flow, PrefetchFlowConfig):
            return self

        if ToolGrant.WILDCARD in self.tools:
            return self

        missing: list[str] = []
        for name in flow.tools:
            if name in self.tools:
                continue

            missing.append(name)

        if missing:
            msg = f"profile: flow tools {missing} are not in profile tools"
            raise ValueError(msg)

        return self


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


class ChatProfiles:
    """Реестр профилей чата: видимость по ролям и выбор профиля сессии.

    Работа без профиля невозможна: не выбранный пользователем профиль
    назначается автоматически только когда доступный профиль единственный.

    Ошибки:
    RefusalError — профиль не выбран, недоступен ролям или ролям не виден
        ни один профиль.
    """

    def __init__(self, profiles: Mapping[str, ChatProfileConfig]) -> None:
        if not profiles:
            msg = "profiles: at least one chat profile is required"
            raise ValueError(msg)

        defaults = [name for name, p in profiles.items() if p.default]
        if len(defaults) != 1:
            msg = (
                "profiles: exactly one profile must set default = true, "
                f"got {defaults or 'none'}"
            )
            raise ValueError(msg)

        self._profiles = dict(profiles)

    @property
    def all(self) -> Mapping[str, ChatProfileConfig]:
        return self._profiles

    def visible_for(
        self, user_roles: frozenset[str]
    ) -> Mapping[str, ChatProfileConfig]:
        visible: dict[str, ChatProfileConfig] = {}
        for name, profile in self._profiles.items():
            if profile.visible_for(user_roles):
                visible[name] = profile

        return visible

    def resolve(
        self,
        name: str | None,
        user_roles: frozenset[str],
    ) -> SelectedProfile:
        visible = self.visible_for(user_roles)

        if not visible:
            raise RefusalError(
                ProfileRefusal.NO_PROFILE_ACCESS,
                "no chat profile is available for your roles",
            )

        if name is not None:
            if name not in visible:
                raise RefusalError(
                    ProfileRefusal.PROFILE_NOT_ALLOWED,
                    f"chat profile {name!r} is not available for your roles",
                )

            return SelectedProfile(name=name, config=visible[name])

        if len(visible) == 1:
            only_name = next(iter(visible))
            return SelectedProfile(name=only_name, config=visible[only_name])

        raise RefusalError(
            ProfileRefusal.PROFILE_NOT_SELECTED,
            "select a chat profile to start the chat",
        )

    def resolve_or_default(
        self,
        name: str | None,
        user_roles: frozenset[str],
    ) -> SelectedProfile:
        """Как resolve, но без имени берётся профиль по умолчанию: для API и страниц."""
        if name is not None:
            return self.resolve(name, user_roles)

        visible = self.visible_for(user_roles)
        if not visible:
            raise RefusalError(
                ProfileRefusal.NO_PROFILE_ACCESS,
                "no chat profile is available for your roles",
            )

        for profile_name, profile in visible.items():
            if profile.default:
                return SelectedProfile(name=profile_name, config=profile)

        first = next(iter(visible))
        return SelectedProfile(name=first, config=visible[first])


class NumberBounds(BaseModel):
    """Границы, шаг и стартовое значение числовой настройки пользователя."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    low: Annotated[float, Field(alias="min")]
    high: Annotated[float, Field(alias="max")]
    step: Annotated[float, Field(gt=0)]
    default: float
    """Значение виджета, когда параметр не задан ни профилем, ни пользователем."""

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        if self.low > self.high:
            msg = f"settings: min {self.low} is above max {self.high}"
            raise ValueError(msg)

        if not self.low <= self.default <= self.high:
            msg = (
                f"settings: default {self.default} is out of [{self.low}, {self.high}]"
            )
            raise ValueError(msg)

        return self

    def clamp(self, value: float) -> float:
        if value < self.low:
            return self.low

        if value > self.high:
            return self.high

        return value


class SettingsBounds(BaseModel):
    """Пределы пользовательских настроек панели; конфиг их не задаёт."""

    model_config = ConfigDict(extra="ignore")

    history_messages: NumberBounds = Field(
        default_factory=lambda: NumberBounds(low=1, high=100, step=1, default=30)
    )


class UserLlmOverrides(BaseModel):
    """Настройки пользователя поверх профиля; None — поле не переопределено."""

    model_config = ConfigDict(extra="ignore")

    history_messages: int | None = None
    user_prompt: str | None = None

    def clamped(
        self,
        bounds: SettingsBounds,
        profile: ChatProfileConfig,
    ) -> UserLlmOverrides:
        """Копия в пределах границ и разрешений профиля.

        Поле, не разрешённое профилем, отбрасывается — даже если пришло в
        обход панели.
        """
        update: dict[str, Any] = {}

        if self.history_messages is not None:
            update[UserSetting.HISTORY_MESSAGES.value] = int(
                bounds.history_messages.clamp(self.history_messages)
            )

        # запрет профиля сильнее любого пришедшего значения — даже мимо панели
        for setting in UserSetting:
            if profile.setting_allowed(setting.value):
                continue

            update[setting.value] = None

        return self.model_copy(update=update)

    def apply_to(self, profile: ChatProfileConfig) -> AgentSettings:
        """Настройки хода: профиль, поверх которого легли поля пользователя."""
        update: dict[str, Any] = {}

        for setting in UserSetting:
            if setting is UserSetting.USER_PROMPT:
                continue

            value = getattr(self, setting.value)
            if value is None:
                continue

            update[setting.value] = value

        if self.user_prompt:
            update["system_prompt"] = f"{profile.system_prompt}\n\n{self.user_prompt}"

        return profile.model_copy(update=update)

    @classmethod
    def edited(
        cls,
        form: Mapping[str, object],
        shown: Mapping[str, object],
        profile: ChatProfileConfig,
        saved: UserLlmOverrides,
    ) -> UserLlmOverrides:
        """Форма панели -> переопределения пользователя.

        Значение, равное показанному, означает «пользователь не трогал»: его
        прежнее состояние сохраняется. Возврат к значению профиля снимает
        переопределение.
        """
        parsed = cls.model_validate(cls._cleared(form))

        update: dict[str, Any] = {}
        for setting in UserSetting:
            name = setting.value
            value = getattr(parsed, name)

            if value == shown.get(name):
                update[name] = getattr(saved, name)
                continue

            if setting is UserSetting.USER_PROMPT:
                continue

            if value == getattr(profile, name, None):
                update[name] = None

        return parsed.model_copy(update=update)

    @staticmethod
    def _cleared(values: Mapping[str, object]) -> dict[str, object]:
        """Пустые строки формы -> None: у Select и TextInput нет None-состояния."""
        cleared: dict[str, object] = {}
        for name, value in values.items():
            if isinstance(value, str) and not value.strip():
                cleared[name] = None
                continue

            cleared[name] = value

        return cleared

    def stored(self) -> dict[str, Any]:
        """Значение для users.meta: только переопределённые поля."""
        return self.model_dump(mode="json", exclude_none=True)


class SettingsView(BaseModel):
    """Итоговые настройки хода: профиль, поверх которого легли личные.

    Один источник правды для всех потребителей: агент берёт отсюда параметры
    запроса, панель — что показать пользователю, сохранение — что записать
    в users.meta.
    """

    model_config = ConfigDict(frozen=True)

    bounds: SettingsBounds
    profile: ChatProfileConfig
    overrides: UserLlmOverrides

    @classmethod
    def of(
        cls,
        bounds: SettingsBounds,
        profile: ChatProfileConfig,
        overrides: UserLlmOverrides,
    ) -> SettingsView:
        """Представление с личными настройками в границах и правах профиля."""
        return cls(
            bounds=bounds,
            profile=profile,
            overrides=overrides.clamped(bounds, profile),
        )

    def agent(self) -> AgentSettings:
        """Настройки, с которыми идёт ход."""
        return self.overrides.apply_to(self.profile)

    def value_of(self, setting: UserSetting) -> Any:
        """Значение настройки как его видит пользователь."""
        if setting is UserSetting.USER_PROMPT:
            return self.overrides.user_prompt

        return getattr(self.agent(), setting.value)

    def edited(
        self,
        form: Mapping[str, Any],
        shown: Mapping[str, Any],
    ) -> SettingsView:
        """Представление после правки формы: изменённое стало личным."""
        edited = UserLlmOverrides.edited(form, shown, self.profile, self.overrides)
        return self.of(self.bounds, self.profile, edited)


class UserMeta(BaseModel):
    """Разбор users.meta: настройки LLM по профилям под ключом llm."""

    model_config = ConfigDict(extra="ignore")

    llm: dict[str, UserLlmOverrides] = {}

    @classmethod
    def of(cls, raw: Mapping[str, Any] | None) -> UserMeta:
        """Метаданные пользователя; битые настройки деградируют до пустых.

        Настройки — пользовательские данные: смена их схемы не должна
        блокировать вход, сброс к дефолтам профиля виден в панели.
        """
        if raw is None:
            return cls()

        try:
            return cls.model_validate(raw)
        except ValidationError:
            logging.getLogger(__name__).warning(
                "user meta has malformed llm settings, ignoring them"
            )
            return cls()

    def overrides_for(self, profile_name: str) -> UserLlmOverrides:
        found = self.llm.get(profile_name)
        if found is None:
            return UserLlmOverrides()

        return found
