# Рефакторинг boba: история и SQL-адаптеры

Цель — агент с типизированной историей в PostgreSQL и готовыми JSON-результатами SQL
для OpenAI и ACP. Совместимость с boba-aid, старым журналом и остальными потребителями
не входит в этот этап. Промежуточная неработоспособность приложения допустима.

Модели: [история](agent-history-and-request-assembly.md), [ACP](agent-history-to-acp.md).
SQL: [boba.agent/sql](../packages/apps/boba-agent/src/boba/agent/sql).
Изменение схемы выполняется командой `boba migrate` из
[boba.agent/migrations](../packages/apps/boba-agent/src/boba/agent/migrations).
Оставшиеся работы и критерии готовности — в [разделе 7](#7-незавершённые-работы).

## 1. Границы исполнения

```text
ACP / MCP / LLM / команды / фоновые задачи
    → производитель типизированных записей
    → HistoryStore.append
    → append.sql + проверка ссылок + commit
    → records с назначенными seq

records → openai.sql → OpenAiRequestValidator → готовый JSON → HTTP
records → sql/acp/v1/*.sql или sql/acp/v2/*.sql → AcpHistoryValidator → готовый JSON → ACP

дельты текущего ответа → потоковый ACP-адаптер → подписчики сессии
завершённый ответ → HistoryStore.append → records
```

Сборка не читает конфигурацию процесса, часы, файлы, MCP или текущую таблицу задач. Производители получают
эти данные, сохраняют структурные значения и готовый текст. SQL использует сохранённые
данные; транспорт не дописывает model, sampling, stream_options или tool_choice.

Формат OpenAI/ACP для сохранённых записей формируется только SQL-адаптерами.
Python проверяет результат и передаёт готовый JSON, без повторной сборки через модели.
Несохранённые потоковые чанки передаются отдельными Python-моделями ACP;
для чанков нет записи в историю и обращения к PostgreSQL.

Ввод сразу сохраняется как message; изменения skills/memory — в своих категориях.
Assistant со ссылками, отдельные ToolCallRecord и начальные ToolResultRecord сохраняются
вместе с созданием задач одной транзакцией PostgreSQL. SQL размещает полный набор tool
results сразу после assistant; pending/running позволяют продолжать диалог. Отдельные
input/admission, записи принятия/отклонения и хранилище очереди не нужны.
Runtime фиксирует through_seq и курсор обработанного ввода; появившиеся позднее
сообщения входят в следующую сборку. Технические записи попыток состав запроса не меняют. Ввод не переносится и не удаляется из истории при отмене генерации.

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
        operation_id: str | None = None,
        *,
        through_seq: int | None = None,
    ) -> OpenAiRequestDraft:
        """Проверенный результат SQL; для HTTP используются provider и request."""
        ...
```

Срез through_seq фиксируется до отправки. Разовые настройки и служебные входы
уже находятся в records; operation_id выбирает их, но не ограничивает диалог. Пересказ и классификатор разрешений используют этот же адаптер в той же сессии:
AssemblyContextRecord выбирает контекст, RequestMessageRecord содержит собственный ввод,
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
отменой. В памяти остаются текущая операция, её operation_id, незавершённый ответ,
исполняемые задачи и подключения. Полной копии истории для сборки нет.

ToolRunner сохраняет проверку разрешений и фактическое исполнение tools. Каталог
MCP и поиск tools остаются исполнительными компонентами. Полученные определения
сохраняются как ToolSchemaRecord до следующей сборки. Наличие tool в JSON запроса
не является разрешением выполнить tool.

TaskService предоставляет API задач и владеет их таблицей; история агента ему неизвестна.
TaskWorker захватывает и исполняет задания через TaskService. TaskCoordinator связывает
call_id с task_id, вызывает API задач и сохраняет наблюдения в историю. Все три компонента
работают в каждом процессе чатбота; отдельный процесс worker для первого запуска не нужен.
Владение задачей не требует владения диалогом сессии. Контракт и проверки — в разделе 7.2.

ElicitationCoordinator и HistoryCompactor остаются производителями записей.
Компактор переносит сохранённые состояния и закрытый хвост диалога;
не обновляет окружение, файлы и схемы tools во время копирования.

### `OpenAiTransport`, `AcpServer`, `SessionStream`

OpenAiTransport передаёт готовый JSON в `OpenAiBackend.completion().generate` из
`boba-llm-http`. Общий `OpenAiCompletion` отправляет тело без изменений и выдаёт
дельты; `StreamAssembly` сохраняет порядок частей ответа, usage и HTTP-статус.
`Generation` переводит части ответа в модели истории; Agent сохраняет
AssistantMessageRecord либо RequestOutputRecord.
SSE и фрагменты аргументов tools разбираются только в общем LLM-компоненте.

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
  QueueEntry/QueueItem и QueueOperationRecord из нового пути. Ввод сразу находится
  в диалоге; пробуждение исполнителя может быть обычным asyncio.Event.
- [loader.py](../packages/core/boba-agent/src/boba/agent/loader.py): удалить разбор старых
  строк, пропуск повреждённых записей и _repair. Resume не создаёт вымышленные результаты
  tools. Восстановление незавершённого исполнения — отдельное решение исполнителя,
  которое при необходимости создаёт новую запись ошибки/отмены.
- [history.py](../packages/core/boba-agent/src/boba/agent/history.py): заменить StoredLine,
  BrokenLine, StoredHistory и старый контракт хранилища. В новый сервер не включать
  JSONL/Memory как альтернативный источник для сборки SQL.

## 4. Переписывание исполнителей

- [turn.py](../packages/core/boba-agent/src/boba/agent/turn.py): заменить цепочку
  _step → writer.stored → builder.build → RequestRecorder на сохранение входов и фиксацию среза,
  затем OpenAiHistoryAdapter.build. Переписать _write_reply/_write_results: один ответ
  с упорядоченным content, наблюдения выполнения tools по правилам раздела 7.2 и
  TokenUsageRecord. Не создавать
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
  Summarizer использует AssemblyContextRecord и общий SQL. Compactor готовит пакет
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

Рабочие запросы и миграции находятся в едином пакете агента:

```text
packages/apps/boba-agent/src/boba/agent/
    sql/
        lock.sql
        append.sql
        read.sql
        history_integrity.sql
        openai.sql
        acp/
            v1/
                history.sql
                options.sql
                prompt.sql
                commands.sql
                notice.sql
                permission.sql
                elicitation.sql
            v2/
                history.sql
                options.sql
                state.sql
                commands.sql
                notice.sql
                permission.sql
                elicitation.sql
    migrations/
        <version>/
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

`sql/*.sql` включается в package-data вместе с `migrations/*/*.sql`. Wheel и сборка Nuitka
сохраняют запросы отдельными UTF-8-файлами. В образе сервиса каталог запросов:
`/app/boba/bin/boba.dist/boba/agent/sql/`. Администратор может открыть
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
   permissions, наблюдения задач и служебные вызовы LLM к одному журналу. Исполнительное
   состояние задач принадлежит TaskService и хранится независимо от истории агента.
4. Подключить готовый OpenAI JSON к HTTP и готовый ACP JSON к транспорту. Реализовать
   потоковый ACP-адаптер и подписки без live-таблиц. Проверить resume во время генерации.
5. Удалить неиспользуемые зависимости и проверки старого поведения. После завершения
   агента отдельно переделывать boba-aid и других потребителей.

Пользовательские сериализаторы истории в OpenAI/ACP исчезают. Обычная JSON-кодировка,
валидация Pydantic и разбор входящих протоколов остаются. SQL не заменяет сетевой цикл,
исполнение tools и координацию задач. Ускорение оценивается измерением всего пути,
включая JSON и ожидание пула, а не предполагается из самого переноса в PostgreSQL.

## 7. Незавершённые работы

Выполненные пункты отмечаются после проверки. Для каждого пункта сначала уточняются модели истории,
схема, SQL и валидаторы; затем подключаются производители записей, исполнение и ACP.
Изменения схемы проходят только через `boba migrate`, рабочая схема — `boba`.

### 7.0. Перевод рабочего агента на целевую историю

Этот этап завершается до реализации таблицы задач и workers.

- [x] Перенести модели из документа истории: operation_id, operation_position,
  parent_operation_id, применимый scope, ToolCallRecord и единый ToolResultRecord.
  Удалить input/admission, LlmRequestBoundaryRecord и прежние записи состояния задач.
- [x] Добавить целевую схему records в migrations; обновить append/read/latest,
  проверки ссылок, fork, копирование при компактизации и курсор обработки runtime.
- [x] Подключить проверенный openai.sql и общий валидатор к пакету агента.
  Сборка принимает session_id, operation_id и through_seq; не зависит от попыток HTTP.
- [x] Обновить отдельные SQL-наборы ACP v1 и v2: вызов из ToolCallRecord,
  обновления из ToolResultRecord, точный источник source_record_id, настройки и usage.
  Потоковые чанки остаются отдельными Python-моделями.
- [x] Перевести существующих производителей: пользовательский ввод, ответы и попытки LLM,
  настройки, команды, permissions/elicitation, компактизацию и сабагентов.
  Составляющие операции сохраняются атомарно; Python не формирует сохранённые OpenAI/ACP сообщения.
- [x] Проверить модели, SQL и публичные пути обеих версий ACP на изолированной БД;
  несколько входящих сообщений, повтор append, срезы, компактизацию и ошибочные ссылки.
  Для ожидающей пачки — одна генерация с operation_id последнего пользовательского
  сообщения: локальные настройки только этой операции, остальное из session.

Проверено на изолированной БД `aid_ss_agent_operations`: интеграционные проверки
OpenAI, ACP v1/v2 и runtime проходят; Pyright — без ошибок. Миграция `0006`
не применялась к рабочей БД. Индекс `records_tool_calls` сократил полный ACP replay
50 000 строк примерно с 19 до 2–2,4 секунды, включая получение JSON и валидацию.
Это измерение одного чтения, не проверка конкурентного исполнения задач.

Исполнение `Agent.task` пока остаётся в памяти процесса; таблицы задач,
захвата worker и LISTEN/NOTIFY ещё нет. Далее выполняется раздел 7.2. API задач принимает задания разных производителей;
operation_id, call_id и структура LLM не являются обязательными полями задания.
Связь задания с историей задаёт TaskCoordinator, а не TaskService или TaskWorker.

### 7.1. Skills, memory и определения агентов

Уточняет производителей контекста из раздела 4.

- [x] Проверить передачу контекста и resume в реализациях Zed, T3 Code,
  Claude ACP и Codex ACP. Модели истории не требуют одноимённых методов ACP.
- [x] Использовать стандартные API SDK без preview `systemPrompt` и собственных
  обработчиков согласования. Системные инструкции сохраняются из профиля агента;
  клиентский текст и ресурсы приходят через `session/prompt` и сохраняют роль user.
- [x] Проверить готовые стандарты: Agent Skills задаёт формат `SKILL.md`, ACP —
  контент и вызов объявленных команд; MCP Skills (`io.modelcontextprotocol/skills`,
  SEP-2640) — удалённый каталог и чтение навыков. Спецификация расширения имеет
  статус Beta; принятие SEP не означает поддержку расширения каждым клиентом.
- [ ] Принимать `text`, `resource` и `resource_link` через `session/prompt`;
  сохранять URI и переданное содержимое как пользовательский контент.
  Само вложение ресурса не означает регистрацию или активацию skill.
- [ ] Сопоставить готовые контракты ACP/MCP с моделями истории. Для удалённых skills
  проверить поддержку MCP Skills закреплённым SDK и источником: согласование
  capabilities, `skills/list`, `skills/get`, `resources/read`. Активацию фиксирует
  обработчик загрузки skill; обычное чтение ресурса активацией не считается.
  Отсутствие skill в частичном каталоге не означает удаление.
- [ ] Для memory и определений агентов использовать доступные MCP tools/resources
  и объявленные команды агента. Схемы tools задают семантику операций; сопоставление
  с историей выполняет их обработчик. Отдельный CRUD API через ACP не требуется.
- [ ] Подключить `SkillAddedRecord`, `SkillRemovedRecord`, `SkillActivatedRecord`,
  `MemoryAddedRecord`, `MemoryRemovedRecord`, `AgentAddedRecord`, `AgentRemovedRecord`
  к обработчикам этих операций. Сохранять исходный URI и полученный контент до
  использования в LLM. Недостающее содержимое получает обработчик через доступный
  источник. Сборщик использует только историю; сервер не сканирует файлы клиента.
- [ ] Обеспечить выбор последней действующей записи по ключу, удаление контекста
  и перенос постоянного состояния при компактизации. Повторная сборка использует
  сохранённый контент, включая прежнюю версию изменившегося внешнего ресурса.

Готовность: регистрация, активация, удаление и resume проходят через публичный API;
исторический LLM-запрос воспроизводится после изменения исходного ресурса.

Стандарты: [Agent Skills](https://agentskills.io/specification),
[MCP Skills, Beta](https://skills.extensions.modelcontextprotocol.io/specification/beta/skills),
[команды ACP](https://agentclientprotocol.com/protocol/v2/slash-commands).

Проверенные реализации: [Zed: блоки вложений](https://github.com/zed-industries/zed/blob/56a157e5b01b62afd5c50a014ed15b379de8dfd6/crates/agent_ui/src/message_editor.rs#L2200),
[Claude: входящий контекст](https://github.com/agentclientprotocol/claude-agent-acp/blob/b1c198754d1cb4e086de6c6bc6a9659ae3351a0a/src/prompt-to-claude.ts),
[Codex: каталог skills в командах](https://github.com/agentclientprotocol/codex-acp/blob/656765115f82253c79f87918abb77189c114a799/src/CodexCommands.ts#L191).
В этих адаптерах skills и правила также загружаются самим агентом из его среды;
наличие интерфейса skills в клиенте не гарантирует передачу полного каталога по ACP.

### 7.2. Независимое исполнение задач

**Первый этап реализован:** миграция `0007` добавляет `boba.tasks`; независимый
компонент [boba.tasks](../packages/apps/boba-agent/src/boba/tasks) предоставляет
TaskService, TaskWorker, TaskExecutor и TaskNotifications. Worker проверен без
Agent, LLM, ACP и records, включая отдельные процессы и потерю исполнителя.
**TaskCoordinator и встроенный агентский task_status ещё не подключены.**
Текущий Agent.task пока не использует эту таблицу; это следующие пункты интеграции.

```text
Агент / планировщик → TaskService.add → boba.tasks
TaskWorker → TaskService.claim → TaskExecutor.execute → TaskService.finish
TaskWorker: цикл обслуживания → TaskService.expire → boba.tasks
изменение tasks + pg_notify → commit
TaskNotifications → TaskCoordinator → TaskService.status → records
```

Добавление не вызывает исполнителя. Worker не знает производителя и не читает историю.
Агент использует добавление, чтение и подписку; выдача заданий и обновление состояния —
контракт worker. Оба компонента могут работать в одном процессе, сохраняя эти границы.

#### 7.2.1. Компоненты и данные

- [x] **TaskService**: единственная реализация SQL-операций над `boba.tasks`.
  Методы производителей: `add`, `status`; методы исполнителя: `claim`, `renew`,
  `progress`, `finish`, `expire`. Создание и чтение проверяют пользователя;
  операции исполнения проверяют полномочия worker и токен владения.
  `add` принимает заранее созданный task_id: одинаковая повторная команда возвращает
  прежнюю задачу, другое содержимое с тем же ID возвращает конфликт. Worker не вызывается.
- [x] **TaskWorker**: цикл захвата, ограничение параллелизма, heartbeat, исполнение,
  завершение и отдельный цикл обслуживания просроченных задач. Методы: `run`, `stop`,
  `run_maintenance`. В конструкторе — TaskService, TaskExecutor и WorkerConfig.
  TaskExecutor требуется только в режиме исполнения. Цикл обслуживания не ждёт
  свободного исполнительного слота или завершения tool.
- [x] **TaskExecutor**: интерфейс `execute(task, execution_context) -> TaskOutcome`.
  Выполняет действие, возвращает результат; через execution_context сообщает прогресс
  и получает сигнал отмены. Не меняет SQL и историю. Выбор обработчика по task.kind
  находится здесь; MCP, сабагент и задание планировщика проходят один жизненный цикл.
- [x] **TaskNotifications**: `start`, `subscribe(task_id, handler)`,
  `subscribe_ready(handler)`, `close`.
  Одно выделенное LISTEN-соединение на процесс, переподключение и сигнал о необходимости
  сверки. Уведомление не является снимком задачи; обработчик вызывает `status`.
- [ ] **TaskCoordinator**: адаптер агента к TaskService, единственный компонент этого
  пути, который знает одновременно task_id и историю. `dispatch`, `observe`,
  `answer_status`, `reconcile` создают записи согласно документу истории.

Модели API: `TaskCreate`, `TaskState`, `TaskOutcome`, `TaskError`, `TaskChanged`.
Внутренние значения: неизменяемые `TaskLease` и `WorkerIdentity`. TaskState содержит
публичное состояние; lease_token выдаётся только исполнителю в TaskLease. Компонент
задач не импортирует модели истории; TaskCoordinator создаёт TaskSnapshot истории
из прочитанного TaskState.

Поля одной строки `boba.tasks`:

```text
task_id                 — идентификатор исполнения и ключ идемпотентного добавления
owner_id                — пользователь, имеющий доступ к задаче
kind                    — тип исполняемого действия
route_key               — требуемый ресурс исполнения, null для общего пула
payload                 — проверенные аргументы действия, jsonb
status                  — pending / running / completed / failed / cancelled / interrupted
revision                — версия публичного состояния задачи
created_at              — время добавления по часам PostgreSQL
started_at              — время захвата, null до запуска
finished_at             — время терминального перехода, null до завершения
run_timeout_sec         — максимальная длительность одного исполнения
execution_deadline_at   — started_at + run_timeout_sec, null до захвата
worker_id               — текущий или последний исполнитель, null до захвата
lease_token             — токен захвата, null до захвата
lease_expires_at        — срок владения, null до захвата
progress                — необязательные данные прогресса, jsonb
result                  — итог действия, jsonb; null до получения результата
error                   — code/message/details, jsonb; null при отсутствии ошибки
```

В таблице нет обязательных session_id, operation_id и call_id. Payload не содержит
объекты Python или функции: задача должна читаться другим процессом. `route_key`
описывает доступ к ресурсу, а не разновидность жизненного цикла. Worker захватывает
только поддерживаемые kind и доступные маршруты с разрешёнными полномочиями.
Для MCP-over-ACP требуется определить предоставление доступа к клиентскому каналу;
автоматической доступности этого канала другому процессу пока нет. Это отдельная
проверка интеграции, а не исключение из правил исполнения задач.

#### 7.2.2. Захват, протухание и восстановление

Предлагаемые переходы:

```text
pending → running → completed
                  → failed        исполнитель получил определённую ошибку
                  → cancelled     отмена исполнения подтверждена
                  → interrupted   исполнение потеряно либо итог неизвестен
```

- [x] `claim.sql` выбирает подходящие pending-строки через `for update skip locked`
  и в той же короткой транзакции устанавливает running, worker_id, новый lease_token,
  started_at, execution_deadline_at и lease_expires_at. Число захватов не больше числа
  свободных слотов. Сначала commit, затем внешний вызов. Порядок выбора — created_at,
  task_id; занятые строки пропускаются, строгая очередность завершения не обещается.
- [x] Worker продлевает lease отдельной короткой транзакцией. Все изменения исполнения
  проверяют **в том же UPDATE**: task_id, worker_id, lease_token, status=running,
  неистёкшие lease_expires_at и execution_deadline_at. Время берётся из PostgreSQL.
  Продление не превышает execution_deadline_at. Истёкший lease нельзя оживить heartbeat.
- [x] `expire.sql` пачкой выбирает просроченные running-задачи через `skip locked` и
  переводит в interrupted. Ошибка `lease_expired` означает потерю подтверждения worker;
  `execution_timeout` — превышение общего времени исполнения. Записываются finished_at,
  error, новая revision и уведомление. Два обслуживающих процесса не завершают задачу дважды.
- [x] Стартовые значения WorkerConfig для проверки: lease — 60 секунд, heartbeat —
  каждые 15 секунд, обслуживание — каждые 5 секунд. Это настройки, не SQL-константы.
  run_timeout_sec сохраняется при добавлении задачи и ограничивает зависший tool даже
  при исправном heartbeat. Pending-задача lease ещё не имеет; timeout выполнения
  отсчитывается от захвата, а срок ожидания запуска в первую версию не включается.
- [x] После потери lease worker прекращает подтверждение результата и пытается отменить
  внешнее исполнение. Запоздавший результат отвергается независимо от того, успел ли
  цикл обслуживания записать interrupted. Токен защищает запись в БД; он сам по себе
  не останавливает уже отправленную операцию на внешнем MCP-сервере.
- [x] Терминальная задача не возвращается в pending. Автоматически повторяется связь
  с БД и сверка commit, но не внешнее действие с неизвестным итогом. Новый запуск —
  новая задача с новым task_id. Для будущего автоматического повторения нужна явная
  гарантия идемпотентности конкретного исполнителя; одного lease для этого недостаточно.
- [x] При неизвестном исходе claim сверять сохранённый токен захвата; при неизвестном
  исходе finish читать строку и сравнивать исход. Повтор записи того же исхода безопасен,
  конфликт виден явно. Повтор finish не вызывает TaskExecutor ещё раз.
- [x] При запуске процесса сначала обработать истёкшие leases, затем брать pending.
  На штатной остановке прекратить захваты и ограниченно ждать/отменять активные действия.
  cancelled допустим при подтверждённой отмене; при неизвестном исходе — interrupted.

**Кто обновляет строку, если worker умер:** `run_maintenance` выполняется отдельным
циклом любого живого экземпляра. Предусмотреть отдельный запуск этого же компонента
в режиме обслуживания без исполнителей. Если остановлены вообще все процессы
обслуживания, срок lease всё равно истекает и права worker теряются, но запись
interrupted и NOTIFY появятся только после возобновления обслуживания. Для обновления
таблицы при остановленных чатботах нужен работающий отдельный процесс обслуживания.
`task_status` остаётся чтением сохранённого состояния и не подменяет этот процесс.

#### 7.2.3. SQL, индексы и уведомления

- [x] Добавить таблицу только новой миграцией `boba migrate` в схеме `boba`.
  Критичные условия строки закрепить ограничениями: согласованность статуса, времён
  и полей владения, положительные timeout/revision. Начальная revision=1.
- [x] Рабочие SQL хранить отдельными файлами компонента: `add.sql`, `status.sql`,
  `claim.sql`, `renew.sql`, `progress.sql`, `finish.sql`, `expire.sql`.
  Параметры — одним CTE, явные столбцы, нижний регистр, принятой формы отступы.
  Захват и обслуживание не требуют временных таблиц, представлений или блокировки
  всей очереди. Для ошибок неизвестного commit добавить точечный поиск по lease_token.
- [x] Проверить индексы планами: PK task_id, частичный индекс выбора pending по
  kind/route_key/created_at/task_id и отдельный индекс очередности created_at/task_id,
  частичные индексы running по lease_expires_at
  и execution_deadline_at, индекс сверки lease_token. Owner-фильтрация обязательна
  для клиентских чтений; необходимость дополнительных индексов подтвердить нагрузкой.
- [x] Изменение публичного состояния увеличивает revision и выполняет `pg_notify`
  в той же транзакции. Heartbeat меняет только lease, не revision и не поток уведомлений.
  Каналы `boba_task_ready` и `boba_task_changed` разделяют пробуждение исполнителей
  и наблюдение статусов; одно соединение слушает оба. Добавление pending уведомляет
  оба канала, остальные переходы — changed. Payload содержит task_id и revision
  без результатов/секретов. Rollback не должен доставлять уведомление.
- [x] Worker проверяет таблицу при старте, по уведомлению о новых заданиях и периодически.
  Подписчик сначала подтверждает LISTEN, регистрирует task_id и читает статус; далее
  обрабатывает уведомления. При подключении после завершения достаточно текущей строки.
  Реконнект и периодическая сверка восстанавливают состояние при потерянном NOTIFY.
  Доставка каждой промежуточной revision не гарантируется и не требуется.
- [ ] Использовать `AsyncPostgresPool` и принципы существующих
  [LiveListener](../packages/infra/db/boba-db-postgres/src/boba/db/postgres/messaging/bus.py)
  и [PgLiveLocks](../packages/infra/auth/boba-identity-postgres/src/boba/identity_postgres/locks.py):
  выделенное соединение, реконнект, часы БД, проверка токена в UPDATE.
  Существующий LiveListener привязан к каналу и Pointer старой шины; не подключать
  задачи к live_events/live_commands/live_locks. Общие компоненты без согласования
  не менять. Проверить выделение переиспользуемого цикла отдельно от старого контракта.

Технические основания: [SKIP LOCKED](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE),
[порядок LISTEN и первого чтения](https://www.postgresql.org/docs/17/sql-listen.html#SQL-LISTEN-NOTES),
[доставка NOTIFY после commit](https://www.postgresql.org/docs/17/sql-notify.html#SQL-NOTIFY-DESCRIPTION).

#### 7.2.4. Порядок реализации и критерии приёмки

1. [x] Закрепить модели задач, ошибки API, WorkerConfig и TaskExecutor. Реализовать
   таблицу и SQL TaskService, затем самостоятельный TaskWorker и цикл обслуживания.
   На этом этапе реальные тесты добавляют задания без агента и без записи в records.
2. [x] Проверить несколько отдельных процессов worker: одно задание исполняется одним
   владельцем; параллельные задачи не удерживают SQL-транзакции; создание заданий
   продолжается при остановленных workers. Проверить права разных пользователей.
3. [ ] Проверить протухание: убить процесс после claim и во время действия; остановить
   heartbeat; задержать старый worker до истечения lease; оборвать БД и результат commit.
   Живой процесс обслуживания записывает interrupted, старый токен не обновляет строку,
   повторного внешнего запуска нет. Проверить гонки finish/expire и renew/expire,
   зависание tool при продолжающемся heartbeat и перезапуск всех процессов.
4. [x] Подключить TaskNotifications и проверить rollback, завершение до подписки,
   пропуск уведомления, разрыв LISTEN и повторную сверку. Проверить отдельный процесс
   обслуживания при остановленных исполнителях. Чтение task_status не меняет задачу.
5. [ ] Только после этого подключить TaskCoordinator: атомарно сохранить assistant,
   ToolCallRecord, начальные ToolResultRecord и созданные задачи. Координатор передаёт
   общий транзакционный контекст; TaskService не знает records. Автоматически наблюдать
   созданные задачи; восстановить подписки из истории при открытии сессии.
6. [ ] Добавить встроенный `task_status`. Каждое успешное чтение сохраняет новый снимок
   в историю, включая ту же revision. Отдельный вызов опроса получает собственный
   tool result; source_record_id закрепляет прочитанный снимок. Агент не обновляет tasks.
   Терминальное наблюдение может пробудить LLM; pending/running сами генерацию не запускают.
7. [ ] Подключить MCP и сабагентов через TaskExecutor, удалить исполнение из Agent.task.
   Проверить одинаковый путь заданий агента и отдельного производителя-планировщика,
   доступ к MCP-over-ACP, отмену и потерю клиентского канала. Все протокольные сообщения
   из сохранённой истории продолжает формировать SQL; чанки ACP не пишутся в PostgreSQL.
8. [ ] Нагрузить одновременные add/claim/renew/finish/status и сборку OpenAI/ACP с
   1/4/16 процессами worker и 64/256/512 клиентами. Измерить задержки p50/p95/p99,
   throughput, ожидания блокировок, пул, истечение leases, рост таблицы и индексов.
   По EXPLAIN проверить выбор задач и обслуживание на большой очереди без tmp-таблиц.
   Итоговые проверки — изолированная БД, реальные процессы, Pyright без ошибок.

Готовность первого результата: worker исполняет задания без Agent и истории, другой
процесс завершает просроченные задачи, устаревший worker не может записать результат.
Готовность интеграции: агент и планировщик используют один API; LLM-запрос воспроизводится
из сохранённой истории независимо от текущего состояния таблицы tasks.

Проверки первого этапа: [test_task_service.py](../packages/apps/boba-agent/tests/test_task_service.py)
и отдельные процессы из [task_process.py](../packages/apps/boba-agent/tests/task_process.py).
Проверены 11 сценариев; общий набор агента — 117 интеграционных тестов, Pyright без ошибок.
Wheel собран и проверен: модули, SQL и миграция входят в пакет.
Миграция применена только к `aid_ss_agent_operations`. В launch.json добавлен target
`Boba tests: независимый worker задач`.

Отдельное обслуживание с конфигурацией сервиса boba-agent:
`boba tasks-maintain --config <config.toml>`. В конфигурации выбирается нужная БД.
Просмотр рабочего SQL: `boba sql tasks/claim`, `boba sql tasks/expire`.
Длительности lease, heartbeat и обслуживания задаются в `[agent.settings.tasks]`.

Смешанная проверка (короткие прогоны по 6 секунд) использовала 1/4/16 процессов worker
и 64/256/512 клиентов add/status/OpenAI/ACP. Ошибок и истёкших активных leases не обнаружено.
При 16 workers / 512 клиентах: около 121 add/с, 127 status/с, 144 OpenAI-сборки/с и
144 ACP-сборки/с; p95 около 0,9–1,1 с. За прогон завершено 350 задач, 468 ожидали захвата.
Это проверка конкуренции, не подтверждение устойчивой пропускной способности:
поступление задач превышало скорость исполнения, очередь росла. Пулы ограничены:
24 соединения клиента и по 2 на процесс worker. Длительная нагрузка и интеграция
полного агентского пути остаются пунктом 8.

На таблице из 50 000 pending и 50 000 running задач проверен EXPLAIN ANALYZE:
захват — около 0,23 мс через `tasks_pending_order`; проверка без просроченных задач —
около 0,15 мс через индексы сроков. Параметры SQL объявлены `not materialized`, чтобы
планировщик видел условия и размер пачки. Эти времена относятся к выполнению SQL,
без ожидания пула, сетевой доставки и Python-валидации.

### 7.3. Slash-команды всех владельцев

- [ ] Подключить регистрацию и удаление команд к `CommandAddedRecord` и
  `CommandRemovedRecord`; доступный каталог восстанавливать из истории.
- [ ] Описать и реализовать маршруты `agent`, `client`, `mcp_prompt`, `mcp_app`
  по `SlashCommandOwner`. Для клиентского обработчика и MCP App сначала определить
  поддерживаемый контракт ACP/MCP и необходимые capabilities.
- [ ] Сохранять `SlashCommandRecord` до исполнения, затем
  `SlashCommandResultRecord` с результатом, отменой или ошибкой. Результат,
  необходимый модели, отдельно сохранять как вход контекста до следующей сборки.

Готовность: проверены все владельцы команд, исчезновение команды, отказ исполнителя
и отмена; replay показывает результат без повторного исполнения команды.

### 7.4. Компактизация и применяемые настройки

Расширяет компактизацию из раздела 4.

- [ ] Определить автоматический запуск по размеру готового запроса и реакцию на
  переполнение контекста. Пересказ выполнять служебным запросом в той же сессии
  через сохранённый `AssemblyContextRecord` и общий `openai.sql`.
- [ ] Координировать пересказ с текущими LLM/tool-сообщениями и продолжать сохранять
  поступающий ввод. Записывать `CompactMessageRecord` и копии состояния одной
  транзакцией после успешного пересказа; при ошибке сохранять прежний контекст.
- [ ] Проверить перенос постоянного контекста, принятого и ожидающего ввода.
- [ ] Сохранить наблюдение незавершённых задач при компактизации: подписка переживает
  смену контекста, поздний итог попадает в новую историю без результата с потерянным
  исходным tool call. До реализации определить перенос группы или отдельное сообщение итога.
- [x] Исправить перенос выбранных схем в `compact_state.sql`: повторный `tool_added`
  сохраняет схему до фактического `tool_removed`. Проверены повторная компактизация,
  удаление с возвращением инструмента и воспроизведение прежнего запроса.
- [ ] Разобрать `context_window`, `max_output_tokens`, `reasoning_return`,
  `tool_result_max_chars`, `tool_result_window_share`, `always_load`.
  Для поддерживаемых настроек определить производителя и место применения;
  устаревшие и дублирующие поля удалить. Параметры LLM сохранять до границы запроса,
  ограничения результатов применять до записи готового содержимого в историю.

Готовность: ручная и автоматическая компактизация, переполнение контекста и неудачный
пересказ имеют проверенные сценарии; все оставленные настройки действительно применяются.

### 7.5. Elicitation и ошибки разрешений

- [x] Подключить общий путь вопросов от агента и MCP без участия LLM:
  сохранение `ElicitationRequestRecord`, отправка через SQL выбранной версии ACP,
  приём ответа и сохранение `ElicitationResponseRecord`. Обратный `mcp/message`
  проверяет принадлежность MCP-подключения и связывает вопрос с активным tool call.
  Поддерживаются `ping`, `roots/list`, `elicitation/create`; MCP capabilities объявляют
  roots и доступные у клиента режимы elicitation. Sampling не объявляется.
- [ ] Проверять принятый ответ формы по `requested_schema` до сохранения результата.
  Для URL запретить содержимое формы и отделить согласие открыть URL от фактического
  завершения внешнего действия; завершение фиксировать `ElicitationCompletedRecord`.
  Credentials остаются во внешнем процессе авторизации.
- [ ] Обработать отмену, отключение клиента, неподдерживаемые capabilities и повторный
  ответ. Resume не должен заново открывать завершённые вопросы и внешние действия.
- [x] Убрать молчаливый `except Exception: return "deny"` у классификатора.
  Проверять формат решения; при сбое сохранять причину, уведомление и явный отказ.

Готовность: проверены form/URL, ответы и отмены без LLM, завершение URL-действия,
невалидная форма и недоступный классификатор; ошибки видны через ACP и в истории.

### 7.6. Восстановление исполнения и частичный resume

- [ ] Определить восстановление запросов, tools, сабагентов и фоновых задач,
  прерванных завершением процесса. Различать подтверждённую ошибку и неизвестный
  исход внешней операции; при необходимости уточнить модели состояния и валидаторы.
- [ ] Реализовать восстановление в исполнителе: использовать сохранённые ID,
  фиксировать обнаруженное прерывание новой записью. Не повторять внешнюю операцию
  автоматически и не создавать вымышленный результат внутри сборщика.
- [ ] Повтор записи после неизвестного исхода commit выполнять с теми же ID
  и содержимым. Разделить ошибку сохранения и ошибку ACP-публикации уже сохранённой
  записи.
- [x] Освобождать владение сессией при неудачном открытии. Проверен реальный отказ
  записи PostgreSQL и отсутствие оставшейся advisory-блокировки процесса.
- [x] Исправить стандартный resume: без `replayFrom` возвращать текущее состояние
  и живой поток без старой истории; с `{"type":"start"}` отправить историю,
  `replay_complete` и текущее состояние до ответа на RPC. При `session/new`
  не отправлять `replay_complete`. Согласовать срез с текущими чанками и сохранить
  `messageId`. Неподдерживаемый курсор возвращает `-32602`.
- [ ] До реализации частичного resume найти поддерживаемый клиентами контракт.
  В проверенных [Zed](https://github.com/zed-industries/zed/blob/56a157e5b01b62afd5c50a014ed15b379de8dfd6/crates/agent_servers/src/acp/v2.rs#L1054),
  [T3 Code](https://github.com/pingdotgg/t3code/blob/7c52ef7401c23baf8c48db97446c3ef0f721033f/packages/effect-acp/src/client.ts#L1146),
  [Claude ACP](https://github.com/agentclientprotocol/claude-agent-acp/blob/b1c198754d1cb4e086de6c6bc6a9659ae3351a0a/src/v2/agent.ts#L438)
  и Codex ACP используется полный replay через `start` либо resume без replay.
  Собственный курсор пока не вводить.

Готовность: проверены перезапуск во время LLM/tool/task, потеря соединения с БД,
неизвестный исход commit, конкуренция владельцев и resume по курсору.

### 7.7. Совместимость SQL и поставка

Детализирует версионирование и упаковку из разделов 5–6.

- [ ] Определить, как история фиксирует версию OpenAI-адаптера и валидатора
  отправленного запроса. Версию агента в общем поле `version` сохранить;
  необходимые сведения адаптера отдельно описать в моделях до изменения схемы.
- [ ] Организовать выбор совместимого набора SQL/валидатора для старой границы
  запроса. Явно отклонять неподдерживаемую версию; не собирать старый запрос
  изменившимися правилами без проверки совместимости.
- [ ] Проверить wheel и Nuitka: рабочие SQL доступны отдельными файлами,
  миграции находятся только в `migrations`, `boba self-check` проверяет ресурсы.
  Удалить оставшиеся зависимости от снятых пакетов хранения и прежней сборки.

Готовность: запрос воспроизводится после обновления агента; комплект поставки содержит
те же SQL, которые доступны администратору, и проходит миграции и self-check.

### 7.8. Проверка сервиса и актуализация документов

- [x] Зафиксировать порядок версий: сначала завершить и проверить ACP v2 с внешним
  клиентом, затем приступать к v1. Zed включает v2 через feature flag;
  для первой проверки используется клиентский модуль T3 Code.
- [x] Добавить согласование ACP v1/v2 на подключении с общей историей и исполнителем.
  `AgentProtocolRouter` SDK выбирает `AcpV1` либо `AcpV2` один раз при initialize.
  Сборка подключения находится в `acp_connection.py`; общие операции не импортируют
  реализации протоколов. SQL разнесены по `sql/acp/v1/` и `sql/acp/v2/`.
  Python передаёт несохранённые чанки.
  Для v1 проверить завершение prompt, отмену, tools, replay после перезапуска и
  одновременное наблюдение несколькими клиентами выбранной версии.
  `/acp/v1` ограничивает согласование v1.
- [x] Разделить тесты в `tests/acp_v1/` и `tests/acp_v2/`: собственные клиенты,
  SDK-модели, сценарии, SQL и WebSocket-проверки; без импорта соседнего набора
  и без параметра выбора версии. Общий стенд содержит PostgreSQL, инференс,
  MCP-сервер и управление ресурсами. Согласование версии проверяется отдельно
  в `test_acp_negotiation.py`. Для каждого набора есть отдельный target в `launch.json`.
- [x] Проверить в интерфейсе T3 отдельные подключения Boba ACP v1 и Boba ACP v2:
  по два последовательных запроса, показ ответов и восстановление после обновления
  страницы без дублирования. Авторизация — OAuth, подключение T3 — локальный мост
  stdio ↔ WebSocket; прямой HTTP-клиент в этой проверке не используется.
- [x] Подключить неизменённый `effect-acp` из T3 Code к штатному WebSocket-входу:
  initialize v2, новый чат, два подписчика на чанки одного ответа, сохранённый
  ресурс с URI и полный replay третьим подключением до ответа на RPC. Использованы
  PostgreSQL и тестовый HTTP-инференс. Дополнительно проверен браузерный T3
  с OAuth и настроенным внешним инференсом.
- [x] Поддержать две редакции MCP-over-ACP через `McpPeer`, без собственного `_meta`.
  Успешный `mcp/connect` подтверждает `connectionId`; далее версия и capabilities
  проверяются через `initialize`. Только ответ `Method not found` на `mcp/connect`
  разрешает проверку варианта `serverId/requestId` через `server/discover`.
  `tools/list` и `tools/call` выполняются после подтверждения возможностей.
  Ошибки авторизации, таймауты и неверные ответы не выбирают другой транспорт.
  Современные запросы передают стандартные MCP version/clientCapabilities/clientInfo.
  Проверены обе редакции; T3 Code прошёл `tool_search → tools/call` через
  старую редакцию. [Контракт новой редакции](https://agentclientprotocol.github.io/rust-sdk/migration-stateless-mcp.html),
  [согласование MCP](https://go.sdk.modelcontextprotocol.io/protocol/).
- [ ] Проверить запущенный HTTP/WebSocket-сервис с авторизацией, несколькими
  подписчиками, медленным клиентом, отключением и resume. Отдельно проверить
  несколько процессов агента и потерю соединения, удерживающего владение сессией.
- [ ] Повторить нагрузочные сценарии полного пути: разные сессии и общая сессия,
  параллельные записи, OpenAI/ACP-сборки, подписки и компактизация. Измерить ожидание
  пула, блокировок, задержки и ошибки; проверить отсутствие временных таблиц.
- [ ] Синхронизировать разделы 2–5 и документы моделей с единым `boba-agent`,
  фактическими классами и путями. Удалить устаревшие ссылки и невостребованные
  промежуточные компоненты. Привести затронутый код к `AGENTS.md`.

Готовность: все пункты 7.1–7.7 подтверждены сценариями через публичные интерфейсы,
проверки типов проходят для кода и тестов, а документы соответствуют реализации.

### 7.9. Управление сессиями и подключениями ACP

- [x] `session/delete`: сохранять `SessionDeletedRecord`, останавливать выполнение
  и освобождать ресурсы. Удалённый чат не попадает в список и не открывается;
  история сохраняется. Повтор подтверждённой записи возвращает прежнюю квитанцию.
- [x] `session/fork`: копировать завершённую историю SQL-запросом в независимую
  сессию; сохранять `SessionForkedRecord`. Сохранённые запросы воспроизводятся
  с прежними ID. Работающие сессии, ожидающий ввод и незавершённые задачи
  отклоняются явно. Ошибка подключения новой ветки закрывает созданную сессию.
- [x] `providers/list`, `providers/set`, `providers/disable`: единая таблица
  `boba.providers` содержит только личные подключения с обязательным `owner_id`.
  Новый пользователь начинает с пустого списка и задаёт произвольный `provider_id`,
  адрес и заголовки через ACP. Настройки переживают перезапуск.
- [x] Выбирать личное подключение параметром `provider` независимо от профиля модели.
  Первое подключение заполняет выбор в чатах без провайдера; последующие добавления
  не меняют выбранное подключение. Отключение запрещает отправку запроса.
- [x] Использовать `SecretCipher` и `ValueCipherConfig` для хранения секретов.
  SQL собирает `providers/list` без credentials. HTTP и streaming выполняют общие
  компоненты; адаптер агента добавляет заголовки в `HttpRequest`. Отдельные попытки
  LLM-вызова координирует агент и фиксирует в истории.
  Общие библиотеки для этого не изменяются.
- [x] Связать `session/set_mode` v1 и параметр `mode` v2 с `PermissionStateRecord`.
  Смена режима сохраняет правила инструментов, обновляет подписчиков и отражается
  при восстановлении. `bypass_permissions` предлагается только при `allow_bypass`.

Для хранения credentials обязателен `[agent.providers].cipher`.
Поддерживается API `openai`; имена личных провайдеров не связаны с серверными профилями моделей.
Отдельные ACP login/logout не объявляются: HTTP-вход проверяет Bearer до initialize.
Редакторские уведомления `did_*` и NES находятся вне этого этапа.
