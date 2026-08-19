"""Тесты единой отрисовки ленты: ChatView + восстановление истории."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from boba.chainlit.agent.flow import PrefetchCall
from boba.chainlit.domain.fields import StepField
from boba.chainlit.chat.history import ConversationTranscript
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
        if not (isinstance(revived, TextResult)):
            raise AssertionError("isinstance(revived, TextResult)")
        if revived.text != "hi":
            raise AssertionError('revived.text == "hi"')

    def test_keeps_model_as_is(self) -> None:
        original = TextResult(text="hi")
        if ToolArtifact.revive(original) is not original:
            raise AssertionError("ToolArtifact.revive(original) is original")

    def test_unknown_payload_is_none(self) -> None:
        if ToolArtifact.revive({"kind": "nope"}) is not None:
            raise AssertionError('ToolArtifact.revive({"kind": "nope"}) is None')
        if ToolArtifact.revive("plain string") is not None:
            raise AssertionError('ToolArtifact.revive("plain string") is None')

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
        if step.output != "hi":
            raise AssertionError('step.output == "hi"')
        if step.language is not None:
            raise AssertionError("step.language is None")
        if step.is_error is not False:
            raise AssertionError("step.is_error is False")

    def test_error_result_marks_step(self) -> None:
        step, _ = self._finish(ErrorResult(message="boom", error_kind="e"))
        if step.is_error is not True:
            raise AssertionError("step.is_error is True")
        if "boom" not in step.output:
            raise AssertionError('"boom" in step.output')

    def test_chart_adds_top_level_step(self) -> None:
        step, sink = self._finish(ChartResult(spec={"data": []}, title="T"))
        if step.output != "chart rendered: T":
            raise AssertionError('step.output == "chart rendered: T"')
        chart = [s for s in sink.steps if s.get("type") == "assistant_message"]
        if len(chart) != 1:
            raise AssertionError("len(chart) == 1")
        if chart[0].get("output") != "T":
            raise AssertionError('chart[0].get("output") == "T"')
        if chart[0].get("parentId") is not None:
            raise AssertionError('chart[0].get("parentId") is None')

    def test_chart_step_id_is_derived_from_tool_call(self) -> None:
        _, sink = self._finish(ChartResult(spec={"data": []}, title="T"))
        chart = next(s for s in sink.steps if s.get("type") == "assistant_message")
        if chart.get("id") != ChatView.derive_id(THREAD, "call_1", StepRole.CHART):
            raise AssertionError('chart.get("id") == ChatView.derive_id(THREAD, "call…')

    def test_artifact_dict_renders_like_model(self) -> None:
        step, _ = self._finish({"kind": "text", "text": "from checkpoint"})
        if step.output != "from checkpoint":
            raise AssertionError('step.output == "from checkpoint"')

    def test_failed_command_is_marked_red(self) -> None:
        """Ненулевой код возврата — неуспех, хотя инструмент отработал."""
        step, _ = self._finish(
            JsonResult(ok=False, payload={"exit_code": 127, "stderr": "not found"})
        )
        if step.name != StepStatus.FAILED.title("demo"):
            raise AssertionError('step.name == StepStatus.FAILED.title("demo")')
        if step.is_error is not True:
            raise AssertionError("step.is_error is True")

    def test_successful_command_is_marked_green(self) -> None:
        step, _ = self._finish(JsonResult(payload={"exit_code": 0, "stdout": "ok"}))
        if step.name != StepStatus.DONE.title("demo"):
            raise AssertionError('step.name == StepStatus.DONE.title("demo")')
        if step.is_error is not False:
            raise AssertionError("step.is_error is False")

    def test_error_result_is_not_ok_by_default(self) -> None:
        if ErrorResult(message="boom", error_kind="e").ok is not False:
            raise AssertionError('ErrorResult(message="boom", error_kind="e").ok is F…')
        if TextResult(text="hi").ok is not True:
            raise AssertionError('TextResult(text="hi").ok is True')

    def test_stopped_tool_is_marked_red(self) -> None:
        view, sink = make_view()

        async def scenario():
            step = await view.tool_started("visualize", {"x": 1}, "k1")
            await view.tool_stopped(step, StepText.STOPPED)
            return step

        step = run(scenario())
        if step.name != StepStatus.FAILED.title("visualize"):
            raise AssertionError('step.name == StepStatus.FAILED.title("visualize")')
        if step.output != StepText.STOPPED:
            raise AssertionError("step.output == StepText.STOPPED")
        if not (sink.steps):
            raise AssertionError("sink.steps")

    def test_non_tool_result_falls_through(self) -> None:
        step, _ = self._finish({"whatever": 1})
        if not (step.output):
            raise AssertionError("step.output")


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

        if [s.get("output") for s in by_type["user_message"]] != ["нарисуй график"]:
            raise AssertionError('[s.get("output") for s in by_type["user_message"]] …')
        if by_type["run"][0].get("name") != StepText.CONTAINER:
            raise AssertionError('by_type["run"][0].get("name") == StepText.CONTAINER')
        if by_type["tool"][0].get("name") != StepStatus.DONE.title("visualize"):
            raise AssertionError('by_type["tool"][0].get("name") == StepStatus.DONE.t…')
        if by_type["tool"][0].get("parentId") != by_type["run"][0].get("id"):
            raise AssertionError('by_type["tool"][0].get("parentId") == by_type["run"…')
        answers = [s.get("output") for s in by_type["assistant_message"]]
        if answers != ["Final", "готово"]:
            raise AssertionError('answers == ["Final", "готово"]')
        if not (all(s.get("parentId") is None for s in by_type["assistant_message"])):
            raise AssertionError('all(s.get("parentId") is None for s in by_type["ass…')

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
        if "a" not in output:
            raise AssertionError('"a" in output')
        if "b" not in output:
            raise AssertionError('"b" in output')

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
        if tool.get("name") != StepStatus.FAILED.title("bad"):
            raise AssertionError('tool.get("name") == StepStatus.FAILED.title("bad")')

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
        if tool.get("isError") is not True:
            raise AssertionError('tool.get("isError") is True')

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
        if len(thinking) != 1:
            raise AssertionError("len(thinking) == 1")
        if thinking[0].get("name") != StepStatus.IDLE.title("thinking"):
            raise AssertionError('thinking[0].get("name") == StepStatus.IDLE.title("t…')
        if thinking[0].get("output") != "размышляю":
            raise AssertionError('thinking[0].get("output") == "размышляю"')

    def test_answer_id_matches_live_rendering(self) -> None:
        sink = self._replay(
            [
                HumanMessage(content="привет", id="chainlit-msg-1"),
                AIMessage(content="и тебе", id="m2"),
            ]
        )
        answer = next(s for s in sink.steps if s.get("type") == "assistant_message")
        expected = ChatView.derive_id(THREAD, "chainlit-msg-1", StepRole.ANSWER)
        if answer.get("id") != expected:
            raise AssertionError('answer.get("id") == expected')

    def test_question_keeps_chainlit_message_id(self) -> None:
        sink = self._replay([HumanMessage(content="привет", id="chainlit-msg-1")])
        question = next(s for s in sink.steps if s.get("type") == "user_message")
        if question.get("id") != "chainlit-msg-1":
            raise AssertionError('question.get("id") == "chainlit-msg-1"')

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
        if step.get("isError") is not True:
            raise AssertionError('step.get("isError") is True')
        output = step.get("output") or ""
        if "провайдер недоступен" not in output:
            raise AssertionError(f"в шаге нет текста ошибки: {output!r}")

    def test_replay_is_deterministic(self) -> None:
        messages = [
            HumanMessage(content="привет", id="m1"),
            AIMessage(content="и тебе", id="m2"),
        ]
        first = self._replay(messages).steps
        second = self._replay(messages).steps
        if [s.get("id") for s in first] != [s.get("id") for s in second]:
            raise AssertionError('[s.get("id") for s in first] == [s.get("id") for s …')

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
        if len(containers) != 2:
            raise AssertionError("len(containers) == 2")
        if containers[0].get("id") == containers[1].get("id"):
            raise AssertionError('containers[0].get("id") != containers[1].get("id")')


class TestPrefetchStageReplay:
    """Сборка ленты из истории обязана дать ту же раскладку, что live."""

    def _replay(self, messages: list) -> RecordingSink:
        view, sink = make_view()
        run(ConversationTranscript(messages, view).replay())
        return sink

    @staticmethod
    def _history() -> list:
        calls = []
        replies = []
        for index, query in enumerate(("kerberos postgres", "gss keytab")):
            call_id = f"{PrefetchCall.PREFIX}{index}"
            calls.append(
                {
                    "name": "kb_fts_search",
                    "args": {"query": query},
                    "id": call_id,
                    "type": "tool_call",
                }
            )
            replies.append(
                ToolMessage(
                    content="hits",
                    id=f"t{index}",
                    tool_call_id=call_id,
                    name="kb_fts_search",
                    artifact=TableResult(rows=[{"page": query}]),
                )
            )

        return [
            HumanMessage(content="как настроить kerberos?", id="m1"),
            AIMessage(content="", id="m2", tool_calls=calls),
            *replies,
            AIMessage(content="вот ответ", id="m3"),
        ]

    def _by_id(self, sink: RecordingSink) -> dict[str, Any]:
        steps: dict[str, Any] = {}
        for step in sink.steps:
            steps[step.get(StepField.ID, "")] = step

        return steps

    def test_prefetch_calls_nest_into_the_stage(self) -> None:
        sink = self._replay(self._history())
        steps = self._by_id(sink)

        stage_id = ChatView.derive_id(THREAD, "m1", StepRole.STAGE)
        if stage_id not in steps:
            raise AssertionError("этап подготовки восстановлен из истории")

        nested = []
        for step in sink.steps:
            if step.get(StepField.PARENT_ID) == stage_id:
                nested.append(step.get(StepField.NAME, ""))

        if len(nested) != 2:
            raise AssertionError(f"в этапе оба вызова подготовки, а не {nested}")

    def test_stage_output_lists_the_queries(self) -> None:
        sink = self._replay(self._history())
        steps = self._by_id(sink)

        stage_id = ChatView.derive_id(THREAD, "m1", StepRole.STAGE)
        if stage_id is None:
            raise AssertionError("id этапа выводится из ключа хода")

        stage = steps[stage_id]

        expected = ChatView.stage_output(["kerberos postgres", "gss keytab"])
        if stage.get(StepField.OUTPUT) != expected:
            raise AssertionError(f"подпись этапа {stage.get(StepField.OUTPUT)!r}")

    def test_model_calls_stay_out_of_the_stage(self) -> None:
        """Вызовы, сделанные самой моделью, в этап не попадают."""
        messages = [
            HumanMessage(content="вопрос", id="m1"),
            AIMessage(
                content="",
                id="m2",
                tool_calls=[
                    {
                        "name": "kb_fts_search",
                        "args": {"query": "своими руками"},
                        "id": "call_own",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="hits",
                id="t0",
                tool_call_id="call_own",
                name="kb_fts_search",
                artifact=TableResult(rows=[{"page": "x"}]),
            ),
        ]

        sink = self._replay(messages)

        stage_id = ChatView.derive_id(THREAD, "m1", StepRole.STAGE)
        for step in sink.steps:
            if step.get(StepField.ID) == stage_id:
                raise AssertionError("без вызовов подготовки этап не рисуется")
