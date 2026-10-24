"""Текст сообщения запроса: строкой или частями, но не тем и другим сразу."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.llm.chat import ChatRole, ChatTurn


class TestTextParts:
    def test_parts_are_joined_for_backends_without_parts(self) -> None:
        turn = ChatTurn(role=ChatRole.USER, parts=["first", "second"])

        assert turn.text_parts() == ["first", "second"]
        assert turn.text() == "first\nsecond"

    def test_content_is_one_part(self) -> None:
        turn = ChatTurn(role=ChatRole.USER, content="hello")

        assert turn.text_parts() == ("hello",)
        assert turn.text() == "hello"

    def test_content_and_parts_together_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="both content and parts"):
            ChatTurn(role=ChatRole.USER, content="x", parts=["y"])
