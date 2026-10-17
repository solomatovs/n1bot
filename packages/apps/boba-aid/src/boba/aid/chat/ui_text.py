"""Подписи интерфейса чата из переводов chainlit развёртывания.

Все строки интерфейса aid — фронта, ленты и панели настроек — живут в
<root>/.chainlit/translations/<язык>.json в формате chainlit: фронт получает их
маршрутом /project/translations, сервер читает здесь те же файлы.

Ошибки:
UiTextError — строки нет ни на языке интерфейса, ни на языке по умолчанию.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

__all__ = ["TranslationFiles", "UiText", "UiTextError"]


class UiTextError(KeyError):
    """В переводах нет строки интерфейса."""


class TranslationFiles:
    """Файлы переводов в каталоге: <каталог>/<язык>.json."""

    SUFFIX: ClassVar[str] = ".json"

    def __init__(self, directory: Path) -> None:
        self._root = directory

    @property
    def directory(self) -> Path:
        return self._root

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


class UiText:
    """Строки интерфейса на одном языке: ключ — путь через точку, как у
    i18next фронта chainlit (`aid.feed.thinking`); подстановки — `{{имя}}`.

    Без строки на языке интерфейса берётся язык по умолчанию. Создают его
    провайдер приложения (подписи ленты на языке развёртывания) и панель
    настроек (язык вкладки).
    """

    DEFAULT_LANGUAGE: ClassVar[str] = "en-US"
    DIRECTORY: ClassVar[tuple[str, str]] = (".chainlit", "translations")
    SEPARATOR: ClassVar[str] = "."

    def __init__(self, root: Path, language: str) -> None:
        if not language:
            language = self.DEFAULT_LANGUAGE

        self._files = TranslationFiles(root.joinpath(*self.DIRECTORY))
        self._language = language
        self._texts = self._files.read(language)
        self._fallback = self._files.read(self.DEFAULT_LANGUAGE)

    @property
    def language(self) -> str:
        return self._language

    def text(self, key: str, **values: object) -> str:
        """Строка по ключу с подстановками.

        Ошибки:
        UiTextError — строки нет ни на языке интерфейса, ни на языке по умолчанию.
        """
        found = self._lookup(self._texts, key)
        if found is None:
            found = self._lookup(self._fallback, key)

        if found is None:
            msg = (
                f"{key}: no translation for {self._language} or "
                f"{self.DEFAULT_LANGUAGE} in {self._files.directory}"
            )
            raise UiTextError(msg)

        for name, value in values.items():
            found = found.replace(f"{{{{{name}}}}}", str(value))

        return found

    def _lookup(self, texts: Mapping[str, Any], key: str) -> str | None:
        node: object = texts
        for part in key.split(self.SEPARATOR):
            if not isinstance(node, Mapping):
                return None

            node = node.get(part)

        if isinstance(node, str):
            return node

        return None
