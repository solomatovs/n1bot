"""Вкладки панели настроек чата про агента: слой пользователя (адрес, ключ и
модель провайдера поверх профиля) и выбор сессии (режим разрешений, профиль
модели из `configOptions` агента).

Значения вкладки «Агент» — слой пользователя, значения вкладки «Сессия» — слой
чата треда; варианты списков присылает агент. Разобранная форма уходит
единому пути правки AgentSettingsChanges, который пишет слои и перезапускает
процесс агента.

Ошибки:
AgentSettingsError — в форме значение списка, которого агент не предлагал.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

from acp.experimental.v2 import schema
from pydantic import SecretStr

from boba.agent.records import PermissionMode
from boba.chainlit.agent.chat import ConfigCategory, SessionOptions
from boba.chainlit.agent.settings import (
    AgentSettingsChange,
    AgentSettingsError,
    ChatAgentSettings,
    UserAgentSettings,
)
from boba.chainlit.chat.panel_text import PanelText
from boba.chat.profiles import ChatProfileConfig, UserSetting
from chainlit.input_widget import InputWidget, Select, Tab, TextInput

__all__ = ["AgentPanel", "AgentPanelTab"]


class AgentPanelTab(StrEnum):
    """Вкладки панели про агента; подписи — в переводах chainlit."""

    AGENT = "agent_tab"
    SESSION = "session_tab"


class SessionWidget:
    """Виджет выбора сессии: id виджета — категория настройки агента с
    префиксом (`session_mode`, `session_model`)."""

    PREFIX: ClassVar[str] = "session_"

    def id_of(self, category: ConfigCategory) -> str:
        return f"{self.PREFIX}{category.value}"


class AgentPanel:
    """Сборка вкладок про агента и разбор их формы для одного профиля.

    Создаётся обработчиками панели на запрос: слой пользователя и слой чата
    треда из таблицы настроек агента, варианты выбора сессии — у агента треда
    вкладки (пустые, пока сессии нет). Ключ провайдера в форме не
    показывается: пустое поле оставляет сохранённый, непустое заменяет его.
    """

    KEPT_HINT: ClassVar[str] = "(kept)"
    """Подсказка поля ключа: сохранённый ключ есть, пустое поле его оставит."""

    def __init__(
        self,
        profile: ChatProfileConfig,
        user: UserAgentSettings,
        chat: ChatAgentSettings | None,
        options: SessionOptions,
        text: PanelText,
    ) -> None:
        self._profile = profile
        self._user = user
        self._chat = chat
        self._options = options
        self._text = text
        self._widgets = SessionWidget()

    def tabs(self) -> list[Tab]:
        """Вкладки; вкладка без виджетов не показывается."""
        built: list[Tab] = []
        agent = self._agent_widgets()
        if agent:
            built.append(
                Tab(
                    id=AgentPanelTab.AGENT.value,
                    label=self._text.tab(AgentPanelTab.AGENT.value),
                    inputs=agent,
                )
            )

        session = self._session_widgets()
        if session:
            built.append(
                Tab(
                    id=AgentPanelTab.SESSION.value,
                    label=self._text.tab(AgentPanelTab.SESSION.value),
                    inputs=session,
                )
            )

        return built

    def parse(self, form: Mapping[str, Any]) -> AgentSettingsChange:
        """Форма панели → правка настроек: слой пользователя целиком и выбор
        вкладки «Сессия» (None — поля в форме не было).

        Ошибки:
        AgentSettingsError — значение списка не из предложенных агентом.
        """
        user = self._user
        if self._allowed():
            user = self._parsed_user(form)

        mode: PermissionMode | None = None
        if chosen := self._chosen(form, ConfigCategory.MODE):
            mode = self._mode(chosen)

        model_name = self._chosen(form, ConfigCategory.MODEL)

        return AgentSettingsChange(user=user, mode=mode, model_name=model_name)

    def _allowed(self) -> bool:
        """Слой пользователя открыт профилем: его настройки названы `*`."""
        for setting in UserSetting:
            if not self._profile.setting_allowed(setting.value):
                return False

        return True

    def _agent_widgets(self) -> list[InputWidget]:
        if not self._allowed():
            return []

        api_key_hint = ""
        if self._user.api_key is not None:
            api_key_hint = self.KEPT_HINT

        return [
            self._input(UserSetting.AGENT_BASE_URL, self._user.base_url),
            self._input(UserSetting.AGENT_API_KEY, None, placeholder=api_key_hint),
            self._input(UserSetting.AGENT_MODEL, self._user.model),
        ]

    def _input(
        self, setting: UserSetting, initial: str | None, placeholder: str = ""
    ) -> TextInput:
        shown = initial
        if shown is None:
            shown = ""

        return TextInput(
            id=setting.value,
            label=self._text.field_label(setting.value),
            description=self._text.field_description(setting.value),
            initial=shown,
            placeholder=placeholder,
        )

    def _session_widgets(self) -> list[InputWidget]:
        widgets: list[InputWidget] = []
        for category in ConfigCategory:
            option = self._option(category)
            if option is None:
                continue

            widgets.append(self._select(category, option))

        return widgets

    def _select(
        self, category: ConfigCategory, option: schema.SelectSessionConfigOption
    ) -> Select:
        # items у Select chainlit — подпись варианта → его значение
        items: dict[str, str] = {}
        for choice in self._choices(option.options):
            items[choice.name] = choice.value

        current = self._current(category, option)
        if current not in items.values():
            items[current] = current

        description = option.description
        if not description:
            description = option.name

        return Select(
            id=self._widgets.id_of(category),
            label=self._text.field_label(self._widgets.id_of(category)),
            description=description,
            items=items,
            initial_value=current,
        )

    def _option(
        self, category: ConfigCategory
    ) -> schema.SelectSessionConfigOption | None:
        """Список агента этой категории; None — агент его не прислал."""
        for option in self._options.selects():
            if option.category == category.value:
                return option

        return None

    def _current(
        self, category: ConfigCategory, option: schema.SelectSessionConfigOption
    ) -> str:
        """Значение списка из слоя чата: оно применится после перезапуска,
        даже если процесс ещё работает с прежним; без слоя — значение агента."""
        chat = self._chat
        if chat is None:
            return option.current_value

        if category is ConfigCategory.MODE:
            return chat.mode.value

        return chat.model_name

    def _chosen(self, form: Mapping[str, Any], category: ConfigCategory) -> str | None:
        """Выбор списка в форме; None — списка нет у агента или в форме.

        Ошибки:
        AgentSettingsError — значение не из предложенных агентом.
        """
        option = self._option(category)
        if option is None:
            return None

        value = form.get(self._widgets.id_of(category))
        if not isinstance(value, str):
            return None

        known: list[str] = [self._current(category, option)]
        for choice in self._choices(option.options):
            known.append(choice.value)

        if value not in known:
            msg = (
                f"settings panel: session option {category.value!r} has no value "
                f"{value!r}, the agent offers {sorted(known)}; pick one of them"
            )
            raise AgentSettingsError(msg)

        return value

    @staticmethod
    def _mode(value: str) -> PermissionMode:
        """Ошибки:
        AgentSettingsError — режим агента, которого чат не знает.
        """
        try:
            return PermissionMode(value)
        except ValueError as exc:
            known = [mode.value for mode in PermissionMode]
            msg = (
                f"settings panel: permission mode {value!r} is offered by the "
                f"agent but unknown to the chat, expected one of {known}"
            )
            raise AgentSettingsError(msg) from exc

    @staticmethod
    def _choices(options: Sequence[Any]) -> list[Any]:
        """Варианты списка; группы вариантов раскрываются в их варианты."""
        found: list[Any] = []
        for item in options:
            nested = getattr(item, "options", None)
            if isinstance(nested, list):
                found.extend(nested)
                continue

            found.append(item)

        return found

    def _parsed_user(self, form: Mapping[str, Any]) -> UserAgentSettings:
        api_key = self._user.api_key
        typed = self._string(form, UserSetting.AGENT_API_KEY)
        if typed:
            api_key = SecretStr(typed)

        return UserAgentSettings(
            base_url=self._optional(form, UserSetting.AGENT_BASE_URL),
            api_key=api_key,
            model=self._optional(form, UserSetting.AGENT_MODEL),
        )

    def _optional(self, form: Mapping[str, Any], setting: UserSetting) -> str | None:
        """Пустое поле — профильное значение, непустое — переопределение."""
        value = self._string(form, setting)
        if not value:
            return None

        return value

    @staticmethod
    def _string(form: Mapping[str, Any], setting: UserSetting) -> str:
        value = form.get(setting.value)
        if not isinstance(value, str):
            return ""

        return value.strip()
