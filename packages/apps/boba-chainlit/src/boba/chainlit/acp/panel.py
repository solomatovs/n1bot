"""Вкладки панели настроек чата про агента: слой пользователя (адрес, ключ и
модель провайдера поверх профиля) и настройки открытой сессии (режим, модель
из `configOptions` агента).

Слой пользователя действует на новые треды: слой чата копируется с него при
первом открытии треда. Настройки сессии применяются к открытому треду вызовом
`session/set_config_option`. Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import SecretStr

from boba.chainlit.acp.chat import SessionOptions
from boba.chainlit.acp.settings import UserAgentSettings
from boba.chainlit.chat.panel_text import PanelText
from boba.chat.profiles import ChatProfileConfig
from chainlit.input_widget import InputWidget, Select, Tab, TextInput

__all__ = ["AgentPanel", "AgentPanelEdit", "AgentPanelTab", "AgentSetting"]


class AgentPanelTab(StrEnum):
    """Вкладки панели про агента; подписи — в переводах chainlit."""

    AGENT = "agent_tab"
    SESSION = "session_tab"


class AgentSetting(StrEnum):
    """Поля слоя пользователя в панели: id виджетов и ключи переводов."""

    BASE_URL = "agent_base_url"
    API_KEY = "agent_api_key"
    MODEL = "agent_model"


class SessionWidget:
    """Виджет настройки сессии: id виджета — настройка агента с префиксом."""

    PREFIX: ClassVar[str] = "session_"

    def id_of(self, config_id: str) -> str:
        return f"{self.PREFIX}{config_id}"

    def config_of(self, widget_id: str) -> str | None:
        """Настройка агента по id виджета; None — виджет не про сессию."""
        if not widget_id.startswith(self.PREFIX):
            return None

        return widget_id[len(self.PREFIX) :]


@dataclass(frozen=True)
class AgentPanelEdit:
    """Итог разбора формы: новый слой пользователя и настройки сессии,
    которые изменились (настройка агента → значение)."""

    user: UserAgentSettings
    session: Mapping[str, str]


class AgentPanel:
    """Сборка вкладок про агента и разбор их формы для одного профиля.

    Создаётся обработчиками панели на запрос: слой пользователя из таблицы
    настроек агента, настройки сессии — у агента треда вкладки (пустые, пока
    сессии нет). Ключ провайдера в форме не показывается: пустое поле
    оставляет сохранённый, непустое заменяет его.
    """

    KEPT_HINT: ClassVar[str] = "(kept)"
    """Подсказка поля ключа: сохранённый ключ есть, пустое поле его оставит."""

    def __init__(
        self,
        profile: ChatProfileConfig,
        user: UserAgentSettings,
        options: SessionOptions,
        text: PanelText,
    ) -> None:
        self._profile = profile
        self._user = user
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

    def parse(self, form: Mapping[str, Any]) -> AgentPanelEdit:
        """Форма панели → слой пользователя и изменившиеся настройки сессии."""
        user = self._user
        if self._allowed():
            user = self._parsed_user(form)

        changed: dict[str, str] = {}
        for option in self._options.selects():
            value = form.get(self._widgets.id_of(option.id))
            if not isinstance(value, str):
                continue

            if value == option.current_value:
                continue

            changed[option.id] = value

        return AgentPanelEdit(user=user, session=changed)

    def _allowed(self) -> bool:
        """Слой пользователя открыт профилем: его настройки названы `*`."""
        for setting in AgentSetting:
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
            self._input(AgentSetting.BASE_URL, self._user.base_url),
            self._input(AgentSetting.API_KEY, None, placeholder=api_key_hint),
            self._input(AgentSetting.MODEL, self._user.model),
        ]

    def _input(
        self, setting: AgentSetting, initial: str | None, placeholder: str = ""
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
        for option in self._options.selects():
            items: dict[str, str] = {}
            for choice in self._choices(option.options):
                items[choice.value] = choice.name

            if option.current_value not in items:
                items[option.current_value] = option.current_value

            description = option.description
            if not description:
                description = option.name

            widgets.append(
                Select(
                    id=self._widgets.id_of(option.id),
                    label=self._text.field_label(self._widgets.id_of(option.id)),
                    description=description,
                    items=items,
                    initial_value=option.current_value,
                )
            )

        return widgets

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
        typed = self._string(form, AgentSetting.API_KEY)
        if typed:
            api_key = SecretStr(typed)

        return UserAgentSettings(
            base_url=self._optional(form, AgentSetting.BASE_URL),
            api_key=api_key,
            model=self._optional(form, AgentSetting.MODEL),
            system_prompt=self._user.system_prompt,
        )

    def _optional(self, form: Mapping[str, Any], setting: AgentSetting) -> str | None:
        """Пустое поле — профильное значение, непустое — переопределение."""
        value = self._string(form, setting)
        if not value:
            return None

        return value

    @staticmethod
    def _string(form: Mapping[str, Any], setting: AgentSetting) -> str:
        value = form.get(setting.value)
        if not isinstance(value, str):
            return ""

        return value.strip()
