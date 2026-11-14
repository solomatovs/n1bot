# Рефакторинг boba: история и SQL-адаптеры

Цель — агент с типизированной историей в PostgreSQL и готовыми JSON-результатами SQL
для OpenAI и ACP. Совместимость с boba-aid, старым журналом и остальными потребителями
не входит в этот этап. Промежуточная неработоспособность приложения допустима.

Модели: [история](agent-history-and-request-assembly.md), [ACP](agent-history-to-acp.md).
SQL и чистые валидаторы: [experiments/history-sql](../experiments/history-sql).
**Изменение кода агента начинается после проверки этих контрактов.** Этот документ
определяет последующую замену компонентов; производственный агент пока не изменён.

## 1. Границы исполнения

```text
ACP / MCP / LLM / команды / фоновые задачи
    → производитель типизированных записей
    → HistoryStore.append
    → append.sql + проверка ссылок + commit
    → records с назначенными seq

records → openai.sql → OpenAiRequestValidator → готовый JSON → HTTP
records → acp_*.sql → AcpHistoryValidator → готовый JSON → ACP

дельты текущего ответа → потоковый ACP-адаптер → подписчики сессии
завершённый ответ → HistoryStore.append → records
```

Сборка не читает конфигурацию процесса, часы, файлы или MCP. Производители получают
эти данные, сохраняют структурные значения и готовый текст. SQL использует сохранённые
данные; транспорт не дописывает model, sampling, stream_options или tool_choice.

Несколько поступивших во время tools сообщений сразу сохраняются как input. После
результатов tools агент принимает всю ожидающую пачку по исходным seq и сохраняет одну
границу запроса. SQL включает текст по seq принятия. Отмена ожидающего ввода фиксируется
MessageRejectedRecord. Отдельного хранилища очереди и фонового сброса записей нет.

## 2. Компоненты, которые остаются

### `HistoryStore` / `PostgresHistoryStore`

HistoryStore — порт агента; PostgresHistoryStore выполняет SQL и управляет короткой
транзакцией записи. Ядро не содержит INSERT, назначения seq или преобразования формата строки.

```python
class HistoryStore(Protocol):
    async def append(
        self,
        session_id: str,
        records: tuple[HistoryRecord, ...],
        *,
        expected_seq: int | None = None,
    ) -> tuple[HistoryRecord, ...]: ...

    async def read(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        through_seq: int | None = None,
    ) -> History: ...

    async def pending_inputs(self, session_id: str) -> tuple[HistoryRecord, ...]: ...
```

append возвращает результат после commit. Неизвестный исход записи разрешается повтором
той же пачки с прежними ID и содержимым; конфликт данных возвращается явно.
Проверка expected_seq и назначение seq выполняются под блокировкой записи одной сессии.
Чтение блокировку не берёт. Блокировка записи не удерживается во время HTTP, MCP или ACP.

Право доступа к сессии и единственный исполнитель сессии сохраняются как отдельные
обязанности сервера. Один исполнитель может обслуживать несколько ACP-подписчиков.

### `OpenAiHistoryAdapter`

Выполняет openai.sql и проверяет результат. Возвращает provider, request и данные
проверки. request уже имеет формат OpenAI; ChatRequest/ChatTurn между SQL и HTTP нет.

```python
class OpenAiHistoryAdapter(Protocol):
    async def build(
        self,
        session_id: str,
        request_id: str | None = None,
        *,
        through_seq: int | None = None,
    ) -> OpenAiRequestDraft:
        """Проверенный результат SQL; для HTTP используются provider и request."""
        ...
```

Срез фиксируется до отправки. Граница, разовые настройки и служебные входы уже находятся
в records. Пересказ и классификатор разрешений используют этот же адаптер в той же сессии:
RequestContextRecord выбирает контекст, RequestMessageRecord содержит собственный ввод,
RequestOutputRecord сохраняет служебный ответ. Ответ не попадает в основной диалог автоматически.

### `AcpHistoryAdapter`

```python
class AcpHistoryAdapter(Protocol):
    async def history(
        self, session_id: str, after_seq: int, through_seq: int
    ) -> AcpHistoryDraft: ...

    async def state(self, session_id: str, through_seq: int) -> AcpStateDraft: ...
    async def commands(self, session_id: str, through_seq: int) -> AcpCommandsDraft: ...
    async def options(self, session_id: str, through_seq: int) -> AcpOptionsDraft: ...
    async def notice(self, session_id: str, record_id: str) -> AcpNoticeDraft: ...
    async def permission(self, session_id: str, record_id: str) -> AcpPermissionDraft: ...
    async def elicitation(self, session_id: str, record_id: str) -> AcpElicitationDraft: ...
```

Каждый метод выполняет одноимённый SQL и проверяет результат. Транспорт отправляет
исходный JSON из SQL, не пересобирая поля из Pydantic-модели. history восстанавливает
показ; permission и elicitation вызываются координатором для конкретного действия,
а не при replay. Каталоги commands/options и state читаются на той же границе, что история.

### `Agent`, `ToolRunner`, координаторы

Agent принимает записи, запускает шаг после подтверждённого сохранения и управляет
отменой. В памяти остаются текущая операция, её request_id, незавершённый ответ,
исполняемые задачи и подключения. Полной копии истории для сборки нет.

ToolRunner сохраняет проверку разрешений и фактическое исполнение tools. Каталог
MCP и поиск tools остаются исполнительными компонентами. Полученные определения
сохраняются как ToolSchemaRecord до следующей сборки. Наличие tool в JSON запроса
не является разрешением выполнить tool.

TaskCoordinator, ElicitationCoordinator и HistoryCompactor остаются производителями
записей. Компактор переносит сохранённые состояния и закрытый хвост диалога;
не обновляет окружение, файлы и схемы tools во время копирования.

### `OpenAiTransport`, `AcpServer`, `SessionStream`

OpenAiTransport отправляет готовое тело, разбирает HTTP/SSE, ошибки, usage и ответ.
Обработка входящего ответа провайдера остаётся необходимой. Один завершённый ответ
становится одной AssistantMessageRecord либо RequestOutputRecord.

AcpServer принимает RPC, проверяет capabilities и права, связывает ответы с ожидающими
permission/elicitation и управляет подписками. SessionStream хранит только текущий
незавершённый вывод и рассылает чанки через ACP. Буферы подписчиков ограничены;
медленный клиент не задерживает остальных. Итоговый upsert использует тот же messageId.
Live-таблиц PostgreSQL для токенов нет. Resume согласует сохранённый срез с буфером
текущего потока. После перезапуска доступны только сохранённые сообщения.

## 3. Удаление прежней сборки

- [builder.py](../packages/core/boba-agent/src/boba/agent/builder.py): удалить RequestBuilder,
  Feed и промежуточные UserTurn/AssistantTurn/SystemTurn. Удалить _reordered, _cleaned,
  _repaired, _unique_calls, _known_results, _drop_pending и преобразование tools.
  SQL и валидатор возвращают явную ошибку; исправления истории при сборке нет.
- [records.py](../packages/core/boba-agent/src/boba/agent/records.py): заменить старый набор
  моделей целевыми строками records. Удалить RequestFeed, FeedSink, HistoryFold,
  RequestTexts, RecordCodec, KeptNull и Announced. Удалить у записей to_request, feed,
  announce, carried, summary_request и остальные методы проекций. Модели содержат
  данные и проверки своей формы.
- [session.py](../packages/core/boba-agent/src/boba/agent/session.py): удалить _records,
  _by_uuid, кэш _conversation, назначение seq в Session.add и восстановление окна
  компактизации в памяти. Оставить состояние текущего исполнения.
- [writer.py](../packages/core/boba-agent/src/boba/agent/writer.py): удалить HistoryWriter,
  RecordKeeper, RequestRecorder и производственную двойную сборку RequestReplay.
  Сохранение не проходит через таймер flush; RecordEvent до commit не публикуется.
  ClientCommands/CommandBook заменить производителями CommandAdded/RemovedRecord.
- [queue.py](../packages/core/boba-agent/src/boba/agent/queue.py): удалить InputQueue,
  QueueEntry/QueueItem и QueueOperationRecord из нового пути. Ожидание ввода выражено
  сохранёнными input/admission, а пробуждение исполнителя может быть обычным asyncio.Event.
- [loader.py](../packages/core/boba-agent/src/boba/agent/loader.py): удалить разбор старых
  строк, пропуск повреждённых записей и _repair. Resume не создаёт вымышленные результаты
  tools. Восстановление незавершённого исполнения — отдельное решение исполнителя,
  которое при необходимости создаёт новую запись ошибки/отмены.
- [history.py](../packages/core/boba-agent/src/boba/agent/history.py): заменить StoredLine,
  BrokenLine, StoredHistory и старый контракт хранилища. В новый сервер не включать
  JSONL/Memory как альтернативный источник для сборки SQL.

## 4. Переписывание исполнителей

- [turn.py](../packages/core/boba-agent/src/boba/agent/turn.py): заменить цепочку
  _step → writer.stored → builder.build → RequestRecorder на сохранение входов и границы,
  затем OpenAiHistoryAdapter.build. Переписать _write_reply/_write_results: один ответ
  с упорядоченным content, отдельные ToolResultRecord и TokenUsageRecord. Не создавать
  новые call_id при записи уже полученного вызова. Удалить _absorb и скрытое обновление
  выбранных схем в _record_deferred. Изменение MCP-каталога и tool search создают явные записи.
- [agent.py](../packages/core/boba-agent/src/boba/agent/agent.py): переписать Agent.open,
  serve, AgentParts и Wiring под новые порты. set_model/set_system_prompt сначала
  сохраняют изменения. Удалить сборку HistoryWriter/InputQueue/RecordKeeper/RequestReplay.
- [asking.py](../packages/core/boba-agent/src/boba/agent/asking.py): сохранить отмену,
  накопление текущего ответа и потоковые события. Вход — готовый JSON; частичный ответ
  при отмене сохраняется явно. Чанки не превращаются в строки истории.
- [compaction.py](../packages/core/boba-agent/src/boba/agent/compaction.py): удалить
  CostFeed/to_request, специальную SummaryRequestRecord и сборку через RequestBuilder.
  Summarizer использует RequestContextRecord и общий SQL. Compactor готовит пакет
  CompactMessageRecord и копий; append проверяет expected_seq. Подсчёт токенов остаётся
  отдельной операцией над готовым запросом и фактическим usage, не над старым Feed.
- [permissions.py](../packages/core/boba-agent/src/boba/agent/permissions.py): сохранить
  PermissionPolicy, но читать сохранённые правила. AutoClassifier не вызывает
  SchemaReply с самостоятельно построенным ChatRequest. Классификатор сохраняет
  служебные входы и использует тот же SQL и транспорт.
- [attachments.py](../packages/core/boba-agent/src/boba/agent/attachments.py): сохранить
  необходимый рендер на стороне производителя, заменить старые типы вложений целевыми
  записями. Рендер не вызывается при чтении истории или сборке.
- [tools.py](../packages/core/boba-agent/src/boba/agent/tools.py): сохранить выполнение,
  поиск, MCP, проверки и отмену. Удалить зависимость от Announced и старых snapshot-моделей.
  Результат поиска и ToolSchemaRecord должны быть сохранены до вызова сборщика.
- [skills.py](../packages/core/boba-agent/src/boba/agent/skills.py): активация работает
  с сохранённым описанием и URI ресурса. Автоматическое чтение директорий клиентского
  окружения сервером не входит в новый путь.

## 5. Инфраструктурные замены

### SQL-файлы сервиса

Единственный рабочий набор запросов находится в пакете PostgreSQL-адаптеров агента:

```text
packages/infra/history/boba-history-postgres/src/boba/history/postgres/
    sql/
        lock.sql
        append.sql
        read.sql
        pending_inputs.sql
        history_integrity.sql
        openai.sql
        acp_history.sql
        acp_state.sql
        acp_commands.sql
        acp_options.sql
        acp_notice.sql
        acp_permission.sql
        acp_elicitation.sql
    schema/
        ...sql
```

При переносе добавить `lock.sql` для короткой блокировки записи сессии и `read.sql`
для чтения исходных записей. Остальные рабочие запросы перенести из experiments;
там оставить тестовые данные и нагрузочные сценарии. Проверки выполняют SQL из пакета.

`PostgresHistoryStore`, `OpenAiHistoryAdapter` и `AcpHistoryAdapter` читают свои файлы
через `importlib.resources` при запуске и выполняют сохранённый текст. Параметры
передаются отдельно через `psycopg.AsyncRawCursor`: `$1`, `$2`, `$3` остаются в SQL,
входные значения объявляются в начальном CTE `params`. Шаблонизации и сборки SQL
из Python-строк нет. Схема применяется механизмом миграций отдельно от рабочих запросов.

`sql/*.sql` включается в package-data вместе с `schema/*.sql`. Wheel и сборка Nuitka
сохраняют запросы отдельными UTF-8-файлами. В образе сервиса каталог запросов:
`/app/boba/bin/boba.dist/boba/history/postgres/sql/`. Администратор может открыть
и выполнить тот же файл, который читает сервис; для ручного запуска задаётся CTE params
либо параметры prepared statement. Наличие файлов проверяется `boba self-check`.
Обновлённый набор загружается при перезапуске; SQL и валидаторы поставляются одной версией.

### PostgreSQL

В [store.py](../packages/infra/history/boba-history-postgres/src/boba/history/postgres/store.py)
удалить RecordFrames.split/join, сборку camelCase-строк и on conflict do nothing.
PostgresHistoryStore использует целевые столбцы, append.sql и history_integrity.sql.
Нельзя присваивать seq из held.last_seq или молча отбрасывать часть пачки в _fresh.

Существующие проверки владельца сессии сохраняются. Метаданные владельца и право
исполнять сессию не подменяются наличием строки в records. Длительная блокировка
исполнителя и короткая блокировка записи должны иметь разные назначения.

### HTTP OpenAI

В [openai.py](../packages/infra/llm/boba-llm-http/src/boba/llm/http/openai.py) удалить из
OpenAiChatModel методы _payload, _messages, _message, _content, _data_url, _calls, _choice.
Удалить построение function tools из исходящего пути. Сохранить WireChunk, разбор SSE,
накопление аргументов tool calls, обработку ответа и ошибок. OpenAiEmbeddingModel
к этой замене не относится: целиком удалять файл нельзя.

[endpoint.py](../packages/infra/llm/boba-llm-http/src/boba/llm/http/endpoint.py) уже принимает
JSON для HTTP; выделить отправку готового тела без ChatRequest. Зависимость нового агента
от исходящих ChatRequest/ChatTurn/ToolSpec убрать; общие пакеты других приложений пока не удалять.
Ollama/ONNX не получают обходной Python-сборщик: подключение требует собственного адаптера.

### ACP и запуск сервера

В [acp_server.py](../packages/apps/boba-cli/src/boba/cli/acp_server.py) удалить FeedPainter,
record.feed, _record_updates, _replay_record и ветку replay через HistoryLoader.
Удалить ожидание вставки через RecordEvent и добавление несохранённого Session.since к replay.
Удалить из AcpMapping преобразования сохранённых записей, которые покрывает SQL.

Сохранить входное преобразование ACP в модели агента, MCP-клиент, capabilities,
RPC-корреляцию, cancellation и обработку живого потока. SessionRelay разделить по этим
обязанностям; SQL не заменяет транспорт и исполнение RPC. Удалить использование
_boba/history для восстановления; остальные расширения оценивать по назначению отдельно.
Закреплённую ревизию ACP v2 согласовать с транспортом: установленный SDK отстаёт
в replay_complete/subagent_update; валидаторы этих сообщений уже описаны отдельно.

В [app.py](../packages/apps/boba-cli/src/boba/cli/app.py) заменить AgentAssembly и выбор
старых хранилищ. URL, credentials и лимиты сервера остаются конфигурацией транспорта.
Начальные model/sampling/system prompt/permissions записываются производителями.
Чтение system_prompt_file — получение начального значения, не операция сборщика.

В [threads.py](../packages/apps/boba-cli/src/boba/cli/threads.py) удалить ThreadedHistoryStore
после перевода портов на единый async-путь. Не выделять соединение PostgreSQL каждому
ACP-подписчику. Использовать общий ограниченный пул и порционное чтение истории.

## 6. Порядок перехода

1. Закончить и проверить схему, SQL и валидаторы, включая конкуренцию записи/чтения,
   несколько ожидающих сообщений, служебные запросы и native ACP. Определить версии
   SQL-наборов: повтор старого запроса использует совместимый набор SQL и валидатора.
2. Перенести SQL в файлы пакета, включить их в wheel, Nuitka и self-check. Перенести
   целевые Pydantic-модели в код и подключить порты SQL. Удалить прежнюю сборку и её
   модели; не создавать временный адаптер между двумя форматами истории.
3. Заменить хранение и всех производителей записей. Привести compaction, tool search,
   permissions, задачи и служебные вызовы LLM к одному журналу.
4. Подключить готовый OpenAI JSON к HTTP и готовый ACP JSON к транспорту. Реализовать
   потоковый ACP-адаптер и подписки без live-таблиц. Проверить resume во время генерации.
5. Удалить неиспользуемые зависимости и проверки старого поведения. После завершения
   агента отдельно переделывать boba-aid и других потребителей.

Пользовательские сериализаторы истории в OpenAI/ACP исчезают. Обычная JSON-кодировка,
валидация Pydantic и разбор входящих протоколов остаются. SQL не заменяет сетевой цикл,
исполнение tools и координацию задач. Ускорение оценивается измерением всего пути,
включая JSON и ожидание пула, а не предполагается из самого переноса в PostgreSQL.
