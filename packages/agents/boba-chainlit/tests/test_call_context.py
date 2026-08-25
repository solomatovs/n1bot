"""Контекст вызова: постановка, чтение, отказ вне контекста, безопасный scope.id."""

from __future__ import annotations

import pytest
from conftest import use_context, use_session
from pydantic import ValidationError

from boba.chainlit.domain.context import (
    CallContext,
    ChatCallContext,
    ChatSurface,
    ContextKind,
    HumanInitiator,
    LlmInitiator,
    Scope,
    ScopeKind,
)
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.session import LoginTemplate, LogUserMark

THREAD = "55555555-5555-5555-5555-555555555555"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestCurrent:
    def test_outside_context_is_a_refusal(self) -> None:
        with pytest.raises(RefusalError) as caught:
            CallContext.current()

        if caught.value.kind != ContextKind.NO_CONTEXT:
            raise AssertionError(caught.value.kind)
        if CallContext.peek() is not None:
            raise AssertionError("peek outside a context must be None")

    def test_applied_sets_context_and_log_mark_for_the_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = use_context(monkeypatch, thread_id=THREAD, login="ivanov")
        CallContext.reset()

        with context.applied():
            if CallContext.current() is not context:
                raise AssertionError("context inside the block")
            if LogUserMark.current() != f"ivanov {THREAD[:8]}":
                raise AssertionError(LogUserMark.current())

        if CallContext.peek() is not None:
            raise AssertionError("context must be gone after the block")
        if LogUserMark.current() != "":
            raise AssertionError("log mark must be gone after the block")

    def test_subject_is_the_access_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_context(monkeypatch, thread_id=THREAD, roles=("ADM",), profile="general")

        subject = CallContext.current_subject()
        if subject.roles != frozenset({"ADM"}) or subject.profile != "general":
            raise AssertionError(subject)
        if subject.user_key != "7":
            raise AssertionError(subject.user_key)


class TestChatContext:
    def test_plain_context_is_not_a_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_context(monkeypatch, thread_id=THREAD)

        with pytest.raises(RefusalError) as caught:
            ChatCallContext.require()

        if caught.value.kind != ContextKind.CHAT_ONLY:
            raise AssertionError(caught.value.kind)

    def test_session_context_is_a_chat_with_a_surface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_session(monkeypatch, user_id="7", thread_id=THREAD)

        context = ChatCallContext.require()
        if not isinstance(context.surface, ChatSurface):
            raise AssertionError("chat context carries a surface")

    def test_tool_call_derives_llm_initiator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = use_context(monkeypatch, thread_id=THREAD)

        derived = context.as_tool_call("call-1")
        if not isinstance(derived.initiator, LlmInitiator):
            raise AssertionError(derived.initiator)
        if derived.initiator.tool_call_id != "call-1":
            raise AssertionError(derived.initiator)
        if derived.subject is not context.subject:
            raise AssertionError("subject is shared")

        human = derived.model_copy(update={"initiator": HumanInitiator(via="api")})
        if human.as_tool_call("call-2") is not human:
            raise AssertionError("only the chat initiator turns into llm")


class TestScope:
    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../x"])
    def test_unsafe_ids_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Scope(kind=ScopeKind.CHAT, id=bad)

    def test_chat_scope(self) -> None:
        scope = Scope.chat(THREAD)
        if scope.kind is not ScopeKind.CHAT or scope.id != THREAD:
            raise AssertionError(scope)


class TestPrincipalFormat:
    def test_single_username_field_is_required(self) -> None:
        if LoginTemplate.check_principal("{username}@X") != "{username}@X":
            raise AssertionError("valid format passes")

        with pytest.raises(ValueError, match="repeats"):
            LoginTemplate.check_principal("{username}@{username}")

        with pytest.raises(ValueError, match="username"):
            LoginTemplate.check_principal("user@X")
