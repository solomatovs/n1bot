"""Язык вкладки по сессии chainlit и подписи панели вопроса агента в
переводах chainlit на обоих языках интерфейса."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.aid.infra.session import current_session


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: тесты подписей не ходят в контекст chainlit."""


class TestTabLanguage:
    """Язык вкладки совпадает с языком остального интерфейса chainlit."""

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

        # вне ws-сессии языка нет: подписи возьмут язык по умолчанию
        if current_session().language != "":
            raise AssertionError(f"language: {current_session().language!r}")


class TestQuestionLabels:
    """Каждый ключ aid.question, который берут элемент Question и
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
        r"(?:\$\{TEXT\}|aid\.question)\.([A-Za-z]+(?:\.[A-Za-z]+)*)"
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
        return json.loads(path.read_text(encoding="utf-8"))["aid"]["question"]

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
