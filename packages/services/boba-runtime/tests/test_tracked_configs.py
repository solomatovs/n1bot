"""Общие конфиги приложений лежат в git: секрет или адрес этой машины попасть в них
не должен. Значение поля, похожего на секрет или адрес, — только ссылка на site-файл
либо на секцию окружения; литерал допустим лишь из явного списка исключений."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import ClassVar

import pytest

from boba.runtime.config import AppLayers


class TrackedConfigs:
    """Отслеживаемые файлы конфигурации приложений и правило для их значений."""

    APPS: ClassVar[Path] = Path(__file__).resolve().parents[3] / "apps"
    SENSITIVE: ClassVar[re.Pattern[str]] = re.compile(
        r"(password|passwd|secret|(^|_)token$|api_key|_key$|^key$|host|url|addr"
        r"|server$|^model$|allowed_clients|mapping$|sender|recipient)",
        re.IGNORECASE,
    )
    REFERENCE: ClassVar[re.Pattern[str]] = re.compile(r"^\$\{[a-z_][\w.${}-]*\}$")
    ALLOWED: ClassVar[Mapping[str, str]] = {}
    """Поля с литералом по делу: путь поля -> почему это не секрет и не адрес."""
    PARTIAL_REFERENCE: ClassVar[re.Pattern[str]] = re.compile(r"\$\{(site|env)\.")
    """Строка, собранная из ссылок (FILE:${site.x}, ${env.krb}/file), литералом
    машины не является: машинная часть приходит из site-файла."""

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
        names = {path.parent.parent.name for path in TrackedConfigs().files()}
        names |= {path.parent.name for path in TrackedConfigs().files()}

        if not {"boba-chainlit", "boba-mcp", "boba-studio"} <= names:
            raise AssertionError(f"common configs of the three apps exist: {names}")

    @pytest.mark.parametrize(
        "path", TrackedConfigs().files(), ids=lambda path: str(path)[-48:]
    )
    def test_no_literal_in_a_sensitive_field(self, path: Path) -> None:
        found = TrackedConfigs().violations(path)

        if found:
            raise AssertionError(
                "literal in a field that looks like a secret or an address; move "
                "the value to site.toml and refer to it as ${site.*}:\n"
                + "\n".join(found)
            )

    @pytest.mark.parametrize(
        "path",
        sorted(TrackedConfigs.APPS.glob("boba-*/conf/config.toml")),
        ids=lambda path: path.parent.parent.name,
    )
    def test_common_config_has_no_site_sections(self, path: Path) -> None:
        with path.open("rb") as body:
            sections = tomllib.load(body)

        misplaced = AppLayers.SITE_SECTIONS & set(sections)
        if misplaced:
            raise AssertionError(f"{path}: sections {misplaced} belong to site.toml")
