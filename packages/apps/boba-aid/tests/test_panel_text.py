"""Подписи панели настроек агента: свой язык, откат на язык по умолчанию,
полнота переводов вкладок и полей, язык панели по сессии chainlit; подписи
панели вопроса агента — в переводах chainlit на обоих языках интерфейса."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.aid.agent.panel import AgentPanelTab
from boba.aid.chat.panel_text import PanelText, PanelTextError
from boba.aid.infra.session import current_session
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


class TestQuestionLabels:
    """Каждый ключ components.Question, который берут элемент Question и
    overlay Footer, есть в переводах chainlit en-US и ru-RU; у счётчика
    вопросов — формы единственного и множественного числа."""

    PACKAGE: ClassVar[Path] = Path(__file__).resolve().parents[1]
    TRANSLATIONS: ClassVar[Path] = PACKAGE / "assets" / ".chainlit" / "translations"
    SOURCES: ClassVar[tuple[Path, ...]] = (
        PACKAGE / "assets" / "public" / "elements" / "Question.jsx",
        PACKAGE
        / "web"
        / "chainlit-ui"
        / "overlay"
        / "frontend"
        / "src"
        / "components"
        / "chat"
        / "Footer.tsx",
    )
    USED: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:\$\{TEXT\}|components\.Question)\.([A-Za-z]+(?:\.[A-Za-z]+)*)"
    )
    STATUSES: ClassVar[tuple[str, ...]] = (
        "status.accept",
        "status.decline",
        "status.cancel",
    )
    """Ключи статуса записи: элемент собирает их по исходу вопроса."""
    COUNTED: ClassVar[str] = "questions"
    PLURALS: ClassVar[tuple[str, ...]] = ("_one", "_other")

    def _used(self) -> set[str]:
        keys: set[str] = set()
        for source in self.SOURCES:
            keys.update(self.USED.findall(source.read_text(encoding="utf-8")))

        keys.discard("status")
        keys.update(self.STATUSES)
        return keys

    def _labels(self, language: str) -> dict[str, Any]:
        path = self.TRANSLATIONS / f"{language}.json"
        return json.loads(path.read_text(encoding="utf-8"))["components"]["Question"]

    @pytest.mark.parametrize("language", ["en-US", "ru-RU"])
    def test_every_used_label_is_translated(self, language: str) -> None:
        labels = self._labels(language)
        used = self._used()
        assert {"submit", "held", "other", "title.one"} <= used, used

        for key in sorted(used):
            if key == self.COUNTED:
                for plural in self.PLURALS:
                    assert labels.get(f"{key}{plural}"), f"{language}: {key}{plural}"

                continue

            node: Any = labels
            for part in key.split("."):
                assert isinstance(node, dict), f"{language}: {key} at {part}"
                assert part in node, f"{language}: no label {key}"
                node = node[part]

            assert node, f"{language}: empty label {key}"
