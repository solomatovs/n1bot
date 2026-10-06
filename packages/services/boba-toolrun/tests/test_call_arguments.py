"""Что доходит до тела инструмента после проверки аргументов вызова.

Инструмент собран настоящей цепочкой обвязок (ToolChain.seal) и зовётся
портом ToolServer, как его зовёт сервис: проверяются ключи и значения,
которые получает тело (дефолты, приведение типов, служебные поля), текст
отказа проверки аргументов и доставка идентификатора вызова обвязке журнала.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

import pytest
from pydantic import BaseModel, Field

from boba.identity.context import CallContexts, LlmInitiator
from boba.identity.run import Runs
from boba.stand.toolstand import ToolStand
from boba.stand_core.context import CallStand
from boba.toolkit.calls import ToolCallBase
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import DagNode, ToolServer
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.result import ExceptionResult, MarkdownResult, ToolResultBase
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.stream_calls import StreamGroupsConfig
from boba.toolrun.streams import CallJournals

pytestmark = pytest.mark.anyio

ROLE = "dev"
PROFILE = "probe"
THREAD = "call-arguments"


class Window(BaseModel):
    """Вложенная модель аргумента: приходит от модели словарём."""

    offset: int = 0
    limit: int = 10


class LookupCall(ToolCallBase):
    """Класс вызова: тело получает аргументы его экземпляром."""

    query: Annotated[str, Field(min_length=1, description="Что искать")]
    limit: Annotated[int, Field(ge=1, description="Сколько строк")] = 3


@dataclass(frozen=True)
class Reply:
    """Итог вызова порта: текст модели, результат и признак ошибки вызова."""

    content: str
    artifact: ToolResultBase
    errored: bool


class Seen:
    """Что получили тела пробных инструментов: аргументы и id вызова из
    контекста, поставленного обвязкой журнала."""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []
        self.call_ids: list[str] = []


class ProbeTools:
    """Пробные инструменты с телами в процессе теста: тело запоминает, что
    получило. Создаётся стендом BodyStand на каждый тест."""

    def __init__(self, seen: Seen, contexts: CallContexts) -> None:
        self._seen = seen
        self._contexts = contexts

    def all(self) -> list[PayloadTool]:
        return [self._async_probe(), self._sync_probe(), self._class_probe()]

    def _taken(self, kwargs: dict[str, Any]) -> MarkdownResult:
        self._seen.kwargs.append(kwargs)
        initiator = self._contexts.current().initiator
        if isinstance(initiator, LlmInitiator):
            self._seen.call_ids.append(initiator.tool_call_id)

        return MarkdownResult(text="taken")

    def _async_probe(self) -> PayloadTool:
        taken = self._taken

        @tool
        async def probe_async(  # noqa: PLR0913 — проба перебирает виды аргументов
            query: Annotated[str, Field(min_length=1, description="Что искать")],
            limit: Annotated[int, Field(ge=1, description="Сколько строк")] = 5,
            ratio: float = 1.5,
            tags: Annotated[list[str], Field(description="Метки")] = [],  # noqa: B006
            mode: Literal["fast", "full"] = "fast",
            window: Window | None = None,
        ) -> MarkdownResult:
            """Проба с async-телом."""
            return taken(
                {
                    "query": query,
                    "limit": limit,
                    "ratio": ratio,
                    "tags": tags,
                    "mode": mode,
                    "window": window,
                }
            )

        return probe_async

    def _sync_probe(self) -> PayloadTool:
        taken = self._taken

        @tool
        def probe_sync(
            query: Annotated[str, Field(min_length=1, description="Что искать")],
            limit: Annotated[int, Field(ge=1, description="Сколько строк")] = 5,
        ) -> MarkdownResult:
            """Проба с sync-телом."""
            return taken({"query": query, "limit": limit})

        return probe_sync

    def _class_probe(self) -> PayloadTool:
        taken = self._taken

        @tool
        async def probe_class(call: LookupCall) -> MarkdownResult:
            """Проба с классом вызова."""
            return taken({"call": call})

        return probe_class


class BodyStand:
    """Пробные инструменты под цепочкой обвязок за портом ToolServer.

    Обёртки запуска нет: тело исполняется в процессе теста, как у
    собственных инструментов приложения, и видит ровно то, что отдали
    проверка аргументов и обвязки.
    """

    STREAM: ClassVar[StreamGroupsConfig] = StreamGroupsConfig(
        open_sec=20.0,
        stall_sec=10.0,
        poll_sec=0.1,
        pipe_bytes=65536,
        pipe_bytes_max=1 << 30,
    )

    def __init__(self, call_stand: CallStand) -> None:
        self.seen = Seen()
        contexts = call_stand.contexts
        stand = ToolStand(
            self.STREAM, contexts, CallJournals(None, Runs(contexts)), CallAmbient(), ()
        )
        stand.host(ProbeTools(self.seen, contexts).all())
        self._server: ToolServer = stand.server(
            stand.access(ROLE, PROFILE), (), DirectCalls()
        )

    async def called(self, name: str, args: Mapping[str, Any], call_id: str) -> Reply:
        call = DagNode(key=call_id, tool=name, args=dict(args))
        outcome = await self._server.call(call)

        return Reply(
            content=outcome.content,
            artifact=outcome.artifact,
            errored=outcome.errored,
        )


@pytest.fixture
def stand(call_stand: CallStand) -> BodyStand:
    call_stand.use(call_stand.context(THREAD, roles=[ROLE], profile=PROFILE))

    return BodyStand(call_stand)


def _only(seen: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if len(seen) != 1:
        raise AssertionError(f"the body is called exactly once: {seen}")

    return seen[0]


class TestBodyArguments:
    async def test_body_gets_every_field_with_defaults_applied(
        self, stand: BodyStand
    ) -> None:
        reply = await stand.called("probe_async", {"query": "q"}, "call-defaults")

        if reply.errored or reply.content != "taken":
            raise AssertionError(f"the call succeeds: {reply}")

        expected = {
            "query": "q",
            "limit": 5,
            "ratio": 1.5,
            "tags": [],
            "mode": "fast",
            "window": None,
        }
        if _only(stand.seen.kwargs) != expected:
            raise AssertionError(f"defaults reach the body: {stand.seen.kwargs}")

    async def test_values_are_coerced_to_the_declared_types(
        self, stand: BodyStand
    ) -> None:
        args = {
            "query": "q",
            "limit": "7",
            "ratio": 2,
            "tags": ["a", "b"],
            "mode": "full",
            "window": {"offset": "4"},
        }
        reply = await stand.called("probe_async", args, "call-coerced")

        if reply.errored:
            raise AssertionError(f"the call succeeds: {reply}")

        got = _only(stand.seen.kwargs)
        if got["limit"] != 7 or type(got["limit"]) is not int:
            raise AssertionError(f"a numeric string becomes int: {got}")
        if got["ratio"] != 2.0 or type(got["ratio"]) is not float:
            raise AssertionError(f"an int becomes float: {got}")
        if got["tags"] != ["a", "b"]:
            raise AssertionError(f"the list arrives as is: {got}")
        if got["window"] != Window(offset=4, limit=10):
            raise AssertionError(f"a dict becomes the nested model: {got}")
        if not isinstance(got["window"], Window):
            raise AssertionError(f"the nested value is a model instance: {got}")

    async def test_service_fields_and_strays_do_not_reach_the_body(
        self, stand: BodyStand
    ) -> None:
        args = {
            "query": "q",
            "intent": "why the call is made",
            "boba_tool_call_id": "forged-by-the-model",
            "stray": 1,
        }
        reply = await stand.called("probe_async", args, "call-service-fields")

        if reply.errored:
            raise AssertionError(f"unknown arguments do not fail the call: {reply}")

        got = _only(stand.seen.kwargs)
        if sorted(got) != ["limit", "mode", "query", "ratio", "tags", "window"]:
            raise AssertionError(f"only the body's own parameters arrive: {got}")

    async def test_sync_body_gets_the_same_arguments(self, stand: BodyStand) -> None:
        reply = await stand.called(
            "probe_sync", {"query": "q", "limit": "2"}, "call-sync"
        )

        if reply.errored or reply.content != "taken":
            raise AssertionError(f"the sync body is called: {reply}")
        if _only(stand.seen.kwargs) != {"query": "q", "limit": 2}:
            raise AssertionError(f"the sync body got: {stand.seen.kwargs}")

    async def test_call_class_arrives_as_its_instance(self, stand: BodyStand) -> None:
        reply = await stand.called(
            "probe_class", {"query": "q", "limit": "9"}, "call-class"
        )

        if reply.errored:
            raise AssertionError(f"the call succeeds: {reply}")

        got = _only(stand.seen.kwargs)
        call = got["call"]
        if not isinstance(call, LookupCall):
            raise AssertionError(f"the body gets its call class: {got}")
        if call.query != "q" or call.limit != 9:
            raise AssertionError(f"the call class carries validated values: {call}")
        if call.model_fields_set != {"query", "limit"}:
            raise AssertionError(f"sent fields are known: {call.model_fields_set}")


class TestCallId:
    async def test_call_id_reaches_the_journal_wrap(self, stand: BodyStand) -> None:
        """Обвязка журнала ставит контекст вызова моделью с id вызова порта."""
        await stand.called("probe_async", {"query": "q"}, "call-one")
        await stand.called("probe_sync", {"query": "q"}, "call-two")

        if stand.seen.call_ids != ["call-one", "call-two"]:
            raise AssertionError(f"each body runs under its call id: {stand.seen}")

    async def test_call_id_of_the_model_does_not_replace_the_real_one(
        self, stand: BodyStand
    ) -> None:
        args = {"query": "q", "boba_tool_call_id": "forged-by-the-model"}
        await stand.called("probe_async", args, "call-real")

        if stand.seen.call_ids != ["call-real"]:
            raise AssertionError(f"the id of the call wins: {stand.seen.call_ids}")


class TestRefusal:
    MISSING: ClassVar[str] = "ValidationError: query: Field required [missing]"

    WRONG_TYPE: ClassVar[str] = (
        "ValidationError: query: String should have at least 1 character "
        "[string_too_short]; limit: Input should be a valid integer, unable to "
        "parse string as an integer [int_parsing]"
    )

    RAISED_AT: ClassVar[str] = "raised at: boba/toolrun/"
    """Вторая строка отказа: место в коде исполнителя, где проверка отказала."""

    def _check_text(self, content: str, expected: str) -> None:
        lines = content.split("\n")
        if lines[0] != expected:
            raise AssertionError(f"the refusal names the reasons: {content!r}")
        if len(lines) != 2 or not lines[1].startswith(self.RAISED_AT):
            raise AssertionError(f"the refusal names its place: {content!r}")

    async def test_missing_argument_is_refused_before_the_body(
        self, stand: BodyStand
    ) -> None:
        reply = await stand.called("probe_async", {}, "call-missing")

        if not reply.errored:
            raise AssertionError(f"a missing argument is an error of the call: {reply}")
        if stand.seen.kwargs:
            raise AssertionError(f"the body is not called: {stand.seen.kwargs}")
        self._check_text(reply.content, self.MISSING)

        artifact = reply.artifact
        if not isinstance(artifact, ExceptionResult):
            raise AssertionError(f"the refusal is an exception result: {artifact!r}")
        if artifact.error_kind != "ValidationError":
            raise AssertionError(f"the kind is the error class: {artifact.error_kind}")

    async def test_wrong_values_are_refused_with_every_reason(
        self, stand: BodyStand
    ) -> None:
        args = {"query": "", "limit": "many"}
        reply = await stand.called("probe_async", args, "call-wrong")

        if not reply.errored:
            raise AssertionError(f"wrong values are an error of the call: {reply}")
        if stand.seen.kwargs:
            raise AssertionError(f"the body is not called: {stand.seen.kwargs}")
        self._check_text(reply.content, self.WRONG_TYPE)
