"""Tool send_file: что видит LLM и как тул отказывает, когда отправлять нечего."""

from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest
from conftest import use_context, use_session
from pydantic import BaseModel

from boba.chainlit.agent.tools.send_file import FileAttachment, build_send_file_tool
from boba.toolkit.result import ErrorResult

THREAD = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestToolInterface:
    def test_llm_sees_only_path(self) -> None:
        """tool_call_id подставляет langchain: в схеме для модели его быть не должно."""
        schema = cast(type[BaseModel], build_send_file_tool().tool_call_schema)
        if set(schema.model_fields) != {"path"}:
            raise AssertionError('set(schema.model_fields) == {"path"}')

    def test_tool_name(self) -> None:
        if build_send_file_tool().name != "send_file":
            raise AssertionError('build_send_file_tool().name == "send_file"')

    @pytest.mark.anyio
    async def test_tool_call_id_is_injected(self) -> None:
        """Инъекция обязана работать: по tool_call_id строится id элемента."""
        call = {
            "args": {"path": f"/workspace/{THREAD}/upload/report.pdf"},
            "id": "call_1",
            "name": "send_file",
            "type": "tool_call",
        }
        message = await build_send_file_tool().ainvoke(call)

        # аргумент связался: до отказа по сессии дело дошло, а не до ошибки схемы
        if not (isinstance(message.artifact, ErrorResult)):
            raise AssertionError("isinstance(message.artifact, ErrorResult)")
        if message.artifact.error_kind != "no_context":
            raise AssertionError('message.artifact.error_kind == "no_context"')


class TestRefusal:
    """Отказ доезжает до LLM ошибкой с причиной, а не исключением."""

    @staticmethod
    async def _attach(path: str) -> ErrorResult:
        result = await FileAttachment.attach(path)
        if not (isinstance(result, ErrorResult)):
            raise AssertionError("isinstance(result, ErrorResult)")
        return result

    @pytest.mark.anyio
    async def test_without_session(self) -> None:
        refusal = await self._attach("/workspace/x/upload/report.pdf")
        if refusal.error_kind != "no_context":
            raise AssertionError('refusal.error_kind == "no_context"')
        if refusal.ok is not False:
            raise AssertionError("refusal.ok is False")

    @pytest.mark.anyio
    async def test_outside_chat_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Контекст есть, но не чата (workflow, REST): вложение слать некуда."""
        use_context(monkeypatch, thread_id=THREAD)

        refusal = await self._attach(f"/workspace/{THREAD}/upload/report.pdf")
        if refusal.error_kind != "chat_only":
            raise AssertionError(refusal.error_kind)

    @pytest.mark.anyio
    async def test_without_active_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_session(monkeypatch, user_id=str(UUID(int=7)), thread_id=THREAD)

        refusal = await self._attach(f"/workspace/{THREAD}/upload/report.pdf")
        if refusal.error_kind != "no_turn":
            raise AssertionError('refusal.error_kind == "no_turn"')
