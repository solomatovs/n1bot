"""Тесты починки коллизий index у параллельных tool_calls в стриме."""

from __future__ import annotations

from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)

from boba.llm.events import LLMToolCallArgumentDelta, LLMToolCallBegin
from boba.llm.models import (
    LLMContext,
    LLMMessage,
    LLMRequest,
    RequestId,
)
from boba.provider.openai.response import FromOpenAIChunkConverter
from boba.provider.openai.tool_call_reindexer import (
    DuplicateToolCallIndexReindexer,
)


def _ctx() -> LLMContext:
    sys_msg = LLMMessage(role="system", content="s")
    req = LLMRequest(
        model="m",
        system_message=sys_msg,
        messages=(LLMMessage(role="user", content="hi"),),
    )
    return LLMContext(request_id=RequestId.new(), request=req)


def _tc(
    *,
    index: int,
    tc_id: str | None,
    name: str | None = None,
    args: str = "",
) -> ChoiceDeltaToolCall:
    fn = (
        ChoiceDeltaToolCallFunction(name=name, arguments=args)
        if name is not None or args
        else None
    )
    return ChoiceDeltaToolCall(index=index, id=tc_id, type="function", function=fn)


def _choice(*tool_calls: ChoiceDeltaToolCall) -> Choice:
    return Choice(
        index=0,
        finish_reason=None,
        delta=ChoiceDelta(
            role=None,
            content="",
            tool_calls=list(tool_calls) or None,
        ),
    )


def _chunk(*choices: Choice) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="c",
        created=1,
        model="m",
        object="chat.completion.chunk",
        choices=list(choices),
    )


def test_one_tool_call_index_unchanged() -> None:
    """Без коллизии index не меняется."""
    rx = DuplicateToolCallIndexReindexer()
    tc = _tc(index=0, tc_id="A", name="t")
    list(rx.stream(_ctx(), [_choice(tc)]))
    assert tc.index == 0


def test_well_formed_parallel_kept() -> None:
    """Корректные параллельные tool_calls (разные index) — не трогаются."""
    rx = DuplicateToolCallIndexReindexer()
    tc_a = _tc(index=0, tc_id="A", name="t")
    tc_b = _tc(index=1, tc_id="B", name="t")
    list(rx.stream(_ctx(), [_choice(tc_a), _choice(tc_b)]))
    assert (tc_a.index, tc_b.index) == (0, 1)


def test_collision_remapped_to_next_free() -> None:
    """Два tool_call с одинаковым index, разные id → второй на 1."""
    rx = DuplicateToolCallIndexReindexer()
    tc_a = _tc(index=0, tc_id="A", name="t")
    tc_b = _tc(index=0, tc_id="B", name="t")
    list(rx.stream(_ctx(), [_choice(tc_a), _choice(tc_b)]))
    assert (tc_a.index, tc_b.index) == (0, 1)


def test_three_collisions_get_unique_indexes() -> None:
    """Три коллизии index=0 → 0,1,2."""
    rx = DuplicateToolCallIndexReindexer()
    tcs = [_tc(index=0, tc_id=f"id-{i}", name="t") for i in range(3)]
    list(rx.stream(_ctx(), [_choice(tc) for tc in tcs]))
    assert [tc.index for tc in tcs] == [0, 1, 2]


def test_continuation_without_id_keeps_index() -> None:
    """Дельты-продолжения (без id) одного tool_call: index не меняется."""
    rx = DuplicateToolCallIndexReindexer()
    head = _tc(index=0, tc_id="A", name="t", args='{"x":')
    cont = _tc(index=0, tc_id=None, args="1}")
    list(rx.stream(_ctx(), [_choice(head), _choice(cont)]))
    assert (head.index, cont.index) == (0, 0)


def test_repeated_id_keeps_remapped_index() -> None:
    """Повторное появление того же id под коллидирующим index идёт на тот же слот."""
    rx = DuplicateToolCallIndexReindexer()
    a1 = _tc(index=0, tc_id="A", name="t", args="{")
    b1 = _tc(index=0, tc_id="B", name="t", args="{")
    b2 = _tc(index=0, tc_id="B", args="}")
    list(rx.stream(_ctx(), [_choice(a1), _choice(b1), _choice(b2)]))
    assert (a1.index, b1.index, b2.index) == (0, 1, 1)


def test_reset_clears_state() -> None:
    """После reset() реиндексер снова начинает со свежим состоянием."""
    rx = DuplicateToolCallIndexReindexer()
    list(
        rx.stream(
            _ctx(),
            [
                _choice(_tc(index=0, tc_id="A", name="t")),
                _choice(_tc(index=0, tc_id="B", name="t")),
            ],
        )
    )
    rx.reset()
    tc_c = _tc(index=0, tc_id="C", name="t")
    tc_d = _tc(index=0, tc_id="D", name="t")
    list(rx.stream(_ctx(), [_choice(tc_c), _choice(tc_d)]))
    assert (tc_c.index, tc_d.index) == (0, 1)


def _begins_and_args(
    events: list,
) -> tuple[list[LLMToolCallBegin], list[LLMToolCallArgumentDelta]]:
    begins = [e for e in events if isinstance(e, LLMToolCallBegin)]
    deltas = [e for e in events if isinstance(e, LLMToolCallArgumentDelta)]
    return begins, deltas


def test_converter_on_separates_collision() -> None:
    """reindex_tool_calls=True (default): коллизия id+index → два LLMToolCallBegin."""
    ctx = _ctx()
    conv = FromOpenAIChunkConverter(ctx.request_id)
    chunks = [
        _chunk(
            _choice(
                _tc(
                    index=0,
                    tc_id="A",
                    name="html_outline",
                    args='{"path":"a.html"}',
                )
            )
        ),
        _chunk(
            _choice(
                _tc(
                    index=0,
                    tc_id="B",
                    name="html_outline",
                    args='{"path":"b.html"}',
                )
            )
        ),
    ]
    events = list(conv.stream(ctx, chunks))
    begins, deltas = _begins_and_args(events)
    assert len(begins) == 2
    assert {b.tool_call_id for b in begins} == {"A", "B"}
    assert {b.index for b in begins} == {0, 1}
    # Два delta-события под разными index (склейки args нет).
    by_index: dict[int, list[str]] = {}
    for d in deltas:
        by_index.setdefault(d.index, []).append(d.arguments)
    assert "".join(by_index[0]) == '{"path":"a.html"}'
    assert "".join(by_index[1]) == '{"path":"b.html"}'


def test_converter_off_keeps_provider_collision() -> None:
    """reindex_tool_calls=False: коллизия не чинится — args склеиваются под index=0."""
    ctx = _ctx()
    conv = FromOpenAIChunkConverter(ctx.request_id, reindex_tool_calls=False)
    chunks = [
        _chunk(
            _choice(
                _tc(
                    index=0,
                    tc_id="A",
                    name="html_outline",
                    args='{"path":"a.html"}',
                )
            )
        ),
        _chunk(
            _choice(
                _tc(
                    index=0,
                    tc_id="B",
                    name="html_outline",
                    args='{"path":"b.html"}',
                )
            )
        ),
    ]
    events = list(conv.stream(ctx, chunks))
    begins, deltas = _begins_and_args(events)
    # Без реиндексации второй tool_call принимается как продолжение первого
    # (один и тот же index=0). Поведение — для документации и регрессии.
    assert {b.index for b in begins} == {0}
    args_under_0 = "".join(d.arguments for d in deltas if d.index == 0)
    assert args_under_0 == '{"path":"a.html"}{"path":"b.html"}'


def test_converter_on_keeps_well_formed_parallel() -> None:
    """reindex_tool_calls=True не ломает корректные параллельные tool_calls."""
    ctx = _ctx()
    conv = FromOpenAIChunkConverter(ctx.request_id)
    chunks = [
        _chunk(
            _choice(
                _tc(
                    index=0,
                    tc_id="A",
                    name="t",
                    args='{"a":1}',
                )
            )
        ),
        _chunk(
            _choice(
                _tc(
                    index=1,
                    tc_id="B",
                    name="t",
                    args='{"b":2}',
                )
            )
        ),
    ]
    events = list(conv.stream(ctx, chunks))
    begins, _ = _begins_and_args(events)
    assert {(b.tool_call_id, b.index) for b in begins} == {("A", 0), ("B", 1)}
