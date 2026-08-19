"""Схема конфигурации приложения: pydantic-модели секций toml-конфига.

Ошибки:
RefusalError — профиль чата не выбран или недоступен ролям пользователя.
"""

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

from boba.chainlit.auth import AuthConfig
from boba.chainlit.domain.config import LocalStorageConfig, RoleConfig, ToolGrant
from boba.chainlit.domain.errors import RefusalError
from boba.db.postgres import PostgresConfig
from boba.sandbox.profile import SandboxConfig
from boba.toolkit.types import StringList

LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s [%(user)s] %(message)s",
            "use_colors": True,
        },
        "uvcorn": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": True,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s [%(user)s] %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "uvcorn": {
            "formatter": "uvcorn",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {"handlers": ["default"], "level": "INFO"},
    "loggers": {
        "uvicorn": {"handlers": ["uvcorn"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


class OpenAiDumpConfig(BaseModel):
    """Дамп HTTP-обмена с провайдером: флаг и каталог файлов."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(
        default=False,
        description="Писать HTTP-обмен с провайдером в path.",
    )

    path: str = Field(
        default="",
        description="Каталог дампов; обязателен при enable = true.",
    )

    @model_validator(mode="after")
    def _validate_path(self) -> Self:
        if not self.enable:
            return self

        if not self.path:
            msg = "openai.dump: enable = true требует path"
            raise ValueError(msg)

        return self


class OpenAiConfig(BaseModel):
    """Транспорт openai-совместимого провайдера: endpoint + httpx-тюнинг."""

    model_config = ConfigDict(extra="ignore")

    base_url: Annotated[
        str,
        Field(description="Endpoint openai-совместимого API."),
    ]

    api_key: Annotated[
        str,
        Field(description="Ключ API провайдера."),
    ]

    ssl_verify: bool = Field(
        default=True,
        description="Проверять TLS-сертификат сервера.",
    )

    dump: OpenAiDumpConfig = Field(
        default_factory=OpenAiDumpConfig,
        description="Дамп HTTP-обмена с провайдером.",
    )

    trust_env: bool = Field(
        default=True,
        description=(
            "Брать прокси и CA из окружения (HTTPS_PROXY, SSL_CERT_FILE); "
            "false — окружение игнорируется целиком."
        ),
    )

    proxy: str = Field(
        default="",
        description="Прокси для запросов к провайдеру; пусто — напрямую.",
    )

    http2: bool = Field(
        default=False,
        description="Разрешить HTTP/2 к провайдеру.",
    )

    stream_chunk_timeout: float = Field(
        default=600,
        ge=0,
        description=(
            "Пауза между чанками контента в стриме, секунды: считает разрывы "
            "между разобранными чанками, keepalive её не сбрасывает. "
            "0 — без потолка."
        ),
    )

    max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Повторы запроса клиентом openai при 429/5xx и таймауте; "
            "каждый повтор — новая полная попытка."
        ),
    )

    connect_timeout: float = Field(
        default=5,
        description="установка TCP-соединения с хостом (включая TLS handshake)",
    )

    read_timeout: float = Field(
        default=600,
        description=(
            "ожидание очередных байт от сервера; при stream=True — пауза "
            "между байтами, а не между чанками контента"
        ),
    )

    write_timeout: float = Field(
        default=100,
        description="отправка тела запроса на сервер",
    )

    pool_timeout: float = Field(
        default=5,
        description=("ожидание свободного соединения из пула httpx (когда все заняты)"),
    )

    max_connections: int = Field(
        default=50,
        description="",
    )

    max_keepalive_connections: int = Field(
        default=10,
        description="",
    )

    keepalive_expiry: float = Field(
        default=5,
        description="",
    )

    retries: int = Field(
        default=3,
        description="Число повторов установления соединения в httpx-транспорте.",
    )

    tcp_keepalive: bool = Field(
        default=True,
        description=(
            "TCP keepalive (SO_KEEPALIVE): защита от молчаливого разрыва "
            "простаивающего соединения файрволом (обычно режут после 5-10 минут "
            "тишины)."
        ),
    )

    tcp_keepidle: int = Field(
        default=60,
        description=(
            "TCP_KEEPIDLE: секунд простоя, после которых ядро шлёт "
            "keepalive-пробу. Без явного значения берётся sysctl "
            "tcp_keepalive_time — обычно 7200 (2 часа простоя!)."
        ),
    )

    tcp_keepintvl: int = Field(
        default=10,
        description="TCP_KEEPINTVL: интервал между повторными пробами (сек).",
    )

    tcp_keepcnt: int = Field(
        default=10,
        description=(
            "TCP_KEEPCNT: число безответных проб, после которых соединение "
            "считается мёртвым; следующая работа с сокетом даст "
            "ECONNABORTED/ETIMEDOUT."
        ),
    )

    tcp_user_timeout: int = Field(
        default=0,
        ge=0,
        description=(
            "TCP_USER_TIMEOUT: сколько миллисекунд ядро ждёт подтверждения "
            "уже отправленных данных, прежде чем оборвать соединение; в "
            "отличие от keepalive работает и на активном обмене. 0 — не задавать."
        ),
    )


class ModelParam(StrEnum):
    """Имена параметров запроса к LLM: так их принимает ChatOpenAI."""

    TEMPERATURE = "temperature"
    MAX_TOKENS = "max_tokens"
    TOP_P = "top_p"
    FREQUENCY_PENALTY = "frequency_penalty"
    PRESENCE_PENALTY = "presence_penalty"
    STOP = "stop_sequences"
    SEED = "seed"
    REASONING_EFFORT = "reasoning_effort"


class ReasoningEffort(StrEnum):
    """Глубина рассуждений reasoning-моделей провайдера."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LlmSettings(BaseModel):
    """Обращение к LLM: транспорт провайдера, модель, промпт и сэмплинг."""

    model_config = ConfigDict(extra="ignore")

    provider: Annotated[
        OpenAiConfig,
        Field(
            description=(
                "Транспорт openai-провайдера; в конфиге подключается ссылкой "
                "${openai.<name>}."
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

    temperature: float | None = Field(
        default=None,
        description="Температура сэмплинга; не задана — параметр не отправляется.",
    )

    max_tokens: int | None = Field(
        default=None,
        gt=0,
        description="Потолок токенов ответа; не задан — параметр не отправляется.",
    )

    top_p: float | None = Field(
        default=None,
        description="Nucleus sampling; не задан — параметр не отправляется.",
    )

    frequency_penalty: float | None = Field(
        default=None,
        description="Штраф за повторы; не задан — параметр не отправляется.",
    )

    presence_penalty: float | None = Field(
        default=None,
        description="Штраф за присутствие темы; не задан — параметр не отправляется.",
    )

    stop: StringList = Field(
        default=[],
        description=(
            "Последовательности, обрывающие генерацию; пустой список — "
            "параметр не отправляется."
        ),
    )

    seed: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Зерно сэмплинга для воспроизводимости; не задано — "
            "параметр не отправляется."
        ),
    )

    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description=(
            "Глубина рассуждений reasoning-модели; не задана — "
            "параметр не отправляется."
        ),
    )

    def chat_kwargs(self) -> dict[str, Any]:
        """Параметры запроса к провайдеру; незаданные не отправляются."""
        numeric: dict[ModelParam, float | int | None] = {
            ModelParam.TEMPERATURE: self.temperature,
            ModelParam.MAX_TOKENS: self.max_tokens,
            ModelParam.TOP_P: self.top_p,
            ModelParam.FREQUENCY_PENALTY: self.frequency_penalty,
            ModelParam.PRESENCE_PENALTY: self.presence_penalty,
            ModelParam.SEED: self.seed,
        }

        kwargs: dict[str, Any] = {}
        for param, value in numeric.items():
            if value is None:
                continue

            kwargs[param.value] = value

        if self.stop:
            kwargs[ModelParam.STOP.value] = list(self.stop)

        if self.reasoning_effort is not None:
            kwargs[ModelParam.REASONING_EFFORT.value] = self.reasoning_effort.value

        return kwargs


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


class RephraserConfig(LlmSettings):
    """Секция [profiles.<name>.flow.rephraser]: модель, готовящая запросы поиска.

    Параметры сэмплинга обязательны на деле, а не формально: роутеры
    подставляют собственный потолок max_tokens, который модель не принимает.
    """

    queries: Annotated[
        int,
        Field(ge=1, description="Сколько поисковых запросов готовит модель."),
    ]


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

    rephraser: RephraserConfig | None = Field(
        default=None,
        description=(
            "Модель, переписывающая запрос пользователя в поисковые; секция "
            "не задана — в инструменты уходит исходный запрос."
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

    MODEL = "model"
    TEMPERATURE = "temperature"
    TOP_P = "top_p"
    MAX_TOKENS = "max_tokens"
    FREQUENCY_PENALTY = "frequency_penalty"
    PRESENCE_PENALTY = "presence_penalty"
    STOP = "stop"
    SEED = "seed"
    REASONING_EFFORT = "reasoning_effort"
    HISTORY_MESSAGES = "history_messages"
    USER_PROMPT = "user_prompt"


class ChatProfileConfig(AgentSettings, ToolGrant):
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

    roles: StringList = Field(
        default=[],
        description="Роли, которым профиль виден; '*' — всем.",
    )

    models: StringList = Field(
        default=[],
        description=(
            "Модели, которые пользователь может выбрать в настройках; "
            "пустой список — выбор модели закрыт."
        ),
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
    def _validate_models(self) -> Self:
        if not self.models:
            return self

        if self.model not in self.models:
            msg = f"profile: model {self.model!r} is not listed in models"
            raise ValueError(msg)

        return self

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

        model_allowed = self.setting_allowed(UserSetting.MODEL)
        if model_allowed and self.settings and not self.models:
            msg = "profile: setting 'model' is allowed, but models list is empty"
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

    def visible_for(self, user_roles: frozenset[str]) -> bool:
        if not user_roles:
            return False

        if ToolGrant.WILDCARD in self.roles:
            return True

        return bool(frozenset(self.roles) & user_roles)


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
                f"settings: default {self.default} is out of "
                f"[{self.low}, {self.high}]"
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
    """Секция [settings]: пределы пользовательских настроек LLM."""

    model_config = ConfigDict(extra="ignore")

    temperature: NumberBounds
    top_p: NumberBounds
    max_tokens: NumberBounds
    frequency_penalty: NumberBounds
    presence_penalty: NumberBounds
    history_messages: NumberBounds


class UserLlmOverrides(BaseModel):
    """Настройки пользователя поверх профиля; None — поле не переопределено."""

    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: list[str] | None = None
    seed: int | None = Field(default=None, ge=0)
    reasoning_effort: ReasoningEffort | None = None
    history_messages: int | None = None
    user_prompt: str | None = None

    def clamped(
        self,
        bounds: SettingsBounds,
        profile: ChatProfileConfig,
    ) -> "UserLlmOverrides":
        """Копия в пределах границ и разрешений профиля.

        Поле, не разрешённое профилем, отбрасывается — даже если пришло в
        обход панели; модель вне белого списка отбрасывается тоже.
        """
        update: dict[str, Any] = {}

        if self.model is not None and self.model not in profile.models:
            update[UserSetting.MODEL.value] = None

        if self.temperature is not None:
            update[UserSetting.TEMPERATURE.value] = bounds.temperature.clamp(
                self.temperature
            )

        if self.top_p is not None:
            update[UserSetting.TOP_P.value] = bounds.top_p.clamp(self.top_p)

        if self.max_tokens is not None:
            update[UserSetting.MAX_TOKENS.value] = int(
                bounds.max_tokens.clamp(self.max_tokens)
            )

        if self.frequency_penalty is not None:
            update[UserSetting.FREQUENCY_PENALTY.value] = (
                bounds.frequency_penalty.clamp(self.frequency_penalty)
            )

        if self.presence_penalty is not None:
            update[UserSetting.PRESENCE_PENALTY.value] = (
                bounds.presence_penalty.clamp(self.presence_penalty)
            )

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
        saved: "UserLlmOverrides",
    ) -> "UserLlmOverrides":
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
    ) -> "SettingsView":
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
    ) -> "SettingsView":
        """Представление после правки формы: изменённое стало личным."""
        edited = UserLlmOverrides.edited(form, shown, self.profile, self.overrides)
        return self.of(self.bounds, self.profile, edited)


class UserMeta(BaseModel):
    """Разбор users.meta: настройки LLM по профилям под ключом llm."""

    model_config = ConfigDict(extra="ignore")

    llm: dict[str, UserLlmOverrides] = {}

    @classmethod
    def of(cls, raw: Mapping[str, Any] | None) -> "UserMeta":
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


class ChainlitExtendConfig(BaseModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8501)
    ssl_cert: str | None = Field(default=None)
    ssl_key: str | None = Field(default=None)
    ssl_ca_certs: str | None = Field(default=None)
    url_prefix: str = Field(default="")
    root: str = Field(default="")

    ping_interval: int = Field(
        default=300,
        description=(
            "Период heartbeat'а engine.io, секунды: с ним сервер шлёт клиенту "
            "ping, значение уезжает клиенту в handshake. Клиент считает "
            "соединение мёртвым, если ping не пришёл за ping_interval + "
            "ping_timeout, и переподключается; большое значение даёт пережить "
            "паузу на breakpoint'е отладки."
        ),
    )

    ping_timeout: int = Field(
        default=300,
        description=(
            "Ожидание pong'а engine.io, секунды: без ответа сервер закрывает "
            "сессию сокета с причиной ping timeout. Вместе с ping_interval "
            "задаёт, через сколько тишины вкладка уходит в реконнект."
        ),
    )

    max_decode_packets: int = Field(
        default=256,
        description=(
            "за паузу на брейкпоинте клиент копит события и шлёт их одним "
            "polling-POST; дефолтных 16 пакетов не хватает"
        ),
    )

    auth_secret: str | None = Field(
        default=None,
        description=(
            "Секрет подписи JWT; chainlit читает его только из env "
            "(CHAINLIT_AUTH_SECRET), бутстрап прокидывает значение туда."
        ),
    )

    ws_ping_interval: float = Field(
        default=20,
        ge=0,
        description=(
            "Период ws-пинга uvicorn, секунды: кадр ping самого протокола "
            "WebSocket, отдельный от heartbeat'а engine.io. Отвечает на него "
            "браузер, а не приложение. 0 — не пинговать, живость остаётся "
            "только на engine.io."
        ),
    )

    ws_ping_timeout: float = Field(
        default=20,
        ge=0,
        description=(
            "Ожидание ws-pong'а, секунды: без ответа uvicorn рвёт соединение "
            "и вкладка уходит в реконнект, даже когда ход жив. Малое значение "
            "рвёт связь на замершей вкладке и сетевых задержках. 0 — не ждать."
        ),
    )

    ws_per_message_deflate: bool = Field(
        default=True,
        description="Сжатие WebSocket-фреймов (permessage-deflate) в uvicorn.",
    )

    ws_protocol: str = Field(
        default="auto",
        description="WebSocket-реализация uvicorn: auto/websockets/wsproto/none.",
    )


class CheckpointerConfig(BaseModel):
    """Конфиг langgraph-checkpointer: postgres-подключение + схема БД."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    postgres: Annotated[
        PostgresConfig,
        Field(
            description=(
                "Подключение и пул; в конфиге подключается ссылкой ${postgres}."
            ),
        ),
    ]

    db_schema: str = Field(
        default="public",
        alias="schema",
        description=(
            "Схема для таблиц checkpointer. Задаётся в search_path соединения, "
            "т.к. AsyncPostgresSaver пишет имена таблиц без схемы."
        ),
    )


class DataLayerConfig(BaseModel):
    """Конфиг chainlit data layer: postgres-подключение + схема БД."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    postgres: Annotated[
        PostgresConfig,
        Field(
            description=(
                "Подключение и пул; в конфиге подключается ссылкой ${postgres}."
            ),
        ),
    ]

    db_schema: str = Field(
        default="public",
        alias="schema",
        description="Схема таблиц data layer; PostgresDataLayer квалифицирует ею SQL.",
    )


class StreamJournalConfig(BaseModel):
    """Журнал живого вывода инструментов: служебный том на пользователя."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(
        default=False,
        description="Писать вывод каждого вызова инструмента в журнал.",
    )

    dir: str = Field(
        default="",
        description=(
            "Корень журналов: каталог, том на пользователя внутри; "
            "переполнение держит отдельная точка монтирования под корнем."
        ),
    )

    reserve_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        description=(
            "Резерв места перед новым журналом: старейшие треды вытесняются, "
            "пока свободного меньше; 0 выключает ротацию."
        ),
    )

    @model_validator(mode="after")
    def _validate_enabled(self) -> Self:
        if not self.enable:
            return self

        if not self.dir:
            msg = "stream_journal: dir is required"
            raise ValueError(msg)

        return self


class AppConfig(BaseModel):
    """Параметры chainlit-приложения: server, профили чата и права ролей."""

    model_config = ConfigDict(extra="ignore")

    chainlit: Annotated[
        ChainlitExtendConfig,
        Field(
            description=("Chainlit config; ${chainlit.<name>}."),
        ),
    ]

    profiles: Annotated[
        dict[str, ChatProfileConfig],
        Field(
            min_length=1,
            description=(
                "Профили чата по имени; в конфиге подключается ссылкой ${profiles}."
            ),
        ),
    ]

    roles: Annotated[
        dict[str, RoleConfig],
        Field(
            description=(
                "Права ролей по имени; в конфиге подключается ссылкой ${roles}."
            ),
        ),
    ]

    settings: Annotated[
        SettingsBounds,
        Field(
            description=(
                "Пределы пользовательских настроек LLM; ссылкой ${settings}."
            ),
        ),
    ]

    auth: Annotated[
        list[AuthConfig],
        Field(
            default_factory=list,
            description="Доступные способы авторизации",
        ),
    ]

    logger: Annotated[
        dict,
        Field(
            default=LOGGING_CONFIG,
            description="Конфигурация логера",
        ),
    ]

    checkpointer: Annotated[
        CheckpointerConfig,
        Field(description="Сервис langgraph-checkpointer: подключение + схема."),
    ]

    data_layer: Annotated[
        DataLayerConfig,
        Field(description="Сервис chainlit data layer: подключение + схема."),
    ]

    storage: Annotated[
        LocalStorageConfig,
        Field(description="Файловое хранилище вложений."),
    ]

    stream_journal: Annotated[
        StreamJournalConfig,
        Field(
            default_factory=StreamJournalConfig,
            description="Журнал живого вывода инструментов.",
        ),
    ]

    sandbox: Annotated[
        SandboxConfig,
        Field(description="Реестр профилей песочницы; ${sandbox}."),
    ]
