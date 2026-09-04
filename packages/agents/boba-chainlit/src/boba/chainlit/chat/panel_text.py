"""Подписи панели настроек: строки живут в переводах формата chainlit.

Ключ chat.settings.llm; файлы поставляются с пакетом (chat/translations), а
развёртывание может переопределить их своими в <root>/.chainlit/translations.
Ошибки:
PanelTextError — в переводах нет строки ни на языке пользователя, ни на языке
    по умолчанию.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from boba.chat.profiles import UserSetting

__all__ = ["PanelText", "PanelTextError"]


class PanelTextError(KeyError):
    """В переводах нет строки панели настроек."""


class TextKey(StrEnum):
    """Путь до строк панели внутри файла переводов."""

    CHAT = "chat"
    SETTINGS = "settings"
    LLM = "llm"
    TABS = "tabs"
    FIELDS = "fields"
    LABEL = "label"
    DESCRIPTION = "description"


class TranslationFiles:
    """Файлы переводов в каталоге: <каталог>/<язык>.json."""

    SUFFIX: ClassVar[str] = ".json"

    def __init__(self, directory: Path) -> None:
        self._root = directory

    def read(self, language: str) -> Mapping[str, Any]:
        """Словарь перевода; точного файла нет — берётся тот же язык другого региона."""
        for path in self._candidates(language):
            if not path.is_file():
                continue

            return json.loads(path.read_text(encoding="utf-8"))

        return {}

    def _candidates(self, language: str) -> list[Path]:
        """Файлы по убыванию точности: ru-RU, затем ru, затем любой ru-*.

        Браузер шлёт то `ru-RU`, то `ru`, а файлы переводов chainlit названы
        по региону — без последнего шага `ru` осталось бы без перевода.
        """
        base = language.split("-", maxsplit=1)[0]

        found = [self._root / f"{language}{self.SUFFIX}"]
        if base != language:
            found.append(self._root / f"{base}{self.SUFFIX}")

        found.extend(sorted(self._root.glob(f"{base}-*{self.SUFFIX}")))
        return found


class PanelText:
    """Строки панели на языке пользователя; без перевода — язык по умолчанию."""

    DEFAULT_LANGUAGE: ClassVar[str] = "en-US"

    PACKAGE_DIRECTORY: ClassVar[Path] = Path(__file__).parent / "translations"
    """Строки, поставляемые с кодом: они есть в любом развёртывании."""

    APP_ROOT_DIRECTORY: ClassVar[tuple[str, str]] = (".chainlit", "translations")
    """Переводы chainlit развёртывания: там строки можно переопределить."""

    def __init__(self, root: str, language: str) -> None:
        if not language:
            language = self.DEFAULT_LANGUAGE

        packaged = TranslationFiles(self.PACKAGE_DIRECTORY)
        deployed = TranslationFiles(Path(root).joinpath(*self.APP_ROOT_DIRECTORY))

        self._texts = self._merged(deployed, packaged, language)
        self._fallback = self._merged(deployed, packaged, self.DEFAULT_LANGUAGE)

    @classmethod
    def _merged(
        cls,
        deployed: TranslationFiles,
        packaged: TranslationFiles,
        language: str,
    ) -> dict[str, Any]:
        """Строки развёртывания поверх строк пакета."""
        merged: dict[str, Any] = {}
        for source in (packaged, deployed):
            section = cls._llm_section(source.read(language))
            for group, entries in section.items():
                group_texts = merged.setdefault(group, {})
                group_texts.update(entries)

        return merged

    def tab(self, tab_id: str) -> str:
        return self._string(TextKey.TABS, tab_id, TextKey.LABEL)

    def label(self, setting: UserSetting) -> str:
        return self._string(TextKey.FIELDS, setting.value, TextKey.LABEL)

    def description(self, setting: UserSetting) -> str:
        return self._string(TextKey.FIELDS, setting.value, TextKey.DESCRIPTION)

    def _string(self, group: TextKey, name: str, field: TextKey) -> str:
        found = self._lookup(self._texts, group, name, field)
        if found is not None:
            return found

        found = self._lookup(self._fallback, group, name, field)
        if found is not None:
            return found

        msg = (
            f"chat.settings.llm.{group.value}.{name}.{field.value}: no translation "
            "in the language texts or in the fallback"
        )
        raise PanelTextError(msg)

    @staticmethod
    def _lookup(
        texts: Mapping[str, Any],
        group: TextKey,
        name: str,
        field: TextKey,
    ) -> str | None:
        entry = texts.get(group.value, {}).get(name)

        # вкладка описана одной строкой, поле — парой label/description
        if isinstance(entry, str):
            return entry

        if not isinstance(entry, Mapping):
            return None

        value = entry.get(field.value)
        if isinstance(value, str):
            return value

        return None

    @staticmethod
    def _llm_section(translation: Mapping[str, Any]) -> Mapping[str, Any]:
        chat = translation.get(TextKey.CHAT.value, {})
        settings = chat.get(TextKey.SETTINGS.value, {})
        return settings.get(TextKey.LLM.value, {})
