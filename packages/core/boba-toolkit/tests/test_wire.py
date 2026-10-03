"""Итог вызова проходит формат MCP туда и обратно без потерь.

Каждый вид результата проверяется на снимках истории: тех же, по которым
проверяется оживление сохранённых сообщений.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boba.stand_core.history import HistorySnapshots
from boba.toolkit.chain import GroupFailureResult
from boba.toolkit.dag import WorkflowResult
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    TableResult,
    ToolArtifact,
)
from boba.toolkit.wire import CallStatus, ResultWire, WireMeta, WireResult

SNAPSHOTS = HistorySnapshots(Path(__file__).parent / "history", "boba.toolkit")

WIRE = ResultWire()


def _snapshot(path: Path):
    """Результат семейства из снимка истории."""
    revived = ToolArtifact.revive(json.loads(path.read_text(encoding="utf-8")))
    if revived is None:
        raise AssertionError(f"снимок {path.name} оживает в результат семейства")

    return revived


class TestRoundTrip:
    def test_kinds_of_other_modules_are_registered(self) -> None:
        """Виды, объявленные вне result.py, попадают в реестр при импорте."""
        if GroupFailureResult.declared_kind() is None:
            raise AssertionError("вид срыва группы объявлен в boba.toolkit.chain")
        if WorkflowResult.declared_kind() is None:
            raise AssertionError("вид workflow объявлен в boba.toolkit.dag")

    @pytest.mark.parametrize("path", SNAPSHOTS.paths(), ids=lambda path: path.stem)
    def test_every_kind_survives_the_wire(self, path: Path) -> None:
        artifact = _snapshot(path)

        wire = WIRE.packed(artifact.llm_view(), artifact, CallStatus.SUCCESS, "call_1")
        sent = json.loads(json.dumps(wire.structured))
        revived = WIRE.revived(
            WireResult(
                content=wire.content,
                structured=sent,
                is_error=wire.is_error,
                meta=json.loads(json.dumps(wire.meta)),
            )
        )

        if revived.artifact != artifact:
            raise AssertionError(f"результат вида {path.stem} оживает тем же")
        if type(revived.artifact) is not type(artifact):
            raise AssertionError(f"класс результата тот же: {type(revived.artifact)}")
        if revived.content != artifact.llm_view():
            raise AssertionError("текст для модели доехал как есть")
        if revived.call_id != "call_1":
            raise AssertionError(f"идентификатор вызова доехал: {revived.call_id!r}")


class TestErrorFlag:
    def test_failure_result_is_an_error_even_with_success_status(self) -> None:
        """Тело вернуло отказ упакованным результатом: статус вызова success,
        а по MCP это сбой; клиент получает статус как был."""
        refusal = ErrorResult(message="no such table", error_kind="probe")

        wire = WIRE.packed(refusal.llm_view(), refusal, CallStatus.SUCCESS, "c")

        if not wire.is_error:
            raise AssertionError("отказ тела — сбой по MCP")

        revived = WIRE.revived(wire)
        if revived.status is not CallStatus.SUCCESS:
            raise AssertionError(f"статус вызова восстановлен: {revived.status}")
        if not isinstance(revived.artifact, FailureResult):
            raise AssertionError(f"отказ оживает отказом: {revived.artifact!r}")

    def test_error_status_is_an_error(self) -> None:
        refusal = ErrorResult(message="bad arguments", error_kind="probe")

        wire = WIRE.packed(refusal.llm_view(), refusal, CallStatus.ERROR, "c")

        if not wire.is_error:
            raise AssertionError("сорванный вызов — сбой по MCP")
        if WIRE.revived(wire).status is not CallStatus.ERROR:
            raise AssertionError("статус error восстановлен")

    def test_plain_result_is_not_an_error(self) -> None:
        table = TableResult(rows=[{"a": 1}])

        wire = WIRE.packed(table.llm_view(), table, CallStatus.SUCCESS, "c")

        if wire.is_error:
            raise AssertionError("обычный результат — не сбой")


class TestForeignServer:
    """Сервер без наших полей и с чужим содержимым: клиент показывает текст."""

    def test_unknown_kind_falls_back_to_the_text(self) -> None:
        wire = WireResult(
            content="plain text",
            structured={"kind": "kind_of_a_newer_server", "x": 1},
            is_error=False,
            meta={},
        )

        revived = WIRE.revived(wire)

        if revived.artifact is not None:
            raise AssertionError(f"неизвестный вид не оживает: {revived.artifact!r}")
        if revived.content != "plain text":
            raise AssertionError("остаётся текст для модели")

    def test_status_comes_from_the_mcp_flag_without_our_meta(self) -> None:
        wire = WireResult(content="boom", structured=None, is_error=True, meta={})

        revived = WIRE.revived(wire)

        if revived.status is not CallStatus.ERROR:
            raise AssertionError(f"сбой чужого сервера — статус error: {revived}")
        if revived.call_id:
            raise AssertionError(f"идентификатора вызова нет: {revived.call_id!r}")

    def test_meta_of_another_shape_is_ignored(self) -> None:
        wire = WireResult(
            content="ok",
            structured=None,
            is_error=False,
            meta={WireMeta.NAMESPACE.value: "not a mapping"},
        )

        if WIRE.revived(wire).status is not CallStatus.SUCCESS:
            raise AssertionError("чужая форма meta не мешает")
