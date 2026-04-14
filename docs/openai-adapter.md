# OpenAI Adapter: конкретные реализации

Реализации абстрактных `MessageSerializer`, `LLMCompletionService`, `DeltaAssembler`,
определённых в [workspace-architecture.md](workspace-architecture.md#6-llm-adapter).

---

## Модели API (OpenAI-совместимый формат)

```python
@dataclass(frozen=True)
class ApiFunctionCall:
    """Вложенный объект function внутри tool_call."""
    name: str
    arguments: str   # JSON-строка

@dataclass(frozen=True)
class ApiToolCall:
    """Tool call в формате OpenAI API."""
    id: str
    type: str        # "function"
    function: ApiFunctionCall

@dataclass(frozen=True)
class ApiMessage:
    """Сообщение в формате OpenAI API."""
    role: str                                  # "system", "developer", "user", "assistant", "tool"
    content: str | None = None
    tool_calls: list[ApiToolCall] | None = None # только для assistant
    tool_call_id: str | None = None             # только для tool
```

---

## OpenAIMessageSerializer

```python
class OpenAIMessageSerializer(MessageSerializer[ApiMessage]):
    """Сериализует доменные модели в формат OpenAI API.
    Склеивает AssistantMessage + AssistantToolMessage в одно API-сообщение."""

    def serialize(self, messages: Iterator[LLMMessage]) -> Iterator[ApiMessage]:
        pending_assistant: AssistantMessage | None = None

        for msg in messages:
            if pending_assistant is not None:
                if isinstance(msg, AssistantToolMessage):
                    yield ApiMessage(
                        role="assistant",
                        content=pending_assistant.content,
                        tool_calls=[self._to_api_tool_call(tc)
                                    for tc in msg.tool_calls],
                    )
                    pending_assistant = None
                    continue
                else:
                    yield self._to_api_message(pending_assistant)
                    pending_assistant = None

            if isinstance(msg, AssistantMessage):
                pending_assistant = msg
            else:
                yield self._to_api_message(msg)

        if pending_assistant is not None:
            yield self._to_api_message(pending_assistant)

    def _to_api_message(self, message: LLMMessage) -> ApiMessage:
        match message:
            case SystemMessage(content=c):
                return ApiMessage(role="system", content=c)
            case DeveloperMessage(content=c):
                return ApiMessage(role="developer", content=c)
            case UserMessage(content=c):
                return ApiMessage(role="user", content=c)
            case AssistantMessage(content=c):
                return ApiMessage(role="assistant", content=c)
            case AssistantToolMessage(tool_calls=calls):
                return ApiMessage(
                    role="assistant",
                    tool_calls=[self._to_api_tool_call(tc) for tc in calls],
                )
            case ToolMessage(content=c, tool_call_id=tid):
                return ApiMessage(role="tool", content=c, tool_call_id=tid)

    def _to_api_tool_call(self, tc: ToolCall) -> ApiToolCall:
        return ApiToolCall(
            id=tc.id,
            type="function",
            function=ApiFunctionCall(
                name=tc.tool_id.name,
                arguments=json.dumps(asdict(tc.arguments)),
            ),
        )
```

---

## OpenAICompletionService

```python
class OpenAICompletionService(LLMCompletionService[ApiMessage]):
    """Стриминг через OpenAI-совместимый API."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client

    def stream_completion(
        self,
        messages: Iterator[ApiMessage],
        tools: Iterator[ToolDefinition],
        model: str,
    ) -> Iterator[CompletionDelta]:
        stream = self._client.chat.completions.create(
            model=model,
            messages=list(messages),
            tools=list(tools),
            stream=True,
            stream_options={"include_usage": True},
        )

        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                yield CompletionDelta(content=delta.content)

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield CompletionDelta(reasoning_content=reasoning)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    func = tc.function
                    yield CompletionDelta(
                        tool_call_index=tc.index,
                        tool_call_id=tc.id or None,
                        tool_call_name=func.name if func else None,
                        tool_call_arguments=func.arguments if func else None,
                    )

            if choice.finish_reason:
                yield CompletionDelta(
                    finish_reason=choice.finish_reason,
                    request_id=chunk.id,
                    model=chunk.model,
                )
```

---

## OpenAIDeltaAssembler

```python
class OpenAIDeltaAssembler(DeltaAssembler):
    """Собирает поток CompletionDelta в поток LLMMessage.
    Yield'ит каждое сообщение как только оно готово."""

    def __init__(self, tools_service: ToolsService) -> None:
        self._tools = tools_service
        self._meta: LLMResponseMeta | None = None

    def assemble(self, deltas: Iterator[CompletionDelta]) -> Iterator[LLMMessage]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # tool_calls: {index → (id, name, [argument_chunks])}
        tool_calls: dict[int, tuple[str, str, list[str]]] = {}
        finish_reason: str | None = None
        request_id: str = ""
        model: str = ""

        for delta in deltas:
            if delta.content:
                content_parts.append(delta.content)

            if delta.reasoning_content:
                reasoning_parts.append(delta.reasoning_content)

            if delta.tool_call_index is not None:
                idx = delta.tool_call_index
                if idx not in tool_calls:
                    tool_calls[idx] = (delta.tool_call_id or "", delta.tool_call_name or "", [])
                if delta.tool_call_arguments:
                    tool_calls[idx][2].append(delta.tool_call_arguments)

            if delta.finish_reason:
                finish_reason = delta.finish_reason
            if delta.request_id:
                request_id = delta.request_id
            if delta.model:
                model = delta.model

        # --- Yield готовых сообщений ---

        if reasoning_parts:
            yield ThinkingMessage(content="".join(reasoning_parts))

        if content_parts:
            yield AssistantMessage(content="".join(content_parts))

        if tool_calls:
            calls = []
            for idx in sorted(tool_calls):
                tc_id, tc_name, arg_chunks = tool_calls[idx]
                raw_args = json.loads("".join(arg_chunks))
                tool = self._tools.get(ToolId(tc_name))
                params = tool.params_type(**raw_args)
                calls.append(ToolCall(id=tc_id, tool_id=ToolId(tc_name), arguments=params))
            yield AssistantToolMessage(tool_calls=calls)

        self._meta = LLMResponseMeta(
            request_id=request_id,
            model=model,
            stop_reason=finish_reason or "",
            usage=TokenUsage(),  # usage приходит отдельно, можно расширить
        )

    def get_meta(self) -> LLMResponseMeta:
        if self._meta is None:
            raise RuntimeError("assemble() not consumed yet")
        return self._meta
```

---

## Пример использования (Agent loop)

```python
# --- DI создаёт конкретные реализации, код работает через абстракции ---
serializer: MessageSerializer = OpenAIMessageSerializer()
completion: LLMCompletionService = OpenAICompletionService(client)
assembler: DeltaAssembler = OpenAIDeltaAssembler(tools_service)

# --- Agent loop ---

while True:
    # 1. Сериализация: Iterator[LLMMessage] → Iterator[ApiMessage]
    api_messages = serializer.serialize(history.get_messages())

    # 2. Стриминг: Iterator[ApiMessage] → Iterator[CompletionDelta]
    deltas = completion.stream_completion(api_messages, tools, model)

    # 3. Сборка: Iterator[CompletionDelta] → Iterator[LLMMessage]
    turn_id = uuid4().hex
    for msg in assembler.assemble(deltas):
        # Каждое сообщение сохраняется сразу, не копится
        history.add_message(msg, turn_id=turn_id, parent_id=last_id)

    # 4. Проверка: продолжать или выйти
    meta = assembler.get_meta()
    if meta.stop_reason in ("stop", "end_turn"):
        break

    # 5. Выполнение tools → ToolMessage → следующая итерация цикла
    for msg in history.get_turn(turn_id):
        if isinstance(msg.message, AssistantToolMessage):
            for tc in msg.message.tool_calls:
                result = await tools_service.execute(tc.tool_id, tc.arguments)
                history.add_message(
                    ToolMessage(content=result.content, tool_call_id=tc.id),
                    turn_id=turn_id, parent_id=last_id,
                )
```
