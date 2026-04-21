# boba-chainlit

Минимальный web-UI для Boba-агента поверх [Chainlit](https://docs.chainlit.io).

## Функционал

* **Stub-авторизация**: любая комбинация login/password → фиксированный
  пользователь `boba`.
* **Upload файлов**: штатная скрепка в composer'е. Файлы сохраняются
  в project-workspace пользователя (`uploads/<name>`), agent видит их
  через свои file-tools (`ls uploads/`, `cat …`, `grep …`). Повторная
  загрузка с тем же именем перезаписывает файл.
* **Чат с агентом**: сообщения идут через `AgentHarness`-подобный
  `ChatSession`, события стримятся в UI (`cl.Message`/`cl.Step`).
* **Выбор модели**: `cl.ChatSettings` (шестерёнка в composer'е) —
  Select-виджет со списком из `[chainlit] models` в `config.toml`.

Управлением файлами (просмотр, удаление, поиск) делегирует агенту через
его file-tools — отдельного UI для этого в приложении нет.

## Структура

| Файл | Роль |
|---|---|
| `src/boba/chainlit/app.py` | Chainlit entrypoint: auth, on_chat_start, on_message, рендерер событий |
| `src/boba/chainlit/bridge.py` | `ChainlitBridgeSink` — мост sync-агент → async-очередь |
| `src/boba/chainlit/session.py` | `ChatSession` — DI-контейнер и per-query запуск агента |
| `src/boba/chainlit/files.py` | `save_upload` — запись attachment'а в project-workspace |
| `src/boba/chainlit/config.py` | Чтение секции `[chainlit]` из `BOBA_CONFIG` |
| `src/boba/chainlit/__main__.py` | Entry point `python -m boba.chainlit` (прокидывает `CHAINLIT_*` env до импорта chainlit) |
| `.chainlit/config.toml` | Настройки фронта Chainlit (в т.ч. `spontaneous_file_upload`) |
| `chainlit.md` | Welcome-экран |

## Запуск

Из корня репозитория:

```bash
poetry install  # подтянет boba-chainlit + chainlit
BOBA_CONFIG=.vscode/config/config.toml \
LITELLM_API_KEY_FILE=.vscode/secrets/litellm_api_key \
WORKSPACE_BASE_DIR=.vscode/workspaces \
poetry run python -m boba.chainlit
```

Переменные окружения те же, что и у CLI — читаются `ConfigLoader`'ом.
`__main__.py` дополнительно прокидывает секцию `[chainlit]` из TOML в
`CHAINLIT_HOST`/`CHAINLIT_PORT`/`CHAINLIT_ROOT_PATH`/`CHAINLIT_AUTH_SECRET`
до импорта chainlit.

## Сессии и workspace

`WorkspaceId` — детерминированный от `user.identifier` (UUID5 с
фиксированным namespace). Один и тот же пользователь всегда попадает
в один и тот же workspace, поэтому `messages.jsonl` и `uploads/`
переживают перезагрузку страницы и новые chat-треды.

## Маппинг AgentEvent → UI

| AgentEvent | UI |
|---|---|
| `AnswerToken` | stream в `cl.Message` |
| `AnswerDiscarded` | очистка буфера ответа |
| `AnswerComplete` | закрытие message |
| `RefusalToken` / `RefusalComplete` | отдельный `cl.Message(author="refusal")` |
| `ThinkingToken` | stream в `cl.Step(type="run")` |
| `GenerationStarted` / `GenerationDone` | `cl.Step(type="llm")` с `finish_reason` |
| `StageStarted` / `StageCompleted` | `cl.Step(type="run")` с `output=detail` |
| `ToolCallBegin` / `ArgumentDelta` / `Complete` | `cl.Step(type="tool")`, stream в `input` |
| `ToolResultReady` | `step.output` |
| `ToolExecutionFailed` / `ToolCallFormatFailed` | `is_error=True` + message |
| `GenerationFailed` / `PromptFailed` / `PersistenceFailed` / `MaxIterationsReached` / `RepeatedFormatFailure` | системное error-сообщение |
| `UserNoticeReady` | системное сообщение с severity |

Рендерер — streaming-first: `*Complete` события используют только как
сигнал закрытия канала, `content` из них не копируется (дельты уже
в Chainlit).
