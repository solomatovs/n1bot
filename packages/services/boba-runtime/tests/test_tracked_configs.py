"""Файлы плагинов сервиса лежат в git: секрет или адрес машины попасть в них не
должен. Значение поля, похожего на секрет или адрес, — только ссылка на секцию
конфига экземпляра; литерал допустим лишь из явного списка исключений."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import ClassVar

import pytest


class TrackedConfigs:
    """Отслеживаемые файлы конфигурации приложений и правило для их значений."""

    APPS: ClassVar[Path] = Path(__file__).resolve().parents[3] / "apps"
    SENSITIVE: ClassVar[re.Pattern[str]] = re.compile(
        r"(password|passwd|secret|(^|_)token$|api_key|_key$|^key$|host|url|addr"
        r"|server$|^model$|allowed_clients|mapping$|sender|recipient|prompt$)",
        re.IGNORECASE,
    )
    REFERENCE: ClassVar[re.Pattern[str]] = re.compile(r"^\$\{[a-z_][\w.${}-]*\}$")
    ALLOWED: ClassVar[Mapping[str, str]] = {}
    """Поля с литералом по делу: путь поля -> почему это не секрет и не адрес."""
    PARTIAL_REFERENCE: ClassVar[re.Pattern[str]] = re.compile(r"\$\{[a-z_][\w.]*\}")
    """Строка, собранная из ссылок (${instance.data_dir}/dump, FILE:${a.b}), литералом
    машины не является: машинная часть приходит из конфига экземпляра."""

    def files(self) -> list[Path]:
        return sorted(self.APPS.glob("boba-*/conf/**/*.toml"))

    def violations(self, path: Path) -> list[str]:
        with path.open("rb") as body:
            sections = tomllib.load(body)

        prefix = ""
        if path.parent.name == "plugins":
            prefix = f"tool.{path.stem}."

        found: list[str] = []
        for field, value in self._leaves(sections, prefix):
            name = field.rsplit(".", 1)[-1]
            if not self.SENSITIVE.search(name):
                continue
            if field in self.ALLOWED:
                continue
            if self._referenced(value):
                continue

            found.append(f"{path.relative_to(self.APPS)}: {field}")

        return found

    def _referenced(self, value: object) -> bool:
        if isinstance(value, bool | int | float):
            return False
        if isinstance(value, str):
            return bool(self.PARTIAL_REFERENCE.search(value)) or bool(
                self.REFERENCE.match(value)
            )
        if isinstance(value, list):
            return bool(value) and all(self._referenced(item) for item in value)

        return False

    def _leaves(self, node: object, prefix: str) -> Iterator[tuple[str, object]]:
        if isinstance(node, dict):
            for key, value in node.items():
                yield from self._leaves(value, f"{prefix}{key}.")
            return

        yield prefix.rstrip("."), node


class TestTrackedConfigs:
    def test_configs_are_found(self) -> None:
        names: set[str] = set()
        for path in TrackedConfigs().files():
            names.add(path.relative_to(TrackedConfigs.APPS).parts[0])

        if "boba-mcp" not in names:
            raise AssertionError(f"plugin files of boba-mcp exist: {names}")

    @pytest.mark.parametrize(
        "path", TrackedConfigs().files(), ids=lambda path: str(path)[-48:]
    )
    def test_no_literal_in_a_sensitive_field(self, path: Path) -> None:
        found = TrackedConfigs().violations(path)

        if found:
            raise AssertionError(
                "literal in a field that looks like a secret or an address; move "
                "the value to the instance config.toml and refer to its section:\n"
                + "\n".join(found)
            )
