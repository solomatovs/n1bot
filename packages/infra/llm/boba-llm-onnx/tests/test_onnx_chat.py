"""Локальная чат-модель: разбор ответа, потолок токенов, рендер диалога, грамматика."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from boba.llm.chat import (
    ChatDelta,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    LlmError,
    ToolCall,
    ToolSpec,
)
from boba.llm.onnx.chat import (
    LocalReplyParser,
    OnnxChatModel,
    OnnxChatRuntime,
    OnnxGenai,
    OnnxGenerator,
    OnnxModel,
    OnnxParams,
    OnnxTokenizer,
    OnnxTokenStream,
    QwenDialogRender,
    RunSpec,
)

pytestmark = pytest.mark.anyio


def _feed(parser: LocalReplyParser, text: str, width: int) -> list[ChatDelta]:
    """Скармливает текст кусками фиксированной ширины."""
    deltas: list[ChatDelta] = []
    for start in range(0, len(text), width):
        delta = parser.feed(text[start : start + width])
        if delta is not None:
            deltas.append(delta)

    return deltas


class TestLocalReplyParser:
    """Разбор ответа локальной модели: think, tool_call, произвольная нарезка."""

    REPLY = (
        "<think>\nобдумываю запрос\n</think>\n\n"
        "Начало ответа "
        '<tool_call>\n{"name": "kb_fts_search", '
        '"arguments": {"query": "kerberos", "intent": "ищу"}}\n</tool_call>'
        " конец"
    )

    @pytest.mark.parametrize("width", [1, 3, 7, 1000])
    def test_split_does_not_depend_on_chunking(self, width: int) -> None:
        parser = LocalReplyParser()
        deltas = _feed(parser, self.REPLY, width)
        reply = parser.finish()

        reasoning = "".join(d.reasoning for d in deltas)
        if "обдумываю запрос" not in reasoning:
            raise AssertionError(f"рассуждения из дельт: {reasoning!r}")

        if reply.reasoning.strip() != "обдумываю запрос":
            raise AssertionError(f"рассуждения финала: {reply.reasoning!r}")

        if "Начало ответа" not in reply.content or "конец" not in reply.content:
            raise AssertionError(f"текст финала: {reply.content!r}")

        if "<tool_call>" in reply.content or "<think>" in reply.content:
            raise AssertionError(f"теги не вычищены: {reply.content!r}")

        if len(reply.tool_calls) != 1:
            raise AssertionError(f"вызовы: {reply.tool_calls}")

        call = reply.tool_calls[0]
        if call.name != "kb_fts_search":
            raise AssertionError(call.name)
        if call.arguments != {"query": "kerberos", "intent": "ищу"}:
            raise AssertionError(call.arguments)
        if not call.id:
            raise AssertionError("вызов получил синтетический id")

    def test_malformed_call_stays_in_content(self) -> None:
        parser = LocalReplyParser()
        _feed(parser, "до <tool_call>это не json</tool_call> после", 5)
        reply = parser.finish()

        if reply.tool_calls:
            raise AssertionError(f"битый вызов не вызов: {reply.tool_calls}")

        if "это не json" not in reply.content:
            raise AssertionError(f"битый вызов остался текстом: {reply.content!r}")

    def test_leading_whitespace_is_not_streamed(self) -> None:
        """Пробелы между think и текстом не засоряют стрим ответа."""
        parser = LocalReplyParser()
        deltas = _feed(parser, "<think>x</think>\n\n  ответ", 3)

        streamed = "".join(d.content for d in deltas)
        if streamed != "ответ"[: len(streamed)]:
            raise AssertionError(f"стрим начался с текста: {streamed!r}")

    def test_plain_text_passes_through(self) -> None:
        parser = LocalReplyParser()
        deltas = _feed(parser, "просто ответ без тегов", 4)
        reply = parser.finish()

        if reply.content != "просто ответ без тегов":
            raise AssertionError(reply.content)
        if reply.reasoning or reply.tool_calls:
            raise AssertionError("ни рассуждений, ни вызовов")

        streamed = "".join(d.content for d in deltas)
        if streamed != reply.content[: len(streamed)]:
            raise AssertionError("дельты — префикс финала")


class _FakeModel(OnnxModel):
    """Пустышка загруженной модели."""


class _FakeStream(OnnxTokenStream):
    def decode(self, token: int) -> str:
        return "x"


class _FakeTokenizer(OnnxTokenizer):
    def encode(self, text: str) -> Sequence[int]:
        return [1, 2, 3]

    def decode(self, tokens: Sequence[int]) -> str:
        return "x" * len(tokens)

    def create_stream(self) -> OnnxTokenStream:
        return _FakeStream()

    def apply_chat_template(self, messages: str, *, add_generation_prompt: bool) -> str:
        return messages


class _FakeParams(OnnxParams):
    def __init__(self) -> None:
        self.max_length = 0
        self.guidance: tuple[str, str] | None = None

    def set_search_options(self, **options: object) -> None:
        raw = options["max_length"]
        if not isinstance(raw, int):
            raise AssertionError(options)
        self.max_length = raw

    def set_guidance(self, kind: str, data: str) -> None:
        self.guidance = (kind, data)


class _FakeGenerator(OnnxGenerator):
    """Генерация до EOS либо до max_length — как настоящий рантайм."""

    def __init__(self, max_length: int, eos_after: int | None) -> None:
        self._max_length = max_length
        self._eos_after = eos_after
        self._held = 0
        self._produced = 0

    def append_tokens(self, tokens: Sequence[int]) -> None:
        self._held += len(tokens)

    def is_done(self) -> bool:
        if self._eos_after is not None and self._produced >= self._eos_after:
            return True

        return self._held >= self._max_length

    def generate_next_token(self) -> None:
        self._held += 1
        self._produced += 1

    def get_next_tokens(self) -> Sequence[int]:
        return [42]

    def get_sequence(self, index: int) -> Sequence[int]:
        return []


class _FakeGenai(OnnxGenai):
    """Рантайм без onnxruntime_genai: генерация по правилам фейка."""

    def __init__(self, eos_after: int | None = None) -> None:
        self._eos_after = eos_after
        self.params_seen: list[_FakeParams] = []

    def load(self, model_dir: str) -> tuple[OnnxModel, OnnxTokenizer]:
        return _FakeModel(), _FakeTokenizer()

    def params(self, model: OnnxModel) -> OnnxParams:
        built = _FakeParams()
        self.params_seen.append(built)
        return built

    def generator(self, model: OnnxModel, params: OnnxParams) -> OnnxGenerator:
        if not isinstance(params, _FakeParams):
            raise AssertionError(type(params))
        return _FakeGenerator(params.max_length, self._eos_after)


class TestLocalTokenCeiling:
    """Полный расход max_tokens локального прогона — честная ошибка."""

    def test_hitting_the_ceiling_raises(self) -> None:
        runtime = OnnxChatRuntime("fake-model", _FakeGenai())
        pieces: list[str] = []

        with pytest.raises(LlmError, match="hit the token ceiling"):
            runtime.run(
                "prompt",
                RunSpec(max_tokens=5),
                pieces.append,
                lambda: False,
            )

        if len(pieces) != 5:
            raise AssertionError(pieces)

    def test_eos_before_the_ceiling_is_fine(self) -> None:
        runtime = OnnxChatRuntime("fake-model", _FakeGenai(eos_after=2))
        pieces: list[str] = []

        runtime.run("prompt", RunSpec(max_tokens=5), pieces.append, lambda: False)

        if len(pieces) != 2:
            raise AssertionError(pieces)


class TestQwenDialogRender:
    """Сборка json-диалога: tools в system, роли и вызовы в сообщениях."""

    TOOLS = (
        ToolSpec(
            name="kb_fts_search",
            description="Поиск.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    )

    def test_tools_merge_into_system_with_full_schema(self) -> None:
        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.SYSTEM, content="Ты ассистент"),
                ChatTurn(role=ChatRole.USER, content="вопрос"),
            ],
            tools=self.TOOLS,
        )

        turns = json.loads(QwenDialogRender().messages_json(request))

        if len(turns) != 2:
            raise AssertionError(f"turns: {turns}")

        system = turns[0]["content"]
        if "Ты ассистент" not in system:
            raise AssertionError("промпт профиля сохранён")
        if '"required": ["query"]' not in system:
            raise AssertionError(f"схема аргументов полная: {system}")
        if "<tools>" not in system or "</tools>" not in system:
            raise AssertionError("блок tools отрендерен")

    def test_assistant_calls_and_tool_role(self) -> None:
        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.USER, content="вопрос"),
                ChatTurn(
                    role=ChatRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="kb_fts_search",
                            arguments={"query": "kerberos"},
                        )
                    ],
                ),
                ChatTurn(role=ChatRole.TOOL, content="found", tool_call_id="c1"),
            ],
        )

        turns = json.loads(QwenDialogRender().messages_json(request))

        assistant = turns[1]
        calls = assistant["tool_calls"]
        if calls[0]["function"]["name"] != "kb_fts_search":
            raise AssertionError(calls)
        if calls[0]["function"]["arguments"] != {"query": "kerberos"}:
            raise AssertionError(calls)

        if turns[2]["role"] != "tool":
            raise AssertionError(turns[2])


class TestOnnxChatModel:
    """Модель поверх рантайма: грамматика по схеме, обязательный max_tokens."""

    async def test_reply_schema_becomes_json_guidance(self) -> None:
        genai = _FakeGenai(eos_after=2)
        model = OnnxChatModel(OnnxChatRuntime("fake-model", genai))
        schema = ToolSpec(name="Answer", description="d", parameters={"type": "object"})
        request = ChatRequest(
            messages=[ChatTurn(role=ChatRole.USER, content="hi")],
            reply_schema=schema,
            sampling={"max_tokens": 8, "reply_prefix": ""},
        )

        reply = await model.reply(request)

        if not isinstance(reply, ChatReply):
            raise AssertionError(reply)
        if genai.params_seen[0].guidance != ("json_schema", '{"type": "object"}'):
            raise AssertionError(genai.params_seen[0].guidance)

    async def test_missing_max_tokens_is_an_error(self) -> None:
        model = OnnxChatModel(OnnxChatRuntime("fake-model", _FakeGenai(eos_after=1)))
        request = ChatRequest(messages=[ChatTurn(role=ChatRole.USER, content="hi")])

        with pytest.raises(LlmError, match="max_tokens"):
            await model.reply(request)
