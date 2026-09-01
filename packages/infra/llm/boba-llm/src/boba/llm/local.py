"""Локальный чат-бэкенд: onnxruntime-genai, рендер qwen-диалога, разбор ответа.

OnnxChatRuntime — общий низкоуровневый прогон локальной модели: загрузка,
лок на процесс, пошаговая генерация. Поверх него живут LocalChatProvider
(чат с инструментами) и LocalOnnxGenerator (generation, ответ по схеме).

Блок инструментов рендерится своими руками, а не параметром tools рантайма:
рантайм пересобирает схемы и теряет required и вложенные свойства.

Ошибки:
ChatProviderError — модель не загрузилась или прогон сорвался.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import threading
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from boba.chat.provider import (
    ChatDelta,
    ChatEvent,
    ChatProvider,
    ChatProviderError,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    ToolCallRequest,
    ToolSpec,
)
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "LocalChatProvider",
    "LocalReplyParser",
    "OnnxChatRuntime",
    "OnnxGenai",
    "QwenDialogRender",
    "RunSpec",
]


class OnnxModel(Protocol):
    """Загруженная модель onnxruntime-genai."""


class OnnxTokenStream(Protocol):
    """Инкрементальный декодер токенов в текст."""

    @abstractmethod
    def decode(self, token: int) -> str: ...


class OnnxTokenizer(Protocol):
    """Токенайзер модели и шаблон диалога при нём."""

    @abstractmethod
    def encode(self, text: str) -> Sequence[int]: ...

    @abstractmethod
    def decode(self, tokens: Sequence[int]) -> str: ...

    @abstractmethod
    def create_stream(self) -> OnnxTokenStream: ...

    @abstractmethod
    def apply_chat_template(
        self,
        messages: str,
        *,
        add_generation_prompt: bool,
    ) -> str: ...


class OnnxParams(Protocol):
    """Параметры прогона: поиск и грамматика ответа."""

    @abstractmethod
    def set_search_options(self, **options: object) -> None: ...

    @abstractmethod
    def set_guidance(self, kind: str, data: str) -> None: ...


class OnnxGenerator(Protocol):
    """Пошаговая генерация одной последовательности."""

    @abstractmethod
    def append_tokens(self, tokens: Sequence[int]) -> None: ...

    @abstractmethod
    def generate_next_token(self) -> None: ...

    @abstractmethod
    def is_done(self) -> bool: ...

    @abstractmethod
    def get_next_tokens(self) -> Sequence[int]: ...

    @abstractmethod
    def get_sequence(self, index: int) -> Sequence[int]: ...


class OnnxGenai:
    """Вход в onnxruntime-genai: библиотека идёт без аннотаций, поэтому её
    объекты разбираются здесь один раз и дальше живут протоколами."""

    MODULE: ClassVar[str] = "onnxruntime_genai"

    def __init__(self) -> None:
        imports = Elapsed()
        try:
            self._module = importlib.import_module(self.MODULE)
        except ImportError as exc:
            msg = f"{self.MODULE} is not installed"
            raise ChatProviderError(msg) from exc

        logger.info("onnx runtime: %s imported in %dms", self.MODULE, imports.ms())

    def load(self, model_dir: str) -> tuple[OnnxModel, OnnxTokenizer]:
        try:
            model = self._module.Model(self._module.Config(model_dir))
            tokenizer = self._module.Tokenizer(model)
        except Exception as exc:
            msg = f"model not loaded: {model_dir}"
            raise ChatProviderError(msg) from exc

        return model, tokenizer

    def params(self, model: OnnxModel) -> OnnxParams:
        return self._module.GeneratorParams(model)

    def generator(self, model: OnnxModel, params: OnnxParams) -> OnnxGenerator:
        return self._module.Generator(model, params)


class RunSpec(BaseModel):
    """Параметры одного прогона рантайма."""

    model_config = ConfigDict(frozen=True)

    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    guidance_kind: str = ""
    guidance_data: str = ""


class OnnxChatRuntime:
    """Общий прогон локальной модели: загрузка, лок, пошаговая генерация.

    Модель одна на процесс, поэтому прогоны сериализуются локом, а сам
    прогон уходит в поток: ONNX и так занимает все доступные ядра, loop
    остаётся свободен.
    """

    def __init__(self, model_dir: str, runtime: OnnxGenai | None = None) -> None:
        if runtime is None:
            runtime = OnnxGenai()

        cores = len(os.sched_getaffinity(0))
        logger.info("onnx runtime: %s on %d core(s)", model_dir, cores)

        load = Elapsed()
        model, tokenizer = runtime.load(model_dir)
        logger.info("onnx runtime: %s loaded in %dms", model_dir, load.ms())

        self._model_dir = model_dir
        self._runtime = runtime
        self._model = model
        self._tokenizer = tokenizer
        self._lock = threading.Lock()

        # захваченный в момент fork замок остался бы захваченным в ребёнке
        # навсегда: владелец в ребёнка не переносится
        os.register_at_fork(after_in_child=self._reset_lock)

    def _reset_lock(self) -> None:
        self._lock = threading.Lock()

    @property
    def model_dir(self) -> str:
        return self._model_dir

    def render(self, messages_json: str) -> str:
        """Промпт по шаблону модели; сообщения — json списком ролей."""
        return self._tokenizer.apply_chat_template(
            messages_json,
            add_generation_prompt=True,
        )

    def run(
        self,
        prompt: str,
        spec: RunSpec,
        on_piece: Callable[[str], None],
        stopped: Callable[[], bool],
    ) -> None:
        """Прогон под локом: каждый декодированный кусок уходит в on_piece.

        stopped проверяется на каждом токене: True — прогон обрывается без
        ошибки, надо остановиться и освободить модель.
        """
        with self._lock:
            self._generate(prompt, spec, on_piece, stopped)

    def _generate(
        self,
        prompt: str,
        spec: RunSpec,
        on_piece: Callable[[str], None],
        stopped: Callable[[], bool],
    ) -> None:
        encoded = self._tokenizer.encode(prompt)

        params = self._runtime.params(self._model)
        params.set_search_options(**self._search_options(len(encoded), spec))

        if spec.guidance_kind:
            params.set_guidance(spec.guidance_kind, spec.guidance_data)

        elapsed = Elapsed()
        produced = 0
        try:
            generator = self._runtime.generator(self._model, params)
            generator.append_tokens(encoded)

            stream = self._tokenizer.create_stream()
            while not generator.is_done():
                if stopped():
                    return

                generator.generate_next_token()
                token = generator.get_next_tokens()[0]
                produced += 1

                piece = stream.decode(int(token))
                if piece:
                    on_piece(piece)
        except Exception as exc:
            msg = f"local generation failed: {self._model_dir}"
            raise ChatProviderError(msg) from exc
        finally:
            logger.info(
                "onnx runtime: %d token(s) in %dms",
                produced,
                elapsed.ms(),
            )

    @staticmethod
    def _search_options(prompt_tokens: int, spec: RunSpec) -> dict[str, object]:
        options: dict[str, object] = {
            "max_length": prompt_tokens + spec.max_tokens,
        }

        sampled = spec.temperature is not None or spec.top_p is not None
        options["do_sample"] = sampled

        if spec.temperature is not None:
            options["temperature"] = spec.temperature

        if spec.top_p is not None:
            options["top_p"] = spec.top_p

        return options


class DialogField(StrEnum):
    """Ключи json-диалога, который читает chat_template модели."""

    ROLE = "role"
    CONTENT = "content"
    REASONING_CONTENT = "reasoning_content"
    TOOL_CALLS = "tool_calls"
    TYPE = "type"
    FUNCTION = "function"
    NAME = "name"
    ARGUMENTS = "arguments"


class QwenDialogRender:
    """Сборка json-диалога и блока инструментов под chat_template qwen.

    Формат блока повторяет родной шаблон модели, но схемы аргументов идут
    полными: рантайм при передаче tools параметром их пересобирает и теряет
    required и вложенные свойства.
    """

    TOOLS_HEADER: ClassVar[str] = (
        "# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> "
        "XML tags:\n<tools>"
    )

    TOOLS_FOOTER: ClassVar[str] = (
        "\n</tools>\n\n"
        "For each function call, return a json object with function name and "
        "arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>"
    )

    @classmethod
    def messages_json(cls, request: ChatRequest) -> str:
        """Json-диалог для apply_chat_template; tools вшиты в system."""
        turns: list[dict[str, Any]] = []
        for message in cls._with_tools(request):
            turns.append(cls._turn(message))

        return json.dumps(turns, ensure_ascii=False)

    @classmethod
    def _with_tools(cls, request: ChatRequest) -> Sequence[ChatTurn]:
        if not request.tools:
            return request.messages

        block = cls._tools_block(request.tools)

        messages = list(request.messages)
        if messages and messages[0].role is ChatRole.SYSTEM:
            head = messages[0]
            merged = head.model_copy(update={"content": f"{head.content}\n\n{block}"})
            return [merged, *messages[1:]]

        system = ChatTurn(role=ChatRole.SYSTEM, content=block)
        return [system, *messages]

    @classmethod
    def _tools_block(cls, tools: Sequence[ToolSpec]) -> str:
        lines: list[str] = [cls.TOOLS_HEADER]
        for tool in tools:
            declared = {
                DialogField.NAME.value: tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            }
            lines.append(json.dumps(declared, ensure_ascii=False))

        return "\n".join(lines) + cls.TOOLS_FOOTER

    @classmethod
    def _turn(cls, message: ChatTurn) -> dict[str, Any]:
        turn: dict[str, Any] = {
            DialogField.ROLE.value: message.role.value,
            DialogField.CONTENT.value: message.content,
        }

        if message.reasoning:
            turn[DialogField.REASONING_CONTENT.value] = message.reasoning

        if message.tool_calls:
            calls: list[dict[str, Any]] = []
            for call in message.tool_calls:
                calls.append(
                    {
                        DialogField.TYPE.value: DialogField.FUNCTION.value,
                        DialogField.FUNCTION.value: {
                            DialogField.NAME.value: call.name,
                            DialogField.ARGUMENTS.value: dict(call.arguments),
                        },
                    }
                )
            turn[DialogField.TOOL_CALLS.value] = calls

        return turn


class ParsedCall(BaseModel):
    """Тело <tool_call>: имя и аргументы, как их написала модель."""

    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: Mapping[str, Any] = {}


class LocalReplyParser:
    """Инкрементальный разбор ответа модели: <think> и <tool_call> из потока.

    Куски приходят произвольной нарезкой — тег может быть расщеплён между
    ними, поэтому хвост, похожий на начало тега, придерживается в буфере.
    Наружу отдаются дельты рассуждений и текста; вызовы копятся и забираются
    целиком в конце.
    """

    THINK_OPEN: ClassVar[str] = "<think>"
    THINK_CLOSE: ClassVar[str] = "</think>"
    CALL_OPEN: ClassVar[str] = "<tool_call>"
    CALL_CLOSE: ClassVar[str] = "</tool_call>"

    _OPENERS: ClassVar[tuple[str, ...]] = ("<think>", "<tool_call>")

    def __init__(self) -> None:
        self._buffer = ""
        self._reasoning = False
        self._in_call = False
        self._calls: list[str] = []
        self._content: list[str] = []
        self._reasoning_text: list[str] = []
        self._content_started = False

    def feed(self, piece: str) -> ChatDelta | None:
        """Разбирает очередной кусок; None — наружу пока нечего отдать."""
        self._buffer += piece

        content: list[str] = []
        reasoning: list[str] = []

        while True:
            emitted = self._step()
            if emitted is None:
                break

            kind, text = emitted
            if not text:
                continue

            if kind:
                reasoning.append(text)
            else:
                content.append(text)

        grown_content = self._visible("".join(content))
        grown_reasoning = "".join(reasoning)
        if not grown_content and not grown_reasoning:
            return None

        return ChatDelta(content=grown_content, reasoning=grown_reasoning)

    def _visible(self, text: str) -> str:
        """Контент без пробельного префикса ответа: он в ленте не нужен."""
        if self._content_started:
            return text

        stripped = text.lstrip()
        if stripped:
            self._content_started = True

        return stripped

    def finish(self) -> ChatReply:
        """Финал: остаток буфера — текст, накопленные вызовы разбираются."""
        tail = self._buffer
        self._buffer = ""
        if tail:
            if self._reasoning:
                self._reasoning_text.append(tail)
            else:
                self._content.append(tail)

        content = "".join(self._content).strip("\n")
        reasoning = "".join(self._reasoning_text).strip("\n")

        calls: list[ToolCallRequest] = []
        for raw in self._calls:
            parsed = self._parse_call(raw)
            if parsed is None:
                # модель написала битый вызов: он остаётся текстом ответа
                content = f"{content}\n{raw}".strip("\n")
                continue

            calls.append(parsed)

        return ChatReply(content=content, reasoning=reasoning, tool_calls=calls)

    def _step(self) -> tuple[bool, str] | None:
        """Одна итерация автомата; (reasoning?, text) — наружу, None — стоп."""
        if self._in_call:
            closed = self._buffer.find(self.CALL_CLOSE)
            if closed < 0:
                return None

            self._calls.append(self._buffer[:closed].strip())
            self._buffer = self._buffer[closed + len(self.CALL_CLOSE) :]
            self._in_call = False
            return (False, "")

        if self._reasoning:
            closed = self._buffer.find(self.THINK_CLOSE)
            if closed < 0:
                safe = self._safe_length(self.THINK_CLOSE)
                return self._drain(safe, reasoning=True)

            text = self._buffer[:closed]
            self._buffer = self._buffer[closed + len(self.THINK_CLOSE) :]
            self._reasoning = False
            self._reasoning_text.append(text)
            return (True, text)

        opened = self._first_opener()
        if opened is None:
            safe = self._safe_length(*self._OPENERS)
            return self._drain(safe, reasoning=False)

        position, tag = opened
        text = self._buffer[:position]
        self._buffer = self._buffer[position + len(tag) :]

        if tag == self.THINK_OPEN:
            self._reasoning = True
        else:
            self._in_call = True

        self._content.append(text)
        return (False, text)

    def _drain(self, safe: int, *, reasoning: bool) -> tuple[bool, str] | None:
        """Отдаёт заведомо безопасную часть буфера; пусто — разбор ждёт."""
        if safe <= 0:
            return None

        text = self._buffer[:safe]
        self._buffer = self._buffer[safe:]

        if reasoning:
            self._reasoning_text.append(text)
        else:
            self._content.append(text)

        return (reasoning, text)

    def _first_opener(self) -> tuple[int, str] | None:
        found: tuple[int, str] | None = None
        for tag in self._OPENERS:
            position = self._buffer.find(tag)
            if position < 0:
                continue

            if found is None or position < found[0]:
                found = (position, tag)

        return found

    def _safe_length(self, *tags: str) -> int:
        """Длина буфера, которая точно не начало одного из тегов."""
        safe = len(self._buffer)
        for tag in tags:
            for width in range(min(len(tag), safe), 0, -1):
                if self._buffer.endswith(tag[:width]):
                    safe = min(safe, len(self._buffer) - width)
                    break

        return safe

    @staticmethod
    def _parse_call(raw: str) -> ToolCallRequest | None:
        try:
            parsed = ParsedCall.model_validate_json(raw)
        except ValidationError:
            logger.warning("local chat: malformed tool call: %.200s", raw)
            return None

        return ToolCallRequest(
            id=LocalCallId.new(),
            name=parsed.name,
            arguments=parsed.arguments,
        )


class LocalCallId:
    """Идентификаторы вызовов локальной модели: она своих не выдаёт."""

    PREFIX: ClassVar[str] = "local-"

    @classmethod
    def new(cls) -> str:
        return f"{cls.PREFIX}{uuid4().hex}"


class LocalChatProvider(ChatProvider):
    """ChatProvider на локальном рантайме: рендер qwen-диалога, поток дельт.

    Сэмплинг приходит конвертом запроса; max_tokens обязателен — без потолка
    локальный прогон не останавливается.
    """

    QUEUE_SIZE: ClassVar[int] = 256

    def __init__(self, runtime: OnnxChatRuntime) -> None:
        self._runtime = runtime

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        spec = self._spec(request.sampling)
        prompt = self._runtime.render(QwenDialogRender.messages_json(request))

        parser = LocalReplyParser()
        queue: asyncio.Queue[str | None | BaseException] = asyncio.Queue(
            maxsize=self.QUEUE_SIZE
        )
        loop = asyncio.get_running_loop()
        stop = threading.Event()

        def on_piece(piece: str) -> None:
            future = asyncio.run_coroutine_threadsafe(queue.put(piece), loop)
            future.result()

        def run() -> None:
            try:
                self._runtime.run(prompt, spec, on_piece, stop.is_set)
            except BaseException as exc:
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
                return

            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        worker = loop.run_in_executor(None, run)
        try:
            while True:
                arrived = await queue.get()
                if arrived is None:
                    break

                if isinstance(arrived, BaseException):
                    raise arrived

                delta = parser.feed(arrived)
                if delta is not None:
                    yield delta

            yield parser.finish()
        finally:
            stop.set()
            await worker

    KNOWN_SAMPLING: ClassVar[frozenset[str]] = frozenset(
        {"max_tokens", "temperature", "top_p"}
    )
    """Ключи админской таблицы sampling, которые понимает локальный рантайм."""

    @classmethod
    def _spec(cls, sampling: Mapping[str, Any]) -> RunSpec:
        unknown = sorted(set(sampling) - cls.KNOWN_SAMPLING)
        if unknown:
            logger.warning("local chat ignores sampling keys: %s", ", ".join(unknown))

        max_tokens = sampling.get("max_tokens")
        if max_tokens is None:
            msg = "local chat requires sampling.max_tokens: no other ceiling exists"
            raise ChatProviderError(msg)

        return RunSpec(
            max_tokens=int(max_tokens),
            temperature=sampling.get("temperature"),
            top_p=sampling.get("top_p"),
        )
