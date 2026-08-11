"""Тесты единой отрисовки ленты: ChatView + восстановление истории."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from boba.chainlit.chat.transcript import ConversationTranscript
from boba.chainlit.rendering.chat_view import (
    ChatView,
    RecordingSink,
    StepRole,
    StepStatus,
    StepText,
)
from boba.toolkit.result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    TableResult,
    TextResult,
    ToolArtifact,
)

THREAD = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_view() -> tuple[ChatView, RecordingSink]:
    sink = RecordingSink()
    return ChatView(THREAD, sink, user_name="tester"), sink


class TestToolArtifact:
    def test_revives_model_from_dict(self) -> None:
        revived = ToolArtifact.revive({"kind": "text", "text": "hi"})
        assert isinstance(revived, TextResult)
        assert revived.text == "hi"

    def test_keeps_model_as_is(self) -> None:
        original = TextResult(text="hi")
        assert ToolArtifact.revive(original) is original

    def test_unknown_payload_is_none(self) -> None:
        assert ToolArtifact.revive({"kind": "nope"}) is None
        assert ToolArtifact.revive("plain string") is None

    def test_broken_own_artifact_raises(self) -> None:
        with pytest.raises(ValidationError):
            ToolArtifact.revive({"kind": "text"})


class TestToolFinished:
    def _finish(self, artifact: Any) -> tuple[Any, RecordingSink]:
        view, sink = make_view()

        async def scenario():
            step = await view.tool_started("demo", {"x": 1}, "k1")
            await view.tool_finished(step, artifact, "call_1")
            return step

        return run(scenario()), sink

    def test_markdown_text(self) -> None:
        step, _ = self._finish(TextResult(text="hi"))
        assert step.output == "hi"
        assert step.language is None
        assert step.is_error is False

    def test_error_result_marks_step(self) -> None:
        step, _ = self._finish(ErrorResult(message="boom", error_kind="e"))
        assert step.is_error is True
        assert "boom" in step.output

    def test_chart_adds_top_level_step(self) -> None:
        step, sink = self._finish(ChartResult(spec={"data": []}, title="T"))
        assert step.output == "chart rendered: T"
        chart = [s for s in sink.steps if s.get("type") == "assistant_message"]
        assert len(chart) == 1
        assert chart[0].get("output") == "T"
        assert chart[0].get("parentId") is None

    def test_chart_step_id_is_derived_from_tool_call(self) -> None:
        _, sink = self._finish(ChartResult(spec={"data": []}, title="T"))
        chart = next(s for s in sink.steps if s.get("type") == "assistant_message")
        assert chart.get("id") == ChatView.derive_id(THREAD, "call_1", StepRole.CHART)

    def test_artifact_dict_renders_like_model(self) -> None:
        step, _ = self._finish({"kind": "text", "text": "from checkpoint"})
        assert step.output == "from checkpoint"

    def test_failed_command_is_marked_red(self) -> None:
        """Ненулевой код возврата — неуспех, хотя инструмент отработал."""
        step, _ = self._finish(
            JsonResult(ok=False, payload={"exit_code": 127, "stderr": "not found"})
        )
        assert step.name == StepStatus.FAILED.title("demo")
        assert step.is_error is True

    def test_successful_command_is_marked_green(self) -> None:
        step, _ = self._finish(JsonResult(payload={"exit_code": 0, "stdout": "ok"}))
        assert step.name == StepStatus.DONE.title("demo")
        assert step.is_error is False

    def test_error_result_is_not_ok_by_default(self) -> None:
        assert ErrorResult(message="boom", error_kind="e").ok is False
        assert TextResult(text="hi").ok is True

    def test_stopped_tool_is_marked_red(self) -> None:
        view, sink = make_view()

        async def scenario():
            step = await view.tool_started("visualize", {"x": 1}, "k1")
            await view.tool_stopped(step, StepText.STOPPED)
            return step

        step = run(scenario())
        assert step.name == StepStatus.FAILED.title("visualize")
        assert step.output == StepText.STOPPED
        assert sink.steps

    def test_non_tool_result_falls_through(self) -> None:
        step, _ = self._finish({"whatever": 1})
        assert step.output


class TestTranscript:
    def _replay(self, messages: list) -> RecordingSink:
        view, sink = make_view()
        run(ConversationTranscript(messages, view).replay())
        return sink

    def test_full_turn_layout(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="нарисуй график", id="m1"),
                AIMessage(
                    content="",
                    id="m2",
                    tool_calls=[
                        {
                            "name": "visualize",
                            "args": {"q": "bar"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content="[chart rendered: Final]",
                    id="m3",
                    name="visualize",
                    tool_call_id="call_1",
                    artifact={"kind": "chart", "spec": {"data": []}, "title": "Final"},
                ),
                AIMessage(content="готово", id="m4"),
            ]
        )
        by_type: dict[str, list] = {}
        for step in sink.steps:
            by_type.setdefault(str(step.get("type")), []).append(step)

        assert [s.get("output") for s in by_type["user_message"]] == ["нарисуй график"]
        assert by_type["run"][0].get("name") == StepText.CONTAINER
        assert by_type["tool"][0].get("name") == StepStatus.DONE.title("visualize")
        assert by_type["tool"][0].get("parentId") == by_type["run"][0].get("id")
        answers = [s.get("output") for s in by_type["assistant_message"]]
        assert answers == ["Final", "готово"]
        assert all(s.get("parentId") is None for s in by_type["assistant_message"])

    def test_table_artifact_from_checkpoint(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="дай таблицу", id="m1"),
                ToolMessage(
                    content="ok",
                    id="m2",
                    name="table_tool",
                    tool_call_id="call_2",
                    artifact=TableResult(rows=[{"a": 1, "b": 2}]).model_dump(),
                ),
            ]
        )
        tool = next(s for s in sink.steps if s.get("type") == "tool")
        output = tool.get("output") or ""
        assert "a" in output
        assert "b" in output

    def test_failed_tool_is_marked_red(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="ломай", id="m1"),
                ToolMessage(
                    content="упало",
                    id="m2",
                    name="bad",
                    tool_call_id="call_9",
                    status="error",
                ),
            ]
        )
        tool = next(s for s in sink.steps if s.get("type") == "tool")
        assert tool.get("name") == StepStatus.FAILED.title("bad")

    def test_error_tool_message(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="ломай", id="m1"),
                ToolMessage(
                    content="упало",
                    id="m2",
                    name="bad",
                    tool_call_id="call_3",
                    status="error",
                ),
            ]
        )
        tool = next(s for s in sink.steps if s.get("type") == "tool")
        assert tool.get("isError") is True

    def test_reasoning_becomes_thinking_step(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="?", id="m1"),
                AIMessage(
                    content="ответ",
                    id="m2",
                    additional_kwargs={"reasoning_content": "размышляю"},
                ),
            ]
        )
        thinking = [s for s in sink.steps if s.get("type") == "llm"]
        assert len(thinking) == 1
        assert thinking[0].get("name") == StepStatus.IDLE.title("thinking")
        assert thinking[0].get("output") == "размышляю"

    def test_answer_id_matches_live_rendering(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="привет", id="chainlit-msg-1"),
                AIMessage(content="и тебе", id="m2"),
            ]
        )
        answer = next(s for s in sink.steps if s.get("type") == "assistant_message")
        expected = ChatView.derive_id(THREAD, "chainlit-msg-1", StepRole.ANSWER)
        assert answer.get("id") == expected

    def test_question_keeps_chainlit_message_id(self) -> None:
        sink = self._replay([HumanMessage(content="привет", id="chainlit-msg-1")])
        question = next(s for s in sink.steps if s.get("type") == "user_message")
        assert question.get("id") == "chainlit-msg-1"

    def test_failure_from_history_renders_as_error(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="?", id="m1"),
                AIMessage(
                    content="**сбой:** провайдер недоступен",
                    id="m2",
                    additional_kwargs={"error": True},
                ),
            ]
        )
        step = next(s for s in sink.steps if s.get("type") == "assistant_message")
        assert step.get("isError") is True
        assert "провайдер недоступен" in (step.get("output") or "")

    def test_replay_is_deterministic(self) -> None:
        messages = [
            HumanMessage(content="привет", id="m1"),
            AIMessage(content="и тебе", id="m2"),
        ]
        first = self._replay(messages).steps
        second = self._replay(messages).steps
        assert [s.get("id") for s in first] == [s.get("id") for s in second]

    def test_each_question_opens_its_own_container(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="раз", id="m1"),
                AIMessage(
                    content="",
                    id="m2",
                    additional_kwargs={"reasoning_content": "a"},
                ),
                HumanMessage(content="два", id="m3"),
                AIMessage(
                    content="",
                    id="m4",
                    additional_kwargs={"reasoning_content": "b"},
                ),
            ]
        )
        containers = [s for s in sink.steps if s.get("type") == "run"]
        assert len(containers) == 2
        assert containers[0].get("id") != containers[1].get("id")
