"""Снимки истории результатов инструментов: проверка каталога tests/history.

Чекпойнтер хранит результаты дольше кода, поэтому каждый вид результата
держит снимок того, что лежало в истории. Вид живёт в пакете, который его
производит, и там же лежит его снимок; проверка у всех пакетов одна.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import ClassVar

from boba.toolkit.dag import DagNode, NodeOutcomes
from boba.toolkit.result import ResultKinds, ToolArtifact
from boba.toolkit.wire import ResultWire

__all__ = ["HistorySnapshots"]


class HistorySnapshots:
    """Снимки истории одного пакета: каталог снимков и модули его видов.

    package — префикс модулей пакета (например "boba.toolkit"): по нему из
    общего реестра видов берутся только свои. Создаёт его тест пакета.
    """

    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, directory: Path, package: str) -> None:
        self._directory = directory
        self._package = package
        self._wire = ResultWire()
        self._outcomes = NodeOutcomes()

    def paths(self) -> Sequence[Path]:
        return sorted(self._directory.glob("*.json"))

    def missing(self) -> Sequence[str]:
        """Виды пакета без снимка: их историю никто не проверяет."""
        covered: set[str] = set()
        for path in self.paths():
            covered.add(str(self._stored(path)["kind"]))

        return sorted(set(self._own_kinds()) - covered)

    def check(self, path: Path) -> None:
        """Снимок восстанавливается своим видом и уходит клиенту блоками MCP."""
        stored = self._stored(path)

        revived = ToolArtifact.revive(stored)
        if revived is None:
            raise AssertionError(f"{path.name}: kind {stored['kind']!r} is unknown")

        if revived.kind != stored["kind"]:
            raise AssertionError(f"{path.name}: revived as {revived.kind}")

        if not revived.llm_view():
            raise AssertionError(f"{path.name}: empty llm view")

        call = DagNode(key=path.stem, tool=path.stem)
        wire = self._wire.packed(self._outcomes.of(call, revived, False))
        if not wire.content:
            raise AssertionError(f"{path.name}: no content blocks")

    def _stored(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding=self.ENCODING))

    def _own_kinds(self) -> Iterator[str]:
        for kind in ResultKinds.kinds():
            model = ResultKinds.of(kind)
            if model is None:
                continue

            if model.__module__.startswith(self._package):
                yield kind
