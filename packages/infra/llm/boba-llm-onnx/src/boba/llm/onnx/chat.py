"""Локальная чат-модель: onnxruntime-genai, рендер qwen-диалога, разбор ответа.

OnnxChatRuntime — низкоуровневый прогон одной загруженной модели: лок на
процесс, пошаговая генерация, грамматика ответа. OnnxChatModel поверх него
ведёт чат с инструментами; форма ответа (reply_schema) уходит грамматикой
llguidance, и ответ по построению — json по схеме.

Блок инструментов рендерится своими руками, а не параметром tools рантайма:
рантайм пересобирает схемы и теряет required и вложенные свойства.

Ошибки:
LlmError — модель не загрузилась, прогон сорвался, сообщение с картинками
    либо генерация упёрлась в потолок max_tokens (ответ неполон).
LlmProvidersError — секция не того провайдера или у провайдера нет
    эмбеддингов.
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
from typing import Any, ClassVar, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.llm.chat import (
    ChatDelta,
    ChatEvent,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    LlmError,
    ToolCall,
    ToolSpec,
)
from boba.llm.embedding import EmbeddingModel
from boba.llm.providers import (
    ChatModelConfig,
    EmbeddingModelConfig,
    LlmBackend,
    LlmProvider,
    LlmProviderManifest,
    LlmProvidersError,
)
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "MANIFEST",
    "LocalReplyParser",
    "OnnxBackend",
    "OnnxChatModel",
    "OnnxChatRuntime",
    "OnnxGenai",
    "OnnxProvider",
    "QwenDialogRender",
    "RunSpec",
]


class OnnxProvider(LlmProvider):
    """Секция `[llm.<имя>]` с kind = "onnx": каталог модели."""

    kind: Literal["onnx"]

    model_dir: str = Field(
        description=(
            "Каталог модели onnxruntime-genai: genai_config.json, веса, "
            "токенайзер и chat_template. Модель кладётся заранее."
        ),
    )


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
            msg = f"onnx chat: importing {self.MODULE} failed: {exc}"
            raise LlmError(msg) from exc

        logger.info("onnx runtime: %s imported in %dms", self.MODULE, imports.ms())

    def load(self, model_dir: str) -> tuple[OnnxModel, OnnxTokenizer]:
        try:
            model = self._module.Model(self._module.Config(model_dir))
            tokenizer = self._module.Tokenizer(model)
        except Exception as exc:
            msg = (
                f"onnx chat: loading model and tokenizer from {model_dir} "
                f"failed: {type(exc).__name__}: {exc}"
            )
            raise LlmError(msg) from exc

        return model, tokenizer

    def params(self, model: OnnxModel) -> OnnxParams:
        return self._module.GeneratorParams(model)

    def generator(self, model: OnnxModel, params: OnnxParams) -> OnnxGenerator:
        return self._module.Generator(model, params)


class GuidanceType(StrEnum):
    """Виды грамматик llguidance в onnxruntime-genai."""

    JSON_SCHEMA = "json_schema"
    REGEX = "regex"
    LARK = "lark"


class RunSpec(BaseModel):
    """Параметры одного прогона рантайма."""

    model_config = ConfigDict(frozen=True)

    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    guidance_kind: str = ""
    guidance_data: str = ""


class OnnxChatRuntime:
    """Прогон одной загруженной модели: лок, пошаговая генерация.

    Модель одна на процесс, поэтому прогоны сериализуются локом, а сам
    прогон уходит в поток: ONNX и так занимает все доступные ядра, loop
    остаётся свободен.
    """

    def __init__(self, model_dir: str, runtime: OnnxGenai) -> None:
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
            msg = (
                f"onnx chat: generation with {self._model_dir} failed after "
                f"{produced} token(s): {type(exc).__name__}: {exc}"
            )
            raise LlmError(msg) from exc
        finally:
            logger.info(
                "onnx runtime: %d token(s) in %dms",
                produced,
                elapsed.ms(),
            )

        # is_done не различает EOS и max_length: полный расход потолка
        # читается как обрыв — честная ошибка вместо тихо неполного ответа
        if produced >= spec.max_tokens:
            msg = (
                f"onnx chat: generation with {self._model_dir} hit the token "
                f"ceiling ({spec.max_tokens} tokens); raise max_tokens in sampling"
            )
            raise LlmError(msg)

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
    DESCRIPTION = "description"
    PARAMETERS = "parameters"
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

    def messages_json(self, request: ChatRequest) -> str:
        """Json-диалог для apply_chat_template; tools вшиты в system."""
        turns: list[dict[str, Any]] = []
        for message in self._with_tools(request):
            turns.append(self._turn(message))

        return json.dumps(turns, ensure_ascii=False)

    def _with_tools(self, request: ChatRequest) -> Sequence[ChatTurn]:
        if not request.tools:
            return request.messages

        block = self._tools_block(request.tools)

        messages = list(request.messages)
        if messages and messages[0].role is ChatRole.SYSTEM:
            head = messages[0]
            merged = head.model_copy(update={"content": f"{head.content}\n\n{block}"})
            return [merged, *messages[1:]]

        system = ChatTurn(role=ChatRole.SYSTEM, content=block)
        return [system, *messages]

    def _tools_block(self, tools: Sequence[ToolSpec]) -> str:
        lines: list[str] = [self.TOOLS_HEADER]
        for tool in tools:
            declared = {
                DialogField.NAME.value: tool.name,
                DialogField.DESCRIPTION.value: tool.description,
                DialogField.PARAMETERS.value: dict(tool.parameters),
            }
            lines.append(json.dumps(declared, ensure_ascii=False))

        return "\n".join(lines) + self.TOOLS_FOOTER

    def _turn(self, message: ChatTurn) -> dict[str, Any]:
        if message.images:
            msg = (
                f"onnx chat: message of role {message.role.value} carries "
                f"{len(message.images)} image(s), the local model has no vision"
            )
            raise LlmError(msg)

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
    CALL_ID_PREFIX: ClassVar[str] = "local-"
    """Локальная модель своих id вызовов не выдаёт."""

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

        calls: list[ToolCall] = []
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

    def _parse_call(self, raw: str) -> ToolCall | None:
        try:
            parsed = ParsedCall.model_validate_json(raw)
        except ValidationError:
            logger.warning(
                "onnx chat: tool call is not {name, arguments} json: %.200s", raw
            )
            return None

        return ToolCall(
            id=f"{self.CALL_ID_PREFIX}{uuid4().hex}",
            name=parsed.name,
            arguments=parsed.arguments,
        )


class SamplingKey(StrEnum):
    """Ключи админской таблицы sampling, которые понимает локальный рантайм."""

    MAX_TOKENS = "max_tokens"
    TEMPERATURE = "temperature"
    TOP_P = "top_p"
    REPLY_PREFIX = "reply_prefix"
    """Текст после метки ответа: у reasoning-моделей qwen3 пустой блок
    '<think>\\n\\n</think>\\n\\n' отключает размышления."""


class OnnxChatModel(ChatModel):
    """Реализация ChatModel на локальном рантайме: рендер qwen-диалога, поток
    дельт, грамматика по reply_schema.

    Сэмплинг приходит конвертом запроса; max_tokens обязателен — без потолка
    локальный прогон не останавливается.
    """

    QUEUE_SIZE: ClassVar[int] = 256

    def __init__(self, runtime: OnnxChatRuntime) -> None:
        self._runtime = runtime
        self._render = QwenDialogRender()

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        spec = self._spec(request)
        prompt = self._prompt(request)

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

    def _prompt(self, request: ChatRequest) -> str:
        rendered = self._runtime.render(self._render.messages_json(request))
        prefix = request.sampling.get(SamplingKey.REPLY_PREFIX.value, "")

        return f"{rendered}{prefix}"

    def _spec(self, request: ChatRequest) -> RunSpec:
        sampling = request.sampling
        known = {key.value for key in SamplingKey}
        unknown = sorted(set(sampling) - known)
        if unknown:
            logger.warning("onnx chat ignores sampling keys: %s", ", ".join(unknown))

        max_tokens = sampling.get(SamplingKey.MAX_TOKENS.value)
        if max_tokens is None:
            msg = (
                "onnx chat requires sampling.max_tokens (no other ceiling "
                f"exists), sampling has only {sorted(sampling)}"
            )
            raise LlmError(msg)

        guidance_kind = ""
        guidance_data = ""
        if request.reply_schema is not None:
            guidance_kind = GuidanceType.JSON_SCHEMA.value
            guidance_data = json.dumps(dict(request.reply_schema.parameters))

        return RunSpec(
            max_tokens=int(max_tokens),
            temperature=sampling.get(SamplingKey.TEMPERATURE.value),
            top_p=sampling.get(SamplingKey.TOP_P.value),
            guidance_kind=guidance_kind,
            guidance_data=guidance_data,
        )


class OnnxBackend(LlmBackend):
    """Реализация LlmBackend: одна загруженная модель на каталог.

    Загрузка идёт в конструкторе: провайдер собирается на старте приложения,
    и модель обслуживает все использования её каталога.
    """

    def __init__(self, provider: LlmProvider) -> None:
        if not isinstance(provider, OnnxProvider):
            msg = (
                f"onnx backend expects an OnnxProvider section, "
                f"got kind {provider.kind!r}"
            )
            raise LlmProvidersError(msg)

        self._provider = provider
        self._runtime = OnnxChatRuntime(provider.model_dir, OnnxGenai())

    def chat(self, cfg: ChatModelConfig) -> ChatModel:
        return OnnxChatModel(self._runtime)

    def embedding(self, cfg: EmbeddingModelConfig) -> EmbeddingModel:
        msg = (
            f"onnx provider at {self._provider.model_dir} has no embedding "
            f"models, asked for {cfg.model!r}"
        )
        raise LlmProvidersError(msg)

    async def aclose(self) -> None:
        return


MANIFEST = LlmProviderManifest(
    kind="onnx",
    config=OnnxProvider,
    backend=OnnxBackend,
)
