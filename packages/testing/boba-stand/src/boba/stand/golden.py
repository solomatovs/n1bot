"""Золотые снимки запросов к модели: тела всех запросов теста агента на
стенде модели сверяются с эталоном байт в байт.

Снимок защищает сборку запроса из истории при переделках записей и
сборщика: любое изменение текста, порядка или состава сообщений видно
расхождением с эталоном. Эталоны лежат файлами JSON рядом с тестами; опция
`--golden-update` переснимает их, прогон при этом падает, а новые эталоны
принимает git.

Ошибки:
AssertionError — тела запросов теста расходятся с эталоном, эталона нет
    или он переснят.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = ["GoldenOption", "GoldenRequests", "RequestSnapshot"]


class GoldenOption(StrEnum):
    """Опция pytest: переснять эталоны запросов. Объявляет её корневой
    conftest, чтобы прогон любого корня тестов её знал."""

    UPDATE = "--golden-update"

    @property
    def dest(self) -> str:
        return self.value.removeprefix("--").replace("-", "_")


class RequestKey(StrEnum):
    """Ключи тела запроса, под которыми лежат идентификаторы вызовов."""

    TOOL_CALLS = "tool_calls"
    TOOL_CALL_ID = "tool_call_id"
    ID = "id"


class SnapshotMark(StrEnum):
    """Метки, которыми снимок заменяет изменчивые части запроса."""

    CALL = "call-{number}"
    TODAY = "<today>"
    DATE = "%Y-%m-%d"


class RequestSnapshot:
    """Тела запросов теста в каноническом тексте.

    Идентификаторы вызовов, которые агент чеканит случайно, заменяются
    порядковыми метками в порядке первого появления; сегодняшняя дата —
    меткой: тесты на системных часах объявляют модели текущий день.
    """

    def text(self, requests: Sequence[Mapping[str, Any]]) -> str:
        text = json.dumps(list(requests), ensure_ascii=False, indent=1)
        for number, call_id in enumerate(self._call_ids(requests), start=1):
            text = text.replace(call_id, SnapshotMark.CALL.format(number=number))

        for day in self._today():
            text = text.replace(day, SnapshotMark.TODAY.value)

        return text + "\n"

    def _call_ids(self, requests: Sequence[Mapping[str, Any]]) -> list[str]:
        seen: dict[str, None] = {}
        for request in requests:
            for call_id in self._ids_in(request):
                seen[call_id] = None

        return list(seen)

    def _ids_in(self, node: Any) -> Iterator[str]:
        if isinstance(node, list):
            for item in node:
                yield from self._ids_in(item)

            return

        if not isinstance(node, Mapping):
            return

        for key, value in node.items():
            if key == RequestKey.TOOL_CALL_ID.value and isinstance(value, str):
                yield value
                continue

            if key == RequestKey.TOOL_CALLS.value and isinstance(value, list):
                yield from self._call_list_ids(value)

            yield from self._ids_in(value)

    @staticmethod
    def _call_list_ids(calls: Sequence[Any]) -> Iterator[str]:
        for call in calls:
            if not isinstance(call, Mapping):
                continue

            call_id = call.get(RequestKey.ID.value)
            if isinstance(call_id, str):
                yield call_id

    @staticmethod
    def _today() -> list[str]:
        utc = datetime.now(UTC).strftime(SnapshotMark.DATE.value)
        local = datetime.now().astimezone().strftime(SnapshotMark.DATE.value)

        return sorted({utc, local})


class GoldenRequests:
    """Эталоны запросов набора тестов: файл на тест в каталоге root.

    Создаёт автоматическая фикстура набора; check сверяет снимок запросов
    теста с его эталоном либо, в режиме update, переписывает эталон.
    """

    def __init__(self, root: Path, update: bool) -> None:
        self._root = root
        self._update = update
        self._snapshot = RequestSnapshot()

    def check(self, nodeid: str, requests: Sequence[Mapping[str, Any]]) -> None:
        path = self._path(nodeid)
        text = self._snapshot.text(requests)
        if self._update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            msg = (
                f"golden requests of {nodeid} were rewritten at {path}; review the "
                "change with git and rerun without --golden-update"
            )
            raise AssertionError(msg)

        if not path.is_file():
            msg = (
                f"golden requests of {nodeid} are missing: expected the file {path}. "
                "Run the test with --golden-update to write it."
            )
            raise AssertionError(msg)

        expected = path.read_text(encoding="utf-8")
        if text == expected:
            return

        msg = (
            f"requests of {nodeid} differ from the golden file {path}: "
            f"{self._first_difference(expected, text)}"
        )
        raise AssertionError(msg)

    def _path(self, nodeid: str) -> Path:
        """Файл эталона: модуль теста каталогом, путь теста в нём — именем."""
        module, _, name = nodeid.partition("::")
        stem = Path(module).stem

        return self._root / stem / f"{name.replace('::', '.')}.json"

    @staticmethod
    def _first_difference(expected: str, actual: str) -> str:
        expected_lines = expected.splitlines()
        actual_lines = actual.splitlines()
        for number, (want, got) in enumerate(
            zip(expected_lines, actual_lines, strict=False), start=1
        ):
            if want != got:
                return f"line {number}: expected {want[:300]!r}, got {got[:300]!r}"

        return f"expected {len(expected_lines)} lines, got {len(actual_lines)} lines"
