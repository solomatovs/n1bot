"""Панель настроек LLM: виджеты chainlit и разбор формы в переопределения.

Панель показывает только разрешённые профилем настройки со значениями профиля
(поверх которых легли уже сохранённые пользовательские); изменённое при
сохранении уходит в users.meta, возврат к значению профиля снимает
переопределение. Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.infra.config import (
    AgentSettings,
    NumberBounds,
    ReasoningEffort,
    SettingsView,
    UserSetting,
)
from chainlit.input_widget import (
    InputWidget,
    NumberInput,
    Select,
    Slider,
    Tab,
    Tags,
    TextInput,
)

__all__ = ["PanelTab", "SettingsPanel"]


class PanelTab(StrEnum):
    """Вкладки панели настроек; подписи — в переводах chainlit."""

    MODEL = "model_tab"
    SAMPLING = "sampling_tab"
    PROMPT = "prompt_tab"


class SettingsPanel:
    """Сборка виджетов панели и разбор её формы для одного профиля."""

    TABS: ClassVar[dict[PanelTab, tuple[UserSetting, ...]]] = {
        PanelTab.MODEL: (
            UserSetting.MODEL,
            UserSetting.REASONING_EFFORT,
            UserSetting.SEED,
        ),
        PanelTab.SAMPLING: (
            UserSetting.TEMPERATURE,
            UserSetting.TOP_P,
            UserSetting.MAX_TOKENS,
            UserSetting.FREQUENCY_PENALTY,
            UserSetting.PRESENCE_PENALTY,
            UserSetting.HISTORY_MESSAGES,
            UserSetting.STOP,
        ),
        PanelTab.PROMPT: (UserSetting.USER_PROMPT,),
    }

    NUMERIC: ClassVar[tuple[UserSetting, ...]] = (
        UserSetting.TEMPERATURE,
        UserSetting.TOP_P,
        UserSetting.MAX_TOKENS,
        UserSetting.FREQUENCY_PENALTY,
        UserSetting.PRESENCE_PENALTY,
        UserSetting.HISTORY_MESSAGES,
    )
    """Настройки-слайдеры: границы, шаг и стартовое значение берутся из [settings]."""

    INTEGER: ClassVar[tuple[UserSetting, ...]] = (
        UserSetting.MAX_TOKENS,
        UserSetting.HISTORY_MESSAGES,
    )

    def __init__(self, view: SettingsView, text: PanelText) -> None:
        self._view = view
        self._text = text
        self._bounds = view.bounds
        self._profile = view.profile

    def tabs(self) -> list[Tab]:
        """Вкладки панели; вкладка без разрешённых настроек не показывается."""
        effective = self._view.agent()

        built: list[Tab] = []
        for tab, settings in self.TABS.items():
            widgets = self._tab_widgets(settings, effective)
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
            values[setting.value] = self._shown_value(setting)

        return values

    def parse(self, form: Mapping[str, Any]) -> SettingsView:
        """Форма панели -> представление с новыми личными настройками."""
        return self._view.edited(form, self.shown_values())

    def _shown_value(self, setting: UserSetting) -> Any:
        value = self._view.value_of(setting)

        if setting is UserSetting.STOP:
            return list(value)

        if setting in self.NUMERIC:
            return self._numeric_value(setting, value)

        if setting is UserSetting.REASONING_EFFORT and value is not None:
            return value.value

        return value

    def _numeric_value(self, setting: UserSetting, value: Any) -> float | int:
        """Число для слайдера: профильное либо стартовое из [settings]."""
        shown = self._bounds_of(setting).default
        if value is not None:
            shown = value

        if setting in self.INTEGER:
            return int(shown)

        return float(shown)

    def _tab_widgets(
        self,
        settings: Sequence[UserSetting],
        effective: AgentSettings,
    ) -> list[InputWidget]:
        widgets: list[InputWidget] = []
        for setting in settings:
            if not self._profile.setting_allowed(setting.value):
                continue

            widget = self._widget_of(setting, effective)
            if widget is None:
                continue

            widgets.append(widget)

        return widgets

    def _widget_of(
        self,
        setting: UserSetting,
        effective: AgentSettings,
    ) -> InputWidget | None:
        if setting is UserSetting.MODEL:
            return self._model_select(effective)

        if setting is UserSetting.REASONING_EFFORT:
            return self._effort_select(effective)

        if setting is UserSetting.SEED:
            return self._seed_input(effective)

        if setting is UserSetting.STOP:
            return Tags(
                id=setting.value,
                label=self._text.label(setting),
                description=self._text.description(setting),
                initial=list(effective.stop),
            )

        if setting is UserSetting.USER_PROMPT:
            return self._prompt_input()

        return self._slider(setting, effective)

    def _model_select(self, effective: AgentSettings) -> Select | None:
        """Модели профиля; выбрана та, что работает сейчас."""
        if not self._profile.models:
            return None

        return Select(
            id=UserSetting.MODEL.value,
            label=self._text.label(UserSetting.MODEL),
            description=self._text.description(UserSetting.MODEL),
            values=list(self._profile.models),
            initial_value=effective.model,
        )

    def _effort_select(self, effective: AgentSettings) -> Select:
        effort = None
        if effective.reasoning_effort is not None:
            effort = effective.reasoning_effort.value

        values: list[str] = []
        for level in ReasoningEffort:
            values.append(level.value)

        return Select(
            id=UserSetting.REASONING_EFFORT.value,
            label=self._text.label(UserSetting.REASONING_EFFORT),
            description=self._text.description(UserSetting.REASONING_EFFORT),
            values=values,
            initial_value=effort,
        )

    def _seed_input(self, effective: AgentSettings) -> NumberInput:
        seed = None
        if effective.seed is not None:
            seed = float(effective.seed)

        return NumberInput(
            id=UserSetting.SEED.value,
            label=self._text.label(UserSetting.SEED),
            description=self._text.description(UserSetting.SEED),
            initial=seed,
        )

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

    def _slider(self, setting: UserSetting, effective: AgentSettings) -> Slider:
        bounds = self._bounds_of(setting)
        value = getattr(effective, setting.value)

        return Slider(
            id=setting.value,
            label=self._text.label(setting),
            description=self._text.description(setting),
            initial=float(self._numeric_value(setting, value)),
            min=bounds.low,
            max=bounds.high,
            step=bounds.step,
        )

    def _bounds_of(self, setting: UserSetting) -> NumberBounds:
        by_setting = {
            UserSetting.TEMPERATURE: self._bounds.temperature,
            UserSetting.TOP_P: self._bounds.top_p,
            UserSetting.MAX_TOKENS: self._bounds.max_tokens,
            UserSetting.FREQUENCY_PENALTY: self._bounds.frequency_penalty,
            UserSetting.PRESENCE_PENALTY: self._bounds.presence_penalty,
            UserSetting.HISTORY_MESSAGES: self._bounds.history_messages,
        }
        return by_setting[setting]
