"""Панель настроек LLM: виджеты chainlit и разбор формы в переопределения.

Панель показывает только разрешённые профилем настройки со значениями профиля
(поверх которых легли уже сохранённые пользовательские); изменённое при
сохранении уходит в users.meta, возврат к значению профиля снимает
переопределение. Модель и параметры сэмплинга пользователю
недоступны — их задаёт администратор в профиле. Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

from boba.chainlit.chat.panel_text import PanelText
from boba.chat.profiles import SettingsView, UserSetting
from chainlit.input_widget import InputWidget, Tab, TextInput

__all__ = ["PanelTab", "SettingsPanel"]


class PanelTab(StrEnum):
    """Вкладки панели настроек; подписи — в переводах chainlit."""

    PROMPT = "prompt_tab"


class SettingsPanel:
    """Сборка виджетов панели и разбор её формы для одного профиля."""

    TABS: ClassVar[dict[PanelTab, tuple[UserSetting, ...]]] = {
        PanelTab.PROMPT: (UserSetting.USER_PROMPT,),
    }

    def __init__(self, view: SettingsView, text: PanelText) -> None:
        self._view = view
        self._text = text
        self._profile = view.profile

    def tabs(self) -> list[Tab]:
        """Вкладки панели; вкладка без разрешённых настроек не показывается."""
        built: list[Tab] = []
        for tab, settings in self.TABS.items():
            widgets = self._tab_widgets(settings)
            if not widgets:
                continue

            built.append(
                Tab(id=tab.value, label=self._text.tab(tab.value), inputs=widgets)
            )

        return built

    def shown_values(self) -> dict[str, Any]:
        """Значения, с которыми панель открылась: с ними сравнивается форма."""
        values: dict[str, Any] = {}
        for setting in UserSetting:
            values[setting.value] = self._view.value_of(setting)

        return values

    def parse(self, form: Mapping[str, Any]) -> SettingsView:
        """Форма панели -> представление с новыми личными настройками."""
        return self._view.edited(form, self.shown_values())

    def _tab_widgets(self, settings: Sequence[UserSetting]) -> list[InputWidget]:
        widgets: list[InputWidget] = []
        for setting in settings:
            if not self._profile.setting_allowed(setting.value):
                continue

            widgets.append(self._prompt_input())

        return widgets

    def _prompt_input(self) -> TextInput:
        user_prompt = ""
        if saved := self._view.overrides.user_prompt:
            user_prompt = saved

        return TextInput(
            id=UserSetting.USER_PROMPT.value,
            label=self._text.label(UserSetting.USER_PROMPT),
            description=self._text.description(UserSetting.USER_PROMPT),
            initial=user_prompt,
            multiline=True,
        )
