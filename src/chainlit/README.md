# boba-chainlit

Web-интерфейс для Boba-агента поверх [Chainlit](https://docs.chainlit.io).

Пакет тонкий: один процесс Chainlit поднимает общий DI-контейнер через
`ChatSession` (мирроринг `AgentHarness`), на каждое сообщение запускает
агентский цикл в worker-потоке и стримит `AgentEvent` в UI через
`ChainlitBridgeSink` (thread-safe очередь).

## Структура

- `src/boba/chainlit/app.py` — Chainlit entrypoint (`on_chat_start`,
  `on_message`); маппинг `AgentEvent` в `cl.Message` / `cl.Step`.
- `src/boba/chainlit/bridge.py` — `ChainlitBridgeSink` (sync → async мост).
- `src/boba/chainlit/session.py` — сборка контейнера и per-query запуск
  агента с подменой sink'а.

## Запуск

Из корня репозитория:

```bash
poetry install  # подтянет boba-chainlit + chainlit
BOBA_CONFIG=.vscode/config/config.toml \
LITELLM_API_KEY_FILE=.vscode/secrets/litellm_api_key \
WORKSPACE_BASE_DIR=.vscode/workspaces \
poetry run chainlit run src/chainlit/src/boba/chainlit/app.py -w
```

`-w` — live-reload при правке кода. Без него — обычный режим.

Все переменные окружения те же, что у CLI-запуска (`ConfigLoader`
читает их единообразно).

## Сессии

Каждая сессия чата получает свой `WorkspaceId.new()`. История
диалога (`messages.jsonl`) пишется внутри workspace — следующие
сообщения в той же сессии видят контекст предыдущих. При перезагрузке
вкладки создаётся новый workspace, старый остаётся на диске.

## Маппинг событий в UI

| AgentEvent | UI |
|---|---|
| `AnswerToken` / `AnswerComplete` | токены в основное `cl.Message` |
| `AnswerDiscarded` | очистка буфера ответа |
| `ThinkingToken` / `ThinkingComplete` | вложенный `cl.Step(type="run")` |
| `ToolCallBegin` / `ArgumentDelta` / `Complete` | `cl.Step(type="tool")`, `input` |
| `ToolResultReady` | `step.output` |
| `ToolExecutionFailed` / `ToolCallFormatFailed` | `step.is_error = True` |
| `GenerationFailed` / `PromptFailed` / `PersistenceFailed` / `MaxIterationsReached` / `RepeatedFormatFailure` | системное error-сообщение |
| `UserNoticeReady` | системное сообщение с severity |
