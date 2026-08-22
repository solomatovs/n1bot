"""Тесты графов flow: подготовка контекста хода и обычный цикл."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Annotated, Any

import pytest
from chainlit.step import StepDict
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import Field, ValidationError

from boba.cancellation import StopReason, ToolStopped, turn_cancellation
from boba.chainlit.agent.flow import (
    GraphSpec,
    LlmRephraser,
    PassthroughRephraser,
    PlainGraphBuilder,
    PrefetchCall,
    PrefetchError,
    PrefetchGraphBuilder,
    PrefetchStage,
    Rephraser,
    RephrasingsParser,
)
from boba.chainlit.agent.toolrun.cancellation import CancellableTools
from boba.chainlit.chat.tracing import AgentTracer, TracedStage
from boba.chainlit.chat.turn import TurnState
from boba.chainlit.domain.fields import StepField
from boba.chainlit.infra.config import (
    AppConfig,
    ChatProfileConfig,
    PlainFlowConfig,
    PrefetchFlowConfig,
    SelectedProfile,
)
from boba.chainlit.infra.providers import (
    _flow_tools,
    build_history_view,
    httpx_clients,
    session_graph_builder,
)
from boba.chainlit.rendering.chat_view import ChatView, RecordingSink, StepText
from boba.llm.generation import (
    GenerationError,
    OpenAiGeneration,
    SchemaSpec,
    StructuredGenerator,
)
from boba.toolkit.calls import ToolIntent
from boba.toolkit.result import ErrorResult, TableResult, ToolArtifact, pack_result

pytestmark = pytest.mark.anyio

OPENAI = {"base_url": "https://llm.example/v1", "api_key": "token"}

REPHRASER = {
    "provider": "openai",
    "openai": OPENAI,
    "model": "small-model",
    "system_prompt": "rephrase",
    "max_tokens": 256,
    "temperature": 0,
}

THREAD = RunnableConfig(configurable={"thread_id": "flow-thread"})


FEED_THREAD = "33333333-3333-3333-3333-333333333333"
FEED_TURN = "44444444-4444-4444-4444-444444444444"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
async def http_context() -> None:
    """Step пишет в emitter сессии."""
    from chainlit.context import init_http_context

    init_http_context()


class ScriptedChat(GenericFakeChatModel):
    """Основная модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class FakeRephraser(Rephraser):
    """Переформулировщик без сети: отдаёт заготовленные варианты."""

    def __init__(self, queries: Sequence[str]) -> None:
        self.queries = queries
        self.asked: list[str] = []

    async def rephrase(self, query: str) -> Sequence[str]:
        self.asked.append(query)
        return self.queries


class RecordingStage(PrefetchStage):
    """Этап без ленты: запоминает, что подготовка открылась и закрылась."""

    def __init__(self) -> None:
        self.opened = 0
        self.searched: list[Sequence[str]] = []
        self.closed: list[Sequence[str]] = []
        self.elapsed: list[int] = []

    async def begin(self) -> None:
        self.opened += 1

    async def searching(self, queries: Sequence[str]) -> None:
        self.searched.append(list(queries))

    async def end(self, queries: Sequence[str], elapsed_ms: int) -> None:
        self.closed.append(list(queries))
        self.elapsed.append(elapsed_ms)


class BrokenRephraser(Rephraser):
    """Переформулировщик, у которого недоступен провайдер."""

    async def rephrase(self, query: str) -> Sequence[str]:
        msg = "provider is down"
        raise RuntimeError(msg)


class FakeGenerator(StructuredGenerator):
    """Генератор без сети: отдаёт заготовленный ответ модели."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.asked: list[str] = []

    async def generate(self, user: str, schema: SchemaSpec) -> str:
        self.asked.append(user)
        return self.reply


class BrokenGenerator(StructuredGenerator):
    """Генератор, у которого недоступен провайдер."""

    async def generate(self, user: str, schema: SchemaSpec) -> str:
        msg = "provider is down"
        raise GenerationError(msg)


@tool(response_format="content_and_artifact")
async def fts_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Полнотекстовый поиск-заглушка."""
    return pack_result(TableResult(rows=[{"hit": f"fts:{query}"}]))


@tool(response_format="content_and_artifact")
async def vector_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Векторный поиск-заглушка."""
    return pack_result(TableResult(rows=[{"hit": f"vector:{query}"}]))


@tool(response_format="content_and_artifact")
async def failing_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Поиск, отвечающий отказом инструмента."""
    return pack_result(
        ErrorResult(message="database is down", error_kind="database_unavailable")
    )


@tool(response_format="content_and_artifact")
async def slow_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Поиск, не успевающий закончиться до остановки хода."""
    await asyncio.sleep(0.2)

    return pack_result(TableResult(rows=[{"hit": f"slow:{query}"}]))


@tool(response_format="content_and_artifact")
async def strict_probe(
    query: Annotated[str, Field(min_length=5, description="Search query.")],
) -> tuple[str, Any]:
    """Поиск, не принимающий короткий запрос."""
    return pack_result(TableResult(rows=[{"hit": f"strict:{query}"}]))


@tool(response_format="content_and_artifact")
async def crashing_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Поиск, падающий исключением."""
    msg = "sandbox crashed"
    raise RuntimeError(msg)


def _graph(builder: Any, answers: Sequence[str]) -> CompiledStateGraph:
    """Граф на фейковой модели: реальные create_agent, checkpointer и history."""
    scripted: list[AIMessage | str] = []
    for answer in answers:
        scripted.append(AIMessage(content=answer))

    chat = ScriptedChat(messages=iter(scripted))
    tools = [fts_probe, vector_probe]

    spec = GraphSpec(
        chat=chat,
        tools=tools,
        system_prompt="you are a search assistant",
        checkpointer=InMemorySaver(),
        history=build_history_view(frozenset({"fts_probe", "vector_probe"}), 30),
    )
    return builder.build(spec)


def _step_named(steps: Sequence[StepDict], name: str) -> StepDict | None:
    """Шаг ленты, чьё название содержит имя; None — такого шага нет."""
    for step in steps:
        if name in str(step.get(StepField.NAME, "")):
            return step

    return None


def _prefetch_calls(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue

        for call in message.tool_calls:
            call_id = call["id"]
            if call_id and call_id.startswith(PrefetchCall.PREFIX):
                calls.append(dict(call))

    return calls


def _tool_messages(messages: Sequence[BaseMessage]) -> list[ToolMessage]:
    found: list[ToolMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            found.append(message)

    return found


class TestRephrasingsParser:
    """Разбор ответа переформулировщика: схема, чужой json, голый текст."""

    SCHEMA_ANSWER = (
        '{"keywords": "kerberos cloudbeaver samba", '
        '"expanded": "настройка kerberos в cloudbeaver через samba AD", '
        '"english": "kerberos authentication in cloudbeaver with samba AD"}'
    )

    def test_schema_answer_gives_every_field(self) -> None:
        parsed = RephrasingsParser.parse(self.SCHEMA_ANSWER)
        if len(parsed) != 3:
            raise AssertionError(f"три варианта, получено {parsed}")

    def test_fenced_json_is_unwrapped(self) -> None:
        fenced = f"```json\n{self.SCHEMA_ANSWER}\n```"
        if RephrasingsParser.parse(fenced) != RephrasingsParser.parse(
            self.SCHEMA_ANSWER
        ):
            raise AssertionError("markdown-блок разобран как голый json")

    def test_foreign_json_gives_its_strings(self) -> None:
        parsed = RephrasingsParser.parse('{"queries": ["first one", "second one"]}')
        if list(parsed) != ["first one", "second one"]:
            raise AssertionError(f"строки чужой схемы взяты как есть: {parsed}")

    def test_plain_lines_are_taken_as_queries(self) -> None:
        parsed = RephrasingsParser.parse('1. first one\n2. "second one"\n- third one')
        if list(parsed) != ["first one", "second one", "third one"]:
            raise AssertionError(f"нумерация и кавычки убраны: {parsed}")

    def test_repeated_variants_are_dropped(self) -> None:
        answer = (
            '{"keywords": "same text", "expanded": "same text", "english": "same text"}'
        )
        if list(RephrasingsParser.parse(answer)) != ["same text"]:
            raise AssertionError("повторы не размножают запросы")

    def test_empty_answer_gives_nothing(self) -> None:
        if RephrasingsParser.parse("   "):
            raise AssertionError("пустой ответ разбирать нечем")

    def test_truncated_json_gives_nothing(self) -> None:
        """Ответ оборвался на лимите токенов: огрызок запросом быть не может."""
        cut = '{"keywords": "kerberos cloudbeaver", "expanded": "настройка kerb'
        if RephrasingsParser.parse(cut):
            raise AssertionError("недописанный json в поиск не идёт")


class TestLlmRephraser:
    """Переформулировщик поверх генератора: любой ответ либо разобран, либо откат."""

    async def test_schema_answer_becomes_queries(self) -> None:
        generator = FakeGenerator(TestRephrasingsParser.SCHEMA_ANSWER)

        queries = await LlmRephraser(generator).rephrase("исходный запрос")

        if len(queries) != 3:
            raise AssertionError(f"три варианта, получено {queries}")
        if generator.asked != ["исходный запрос"]:
            raise AssertionError(f"в модель ушёл запрос: {generator.asked}")

    async def test_text_answer_is_used_as_well(self) -> None:
        generator = FakeGenerator("first variant\nsecond variant")

        queries = await LlmRephraser(generator).rephrase("исходный запрос")

        if list(queries) != ["first variant", "second variant"]:
            raise AssertionError(f"текстовый ответ разобран: {queries}")

    async def test_broken_provider_searches_by_user_query(self) -> None:
        queries = await LlmRephraser(BrokenGenerator()).rephrase("исходный запрос")

        if list(queries) != ["исходный запрос"]:
            raise AssertionError(f"откат на исходный запрос: {queries}")

    async def test_unusable_answer_searches_by_user_query(self) -> None:
        queries = await LlmRephraser(FakeGenerator("")).rephrase("исходный запрос")

        if list(queries) != ["исходный запрос"]:
            raise AssertionError(f"откат на исходный запрос: {queries}")


class TestPrefetchGraph:
    """Prefetch-граф: ход получает контекст до обращения к основной модели."""

    async def test_turn_prefetches_search_results(self) -> None:
        rephraser = FakeRephraser(["variant one", "variant two"])
        graph = _graph(
            PrefetchGraphBuilder(
                rephraser, [fts_probe, vector_probe], RecordingStage()
            ),
            answers=["answered with context"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("how to configure kerberos?")]},
            config=THREAD,
        )
        messages = result["messages"]

        if rephraser.asked != ["how to configure kerberos?"]:
            raise AssertionError(f"rephraser got {rephraser.asked}")

        calls = _prefetch_calls(messages)
        if len(calls) != 4:
            raise AssertionError(f"2 queries x 2 tools == 4 calls, got {len(calls)}")

        names = set()
        for call in calls:
            names.add(call["name"])
        if names != {"fts_probe", "vector_probe"}:
            raise AssertionError(f"both tools are called, got {names}")

        replies = _tool_messages(messages)
        if len(replies) != 4:
            raise AssertionError(f"4 tool messages expected, got {len(replies)}")

        revived = ToolArtifact.revive(replies[0].artifact)
        if not isinstance(revived, TableResult):
            raise AssertionError(f"artifact is TableResult, got {type(revived)}")

        if not isinstance(messages[-1], AIMessage):
            raise AssertionError("last message is the model answer")
        if messages[-1].content != "answered with context":
            raise AssertionError(f"answer survived: {messages[-1].content!r}")

    async def test_stage_wraps_the_preparation(self) -> None:
        """Этап открывается до подготовки и закрывается запросами, что ушли в поиск."""
        rephraser = FakeRephraser(["variant one", "variant two"])
        stage = RecordingStage()
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [fts_probe], stage),
            answers=["answered"],
        )

        await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )

        if stage.opened != 1:
            raise AssertionError(f"этап открывается один раз, а не {stage.opened}")

        if stage.searched != [["variant one", "variant two"]]:
            raise AssertionError(f"фаза поиска подписана запросами {stage.searched}")

        if stage.closed != [["variant one", "variant two"]]:
            raise AssertionError(f"этап закрыт запросами {stage.closed}")

    async def test_search_phase_is_not_announced_when_rephrasing_fails(self) -> None:
        """Переформулировщик сорвался — фаза поиска не наступила."""
        stage = RecordingStage()
        graph = _graph(
            PrefetchGraphBuilder(BrokenRephraser(), [fts_probe], stage),
            answers=["never reached"],
        )

        with pytest.raises(PrefetchError):
            await graph.ainvoke(
                {"messages": [HumanMessage("question")]},
                config=THREAD,
            )

        if stage.searched:
            raise AssertionError(f"фазы поиска не было, получено {stage.searched}")

    async def test_prefetch_calls_carry_the_query_as_intent(self) -> None:
        """Подпись вызова подготовки — сам поисковый запрос: его покажет лента."""
        rephraser = FakeRephraser(["variant one"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [fts_probe], RecordingStage()),
            answers=["answered"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )

        calls = _prefetch_calls(result["messages"])
        if len(calls) != 1:
            raise AssertionError(f"один запрос в один инструмент, got {len(calls)}")

        intent = ToolIntent.of(calls[0]["args"])
        if intent != "variant one":
            raise AssertionError(f"подпись вызова: {intent!r}")

    async def test_stage_closes_when_preparation_fails(self) -> None:
        """Сбой подготовки не оставляет этап открытым висеть в ленте."""
        stage = RecordingStage()
        graph = _graph(
            PrefetchGraphBuilder(BrokenRephraser(), [fts_probe], stage),
            answers=["never reached"],
        )

        with pytest.raises(PrefetchError):
            await graph.ainvoke(
                {"messages": [HumanMessage("question")]},
                config=THREAD,
            )

        if stage.opened != 1:
            raise AssertionError("этап был открыт")
        if stage.closed != [[]]:
            raise AssertionError(f"этап закрыт без запросов, получено {stage.closed}")

    async def test_query_goes_as_is_without_rephraser(self) -> None:
        """Профиль без модели-переформулировщика ищет по запросу пользователя."""
        stage = RecordingStage()
        graph = _graph(
            PrefetchGraphBuilder(
                PassthroughRephraser(), [fts_probe, vector_probe], stage
            ),
            answers=["answered"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("как настроить kerberos?")]},
            config=THREAD,
        )

        calls = _prefetch_calls(result["messages"])
        if len(calls) != 2:
            raise AssertionError(f"один запрос в каждый инструмент, а не {len(calls)}")

        asked = set()
        for call in calls:
            asked.add(call["args"]["query"])
        if asked != {"как настроить kerberos?"}:
            raise AssertionError(f"в поиск ушёл не исходный запрос: {asked}")

        if stage.closed != [["как настроить kerberos?"]]:
            raise AssertionError(f"этап подписан запросами {stage.closed}")

    async def test_every_turn_is_prefetched(self) -> None:
        rephraser = FakeRephraser(["variant"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [fts_probe], RecordingStage()),
            answers=["first answer", "second answer"],
        )

        first = await graph.ainvoke(
            {"messages": [HumanMessage("first question")]},
            config=THREAD,
        )
        second = await graph.ainvoke(
            {"messages": [HumanMessage("follow-up question")]},
            config=THREAD,
        )

        if len(_prefetch_calls(first["messages"])) != 1:
            raise AssertionError("первый ход обязан готовить контекст")

        if len(_prefetch_calls(second["messages"])) != 2:
            raise AssertionError(
                "второй вопрос обязан добрать контекст: "
                f"{len(_prefetch_calls(second['messages']))} вызовов"
            )

        if rephraser.asked != ["first question", "follow-up question"]:
            raise AssertionError(f"запросы каждого хода: {rephraser.asked}")

    async def test_search_error_result_reaches_the_model(self) -> None:
        """Отказ инструмента едет в контекст: модель отвечает, ход не рвётся."""
        rephraser = FakeRephraser(["variant"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [failing_probe], RecordingStage()),
            answers=["answered anyway"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )
        messages = result["messages"]

        replies = _tool_messages(messages)
        if len(replies) != 1:
            raise AssertionError(f"отказ доехал конвертом: {replies}")

        revived = ToolArtifact.revive(replies[0].artifact)
        if not isinstance(revived, ErrorResult):
            raise AssertionError(f"в конверте отказ инструмента: {revived}")

        if messages[-1].content != "answered anyway":
            raise AssertionError(f"ход дошёл до ответа: {messages[-1].content!r}")

    async def test_search_crash_reaches_the_model(self) -> None:
        """Упавшее тело инструмента ход не роняет: причина уходит модели."""
        rephraser = FakeRephraser(["variant"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [crashing_probe], RecordingStage()),
            answers=["answered anyway"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )
        messages = result["messages"]

        replies = _tool_messages(messages)
        if len(replies) != 1:
            raise AssertionError(f"сбой доехал конвертом: {replies}")

        if replies[0].status != "error":
            raise AssertionError(f"конверт помечен ошибкой: {replies[0].status}")

        if "sandbox crashed" not in str(replies[0].content):
            raise AssertionError(f"причина в тексте: {replies[0].content!r}")

        if messages[-1].content != "answered anyway":
            raise AssertionError(f"ход дошёл до ответа: {messages[-1].content!r}")

    async def test_bad_arguments_do_not_break_the_turn(self) -> None:
        """Вызов с негодными аргументами: ошибка валидации уходит модели."""
        rephraser = FakeRephraser(["x"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [strict_probe], RecordingStage()),
            answers=["answered anyway"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )
        messages = result["messages"]

        replies = _tool_messages(messages)
        if len(replies) != 1:
            raise AssertionError(f"сорванный вызов доехал конвертом: {replies}")

        if replies[0].status != "error":
            raise AssertionError(f"конверт помечен ошибкой: {replies[0].status}")

        if messages[-1].content != "answered anyway":
            raise AssertionError(f"ход дошёл до ответа: {messages[-1].content!r}")

    async def test_rephraser_failure_fails_the_turn(self) -> None:
        graph = _graph(
            PrefetchGraphBuilder(BrokenRephraser(), [fts_probe], RecordingStage()),
            answers=["never reached"],
        )

        with pytest.raises(PrefetchError, match="provider is down"):
            await graph.ainvoke(
                {"messages": [HumanMessage("question")]},
                config=THREAD,
            )


class TestPrefetchCancellation:
    """Остановка хода во время подготовки: обрыв, а не отказ инструмента.

    Инструменты обёрнуты тем же CancellableTools, что и в приложении: после
    остановки их результат в контекст не идёт.
    """

    async def test_stop_breaks_the_turn_instead_of_feeding_the_model(self) -> None:
        stage = RecordingStage()
        guarded = CancellableTools.guard_all([slow_probe])
        graph = _graph(
            PrefetchGraphBuilder(FakeRephraser(["variant"]), guarded, stage),
            answers=["never reached"],
        )

        with turn_cancellation() as cancellation:

            async def stop_soon() -> None:
                await asyncio.sleep(0.05)
                cancellation.cancel(StopReason.USER_STOP)

            stopper = asyncio.create_task(stop_soon())

            with pytest.raises(ToolStopped):
                await graph.ainvoke(
                    {"messages": [HumanMessage("question")]},
                    config=THREAD,
                )

            await stopper

        if cancellation.reason is not StopReason.USER_STOP:
            raise AssertionError(f"причина остановки: {cancellation.reason}")

        if stage.closed != [["variant"]]:
            raise AssertionError(f"этап закрыт даже на обрыве: {stage.closed}")

    async def test_stop_before_the_call_refuses_to_start_it(self) -> None:
        """Остановка до вызова: инструмент не стартует, ход обрывается."""
        stage = RecordingStage()
        guarded = CancellableTools.guard_all([slow_probe])
        graph = _graph(
            PrefetchGraphBuilder(FakeRephraser(["variant"]), guarded, stage),
            answers=["never reached"],
        )

        with turn_cancellation() as cancellation:
            cancellation.cancel(StopReason.USER_STOP)

            with pytest.raises(ToolStopped):
                await graph.ainvoke(
                    {"messages": [HumanMessage("question")]},
                    config=THREAD,
                )

        if stage.closed != [["variant"]]:
            raise AssertionError(f"этап закрыт даже на обрыве: {stage.closed}")


class TestPrefetchFeed:
    """Подготовка в ленте: этап с фазами и шаги инструментов с подписями.

    Стенд повторяет прод: граф зовут с колбэком AgentTracer, шаги копит
    RecordingSink — так лента и получает вызовы подготовки.
    """

    async def test_prefetch_calls_are_drawn_inside_the_stage(
        self, http_context: None
    ) -> None:
        sink = RecordingSink()
        view = ChatView(FEED_THREAD, sink, user_name="Пользователь")
        view.begin_turn(FEED_TURN)

        graph = _graph(
            PrefetchGraphBuilder(
                FakeRephraser(["variant one"]),
                [fts_probe],
                TracedStage(StepText.PREFETCH.value),
            ),
            answers=["answered"],
        )

        config = RunnableConfig(
            configurable={"thread_id": "feed-thread"},
            callbacks=[AgentTracer(view, TurnState())],
        )
        await graph.ainvoke({"messages": [HumanMessage("question")]}, config=config)

        stage = _step_named(sink.steps, StepText.PREFETCH.value)
        if stage is None:
            raise AssertionError(f"этап подготовки нарисован: {sink.steps}")

        if stage.get(StepField.OUTPUT) != "- variant one":
            raise AssertionError(f"этап подписан запросами: {stage}")

        tool_step = _step_named(sink.steps, "fts_probe")
        if tool_step is None:
            raise AssertionError("вызов подготовки нарисован шагом")

        if tool_step.get(StepField.PARENT_ID) != stage.get(StepField.ID):
            raise AssertionError("шаг вызова лежит внутри этапа")

        drawn = str(tool_step.get(StepField.NAME, ""))
        if "variant one" not in drawn:
            raise AssertionError(f"шаг назван подписью: {drawn!r}")


class TestPlainGraph:
    """Plain-граф: обычный цикл без подготовки контекста."""

    async def test_no_prefetch_happens(self) -> None:
        graph = _graph(PlainGraphBuilder(), answers=["plain answer"])

        result = await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )
        messages = result["messages"]

        if _prefetch_calls(messages):
            raise AssertionError("plain graph must not prefetch")

        if messages[-1].content != "plain answer":
            raise AssertionError(f"answer survived: {messages[-1].content!r}")


class TestFlowConfig:
    """Секция flow профиля: дефолт, разбор prefetch и согласие с tools."""

    def _profile(self, **kw: Any) -> ChatProfileConfig:
        base: dict[str, Any] = {
            "display_name": "Profile",
            "description": "test profile",
            "provider": OPENAI,
            "model": "test-model",
        }
        base.update(kw)
        return ChatProfileConfig.model_validate(base)

    def test_flow_defaults_to_plain(self) -> None:
        profile = self._profile()
        if not isinstance(profile.flow, PlainFlowConfig):
            raise AssertionError(f"default flow is plain, got {profile.flow}")

    def test_prefetch_flow_is_parsed(self) -> None:
        profile = self._profile(
            tools=["kb_fts_search", "kb_vector_search", "web_fetch_page"],
            flow={
                "kind": "prefetch",
                "tools": ["kb_fts_search", "kb_vector_search"],
                "rephraser": REPHRASER,
            },
        )

        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            raise AssertionError(f"flow is prefetch, got {flow}")
        if not isinstance(flow.rephraser, OpenAiGeneration):
            raise AssertionError(f"openai rephraser expected, got {flow.rephraser}")
        if flow.rephraser.model != "small-model":
            raise AssertionError(f"model is parsed, got {flow.rephraser.model}")

    def test_flow_tool_outside_profile_is_config_error(self) -> None:
        with pytest.raises(ValidationError, match="flow tools"):
            self._profile(
                tools=["web_fetch_page"],
                flow={
                    "kind": "prefetch",
                    "tools": ["kb_fts_search"],
                    "rephraser": REPHRASER,
                },
            )

    def test_wildcard_profile_accepts_any_flow_tools(self) -> None:
        profile = self._profile(
            tools=["*"],
            flow={
                "kind": "prefetch",
                "tools": ["kb_fts_search"],
                "rephraser": REPHRASER,
            },
        )
        if not isinstance(profile.flow, PrefetchFlowConfig):
            raise AssertionError("prefetch flow is parsed with wildcard tools")

    def test_client_key_is_stable(self) -> None:
        if PrefetchFlowConfig.client_key("search") != "search:flow":
            raise AssertionError("client key is '<profile>:flow'")


class TestProviderAssembly:
    """Сборка на стороне провайдеров: билдер по профилю и клиент flow."""

    def _selected(self, **profile_kw: Any) -> SelectedProfile:
        base: dict[str, Any] = {
            "display_name": "Search",
            "description": "search profile",
            "provider": OPENAI,
            "model": "test-model",
            "tools": ["*"],
        }
        base.update(profile_kw)
        config = ChatProfileConfig.model_validate(base)
        return SelectedProfile(name="search", config=config)

    def _flow(self, **kw: Any) -> dict[str, Any]:
        flow: dict[str, Any] = {
            "kind": "prefetch",
            "tools": ["fts_probe"],
            "rephraser": REPHRASER,
        }
        flow.update(kw)
        return flow

    def test_plain_profile_gets_plain_builder(self) -> None:
        builder = session_graph_builder({}, self._selected(), [])
        if not isinstance(builder, PlainGraphBuilder):
            raise AssertionError(f"plain builder expected, got {type(builder)}")

    def test_prefetch_profile_gets_prefetch_builder(self) -> None:
        selected = self._selected(flow=self._flow())
        generators = {"search": FakeGenerator("{}")}

        builder = session_graph_builder(generators, selected, [fts_probe])
        if not isinstance(builder, PrefetchGraphBuilder):
            raise AssertionError(f"prefetch builder expected, got {type(builder)}")

    def test_flow_tool_missing_in_session_is_build_error(self) -> None:
        with pytest.raises(RuntimeError, match="not available"):
            _flow_tools(["kb_fts_search"], [fts_probe])

    async def test_httpx_clients_carry_flow_client(self, app_config: AppConfig) -> None:
        profile = self._selected(flow=self._flow()).config
        config = app_config.model_copy(update={"profiles": {"search": profile}})

        clients_gen = httpx_clients(config)
        clients = await anext(clients_gen)
        try:
            if set(clients) != {"search", "search:flow"}:
                raise AssertionError(f"flow client is created, got {set(clients)}")
        finally:
            await anext(clients_gen, None)
