"""Контракт ошибок payload'а: ожидаемое едет кадром, неожиданное — трейсбеком."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from boba.toolkit.launcher import LaunchPayload
from boba.toolkit.payload import ChunkEmitter, PayloadEntry, PayloadError


class Request(BaseModel):
    """Схема запроса тестовых операций: ловит невалидный вход."""

    op: str = Field(min_length=1)
    size: int = Field(ge=1)


class UnreadableError(Exception):
    """Сторонний тип, объявленный инструментом ожидаемым."""


class SilentError(Exception):
    """Ожидаемый тип с пустым текстом: у части библиотек так и бывает."""


class Ops:
    """Тестовый payload: у каждой операции свой способ упасть."""

    LOG: ClassVar[logging.Logger] = logging.getLogger("boba.tool.probe")

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        UnreadableError: "document_unreadable",
        SilentError: "silent_failure",
    }

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        req = Request.model_validate(request)
        if req.op == "logs":
            cls.LOG.info("работаю: size=%d", req.size)
            cls.LOG.warning("что-то подозрительное")
            return {"size": req.size}
        if req.op == "ok":
            emit("данные")
            return {"size": req.size}
        if req.op == "declared":
            raise UnreadableError("формат .md не читается")
        if req.op == "silent":
            raise SilentError
        if req.op == "explicit":
            raise PayloadError("bad_spec", "спека не по схеме")
        raise KeyError(req.op)


def _run(entry: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    """Запуск точки входа на готовом запросе; кадры разбираются как на хосте."""
    monkey = pytest.MonkeyPatch()
    monkey.setattr("sys.stdin", _Stdin(json.dumps(entry)))
    try:
        code = PayloadEntry.main(Ops)
    finally:
        monkey.undo()
    return code, capsys.readouterr()


class _Stdin:
    """Подменённый stdin: запрос payload читает целиком, как из пайпа."""

    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> str:
        return self._body


def _frames(stdout: str, marker: str) -> list[Any]:
    bodies: list[Any] = []
    for line in stdout.splitlines():
        if line.startswith(marker):
            bodies.append(json.loads(line[len(marker) :]))
    return bodies


class TestExpectedErrors:
    """Объявленный отказ уезжает кадром: пользователь видит причину, не стек."""

    def test_declared_type_becomes_error_frame(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, captured = _run({"op": "declared", "size": 1}, capsys)
        frames = _frames(captured.out, LaunchPayload.ERROR_MARKER)
        assert code != 0
        assert len(frames) == 1
        assert frames[0]["kind"] == "document_unreadable"
        assert "формат .md не читается" in frames[0]["message"]
        assert not _frames(captured.out, LaunchPayload.MARKER)

    def test_explicit_payload_error_keeps_its_wording(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, captured = _run({"op": "explicit", "size": 1}, capsys)
        frame = _frames(captured.out, LaunchPayload.ERROR_MARKER)[0]
        assert (frame["kind"], frame["message"]) == ("bad_spec", "спека не по схеме")

    def test_invalid_request_is_expected_everywhere(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Запрос не по контракту объявлять не нужно: это общее для всех."""
        _, captured = _run({"op": "ok", "size": 0}, capsys)
        frame = _frames(captured.out, LaunchPayload.ERROR_MARKER)[0]
        assert frame["kind"] == "invalid_request"

    def test_empty_reason_falls_back_to_kind(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Кадр требует непустой текст: у молчаливой ошибки берём её имя."""
        _, captured = _run({"op": "silent", "size": 1}, capsys)
        frame = _frames(captured.out, LaunchPayload.ERROR_MARKER)[0]
        assert frame["message"] == "SilentError"

    def test_reason_goes_to_stderr_without_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Оператору — одна запись в журнал, а не простыня стека."""
        _, captured = _run({"op": "declared", "size": 1}, capsys)
        frames = _frames(captured.err, LaunchPayload.LOG_MARKER)
        assert len(frames) == 1
        assert frames[0]["lvl"] == "ERROR"
        assert "Traceback" not in captured.err


class TestUnexpectedErrors:
    """Необъявленное не прячется: интерпретатор печатает трейсбек сам."""

    def test_undeclared_type_propagates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(KeyError):
            _run({"op": "неизвестная", "size": 1}, capsys)


class TestLogging:
    """Логи инструмента уезжают кадрами в stderr — на исход они не влияют."""

    def test_log_frames_go_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, captured = _run({"op": "logs", "size": 3}, capsys)
        frames = _frames(captured.err, LaunchPayload.LOG_MARKER)
        levels = [frame["lvl"] for frame in frames]
        assert code == 0
        assert "INFO" in levels
        assert "WARNING" in levels
        assert frames[0]["name"] == "boba.tool.probe"
        assert "size=3" in frames[0]["msg"]

    def test_logs_do_not_touch_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        """stdout — плоскость данных: лог там сломал бы разбор кадров."""
        _, captured = _run({"op": "logs", "size": 1}, capsys)
        assert LaunchPayload.LOG_MARKER not in captured.out
        assert _frames(captured.out, LaunchPayload.MARKER) == [{"size": 1}]

    def test_declared_error_is_logged_too(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Отказ виден и в журнале, но решает его кадр на stdout."""
        _, captured = _run({"op": "declared", "size": 1}, capsys)
        frames = _frames(captured.err, LaunchPayload.LOG_MARKER)
        assert [frame["lvl"] for frame in frames] == ["ERROR"]
        assert "document_unreadable" in frames[0]["msg"]
        assert _frames(captured.out, LaunchPayload.ERROR_MARKER)


class TestSuccess:
    """Штатный путь кадром ошибки не задет."""

    def test_trailer_and_chunks_survive(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, captured = _run({"op": "ok", "size": 7}, capsys)
        assert code == 0
        assert _frames(captured.out, LaunchPayload.CHUNK_MARKER) == ["данные"]
        assert _frames(captured.out, LaunchPayload.MARKER) == [{"size": 7}]
        assert not _frames(captured.out, LaunchPayload.ERROR_MARKER)
