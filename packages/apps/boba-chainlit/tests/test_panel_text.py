"""Подписи панели настроек агента: свой язык, откат на язык по умолчанию,
полнота переводов вкладок и полей, язык панели по сессии chainlit."""

from __future__ import annotations

import pytest

from boba.chainlit.agent.panel import AgentPanelTab
from boba.chainlit.chat.panel_text import PanelText, PanelTextError
from boba.chainlit.infra.session import current_session
from boba.chat.profiles import UserSetting

NO_OVERRIDES = ""
"""Пустой app_root: строки берутся из пакета, как в чистом развёртывании."""

SESSION_FIELDS = ("session_mode", "session_model")
"""Виджеты вкладки «Сессия»: категории настроек агента с префиксом."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: тесты подписей не ходят в контекст chainlit."""


class TestPanelText:
    def test_russian_strings_are_translated(self) -> None:
        text = PanelText(NO_OVERRIDES, "ru-RU")

        label = text.field_label(UserSetting.AGENT_MODEL.value)
        if label != "Модель":
            raise AssertionError(f"label: {label!r}")
        if text.tab(AgentPanelTab.AGENT.value) != "Агент":
            raise AssertionError(f"tab: {text.tab(AgentPanelTab.AGENT.value)!r}")

    def test_unknown_language_falls_back_to_default(self) -> None:
        text = PanelText(NO_OVERRIDES, "de-DE")

        label = text.field_label(UserSetting.AGENT_BASE_URL.value)
        if label != "Provider URL":
            raise AssertionError(f"label: {label!r}")

    def test_every_tab_and_field_is_translated(self) -> None:
        fields = [setting.value for setting in UserSetting]
        fields.extend(SESSION_FIELDS)
        for language in ("en-US", "ru-RU"):
            text = PanelText(NO_OVERRIDES, language)

            for field in fields:
                if not text.field_label(field):
                    raise AssertionError(f"{language}: no label for {field}")
                if not text.field_description(field):
                    raise AssertionError(f"{language}: no hint for {field}")

            for tab in AgentPanelTab:
                if not text.tab(tab.value):
                    raise AssertionError(f"{language}: no label for tab {tab.value}")

    def test_retired_prompt_strings_are_gone(self) -> None:
        text = PanelText(NO_OVERRIDES, PanelText.DEFAULT_LANGUAGE)

        with pytest.raises(PanelTextError, match="no translation"):
            text.field_label("user_prompt")

        with pytest.raises(PanelTextError, match="no translation"):
            text.tab("prompt_tab")


class TestPanelLanguage:
    """Язык панели совпадает с языком остального интерфейса chainlit."""

    def test_forced_ui_language_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from chainlit.config import config as chainlit_config

        monkeypatch.setattr(chainlit_config.ui, "language", "ru-RU")

        if current_session().language != "ru-RU":
            raise AssertionError(f"language: {current_session().language!r}")

    def test_without_forced_language_falls_to_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chainlit.config import config as chainlit_config

        monkeypatch.setattr(chainlit_config.ui, "language", None)

        # вне ws-сессии языка нет: панель возьмёт язык по умолчанию
        if current_session().language != "":
            raise AssertionError(f"language: {current_session().language!r}")
