"""Tool send_file: что видит LLM и как тул отказывает, когда отправлять нечего."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import BaseModel

from boba.chainlit.agent.tools.send_file import FileAttachment, build_send_file_tool
from boba.chainlit.domain import session as session_module
from boba.toolkit.result import ErrorResult

THREAD = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestToolInterface:
    def test_llm_sees_only_path(self) -> None:
        """tool_call_id подставляет langchain: в схеме для модели его быть не должно."""
        schema = cast(type[BaseModel], build_send_file_tool().tool_call_schema)
        assert set(schema.model_fields) == {"path"}

    def test_tool_name(self) -> None:
        assert build_send_file_tool().name == "send_file"

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
        assert isinstance(message.artifact, ErrorResult)
        assert message.artifact.error_kind == "no_session"


class TestRefusal:
    """Отказ доезжает до LLM ошибкой с причиной, а не исключением."""

    @staticmethod
    async def _attach(path: str) -> ErrorResult:
        result = await FileAttachment.attach(path, "call_1")
        assert isinstance(result, ErrorResult)
        return result

    @pytest.mark.anyio
    async def test_without_session(self) -> None:
        refusal = await self._attach("/workspace/x/upload/report.pdf")
        assert refusal.error_kind == "no_session"
        assert refusal.ok is False

    @pytest.mark.anyio
    async def test_without_active_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_module, "current_user_id", lambda: "7")
        monkeypatch.setattr(session_module, "current_thread_id", lambda: THREAD)

        refusal = await self._attach(f"/workspace/{THREAD}/upload/report.pdf")
        assert refusal.error_kind == "no_turn"
