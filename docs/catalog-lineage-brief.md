# Каталог данных: задание исполнителю

Это рабочее задание для агента, который реализует план
`docs/catalog-lineage-plan.md`. План описывает «что и зачем», здесь «как,
где и в каком порядке». План первичен: при расхождении править это задание,
а не отступать от плана молча.

## 1. Правила, без которых работа не принимается

1. Прочитать `CLAUDE.md` в корне репозитория до первой строки кода. Ключевое:
   pyright без ошибок и по коду, и по тестам; данные только моделями
   pydantic; валидация на границе; контракт ошибок в docstring модуля;
   минимум `None`; никаких тернарников, `or`-дефолтов и inline-циклов в
   аргументах; группы констант только `StrEnum`/`IntEnum`; сообщения
   пользователю и в лог по-английски, docstring и комментарии по-русски;
   реализация `Protocol` наследует его явно; никаких `TYPE_CHECKING`-импортов;
   конфиги без комментариев.
2. Проверка типов после каждого цикла правок:
   `/app/docker/compose/boba/.venv/bin/pyright <изменённые пакеты>`.
   Итоговый прогон по всему `packages` обязан быть чистым.
3. Тесты интеграционные, на реальном Postgres стенда, без моков. Маркер
   `integration` для тестов с базой, `ui` для браузерных. Запуск из каталога
   `compose/chainlit`:
   `cd compose/chainlit && ../../.venv/bin/pytest <путь> -m integration`.
   Чистые доменные тесты (этап 1) маркера не требуют.
4. Никаких shim'ов совместимости, никаких «залогировали и поехали дальше».
5. Коммиты только в ветку `feature/catalog_lineage`. По завершении каждого
   этапа: остановиться, дать отчёт (что сделано, какие тесты и как прогнаны,
   что осталось), ждать проверки. Не переходить к следующему этапу без
   подтверждения.

## 2. Окружение и ветка

- Репозиторий `/app/docker/compose/boba`. Рабочее дерево пользователя стоит
  на другой ветке с незакоммиченными правками. Переключать его нельзя.
- Ветка работ `feature/catalog_lineage` создана от `dev`, в ней уже лежит
  план (`9acda6e3`). Развернуть её отдельным рабочим деревом:

  ```
  git -C /app/docker/compose/boba worktree add /app/docker/compose/boba-catalog feature/catalog_lineage
  ```

  Работать в `/app/docker/compose/boba-catalog`. Виртуальное окружение
  общее: `/app/docker/compose/boba/.venv`. Оно уже содержит все сторонние
  зависимости (psycopg, pydantic, fastapi, pytest, pyright). Новые пакеты
  импортируются через списки путей в корневом `pyproject.toml` (см. п. 4),
  устанавливать их в venv не нужно, пока не появится новая сторонняя
  зависимость. Появилась новая зависимость: остановиться и сказать об этом
  в отчёте, установку делает пользователь.
- Каталог `docs/` целиком в `.gitignore`. Файлы документации добавляются
  через `git add -f`.
- Версия всех пакетов монорепозитория одна: `0.0.15.dev6`. Новые пакеты
  объявляют её же и зависят от соседей с точным `==`.
- Стенд Postgres и его учётки описаны в `compose/chainlit/conf/stand.toml`,
  в тестах адреса не хардкодить, брать фикстуры `boba.stand.fixtures`
  (`pool`, `runtime_config`).

## 3. Что копировать как образец

| Что нужно | Где смотреть |
|---|---|
| Доменный пакет без I/O, pyproject, раскладка `src/boba/<name>/` | `packages/core/boba-connections` |
| Хранилище на `PostgresTable`, `setup()` с DDL, `psycopg.sql`, ошибки слоя | `packages/services/boba-connection-broker/src/boba/connection_broker/store.py` |
| Интеграционный тест хранилища (drop schema, setup, фикстура `pool`) | `packages/services/boba-connection-broker/tests/test_connection_store.py` |
| Секция конфига с `db_schema` и `enable` | `ConnectionsConfig` в том же `store.py` |
| Сообщение шины и его регистрация в union | `WorkflowDraftChanged`, `ConnectionsChanged` в `packages/core/boba-messaging/src/boba/messaging/messages.py` |
| REST-маршруты, тонкие, с разбором в модель и вызовом сервиса | `packages/agents/boba-studio/src/boba/studio/api/workflows.py`, `urls.py` |
| Кастомный маршрут в chainlit с текущим пользователем | `CanvasServing.serve` в `packages/agents/boba-chainlit/src/boba/chainlit/data/upload.py`, регистрация в `infra/bootstrap.py` |
| Инструменты LLM, живущие на хосте и зовущие сервис, их плагин и конфиг | `packages/agents/boba-chainlit/src/boba/chainlit/canvas/diagram.py` (`build_diagram_tools`), `infra/plugins.py` (секция `canvas`), `compose/chainlit/conf/plugins/canvas.toml` |
| Элемент чата со ссылкой на страницу | `packages/agents/boba-chainlit/assets/public/elements/CanvasLink.jsx` |
| Страница на vite + React Flow, генерация типов из OpenAPI, look-тесты | `packages/agents/boba-studio/web/workflow`, `packages/agents/boba-studio/tests/ui/test_workflow_look_ui.py` |
| Субъект вызова, роли, профиль | `Subject` в `packages/core/boba-identity/src/boba/identity/context.py` |

Инструменты из `packages/tools/*` это тела для песочницы, они не подходят:
инструменты каталога зовут сервис на хосте, поэтому живут в chainlit по
образцу `canvas`.

## 4. Регистрация нового пакета

Для каждого нового пакета в корневом `pyproject.toml`:

1. `[tool.pytest.ini_options].pythonpath`: добавить `packages/<слой>/<имя>/src`.
2. `[tool.pyright].extraPaths`: тот же путь.
3. `[tool.uv.sources]`: `<имя> = { workspace = true }`.
   `[tool.uv.workspace].members` покрывает `packages/core/*`,
   `packages/services/*` глобом, отдельной строки не нужно.
4. В `packages/agents/boba-chainlit/pyproject.toml` добавить пакет в
   `dependencies` (сервис и домен) с точной версией.

## 5. Этап 1: домен `boba-catalog`

Пакет `packages/core/boba-catalog`, модуль `boba.catalog`. Зависимости:
только `pydantic`. Ни одного импорта из инфраструктуры.

### Файлы

- `model.py`: сущности и снимок.
- `ops.py`: операции и `apply`.
- `diff.py`: сравнение снимков.
- `__init__.py`: публичный экспорт.
- `tests/test_model.py`, `tests/test_ops.py`, `tests/test_diff.py`.

### Модели (`model.py`)

Все модели `frozen=True, extra="forbid"`. Идентификаторы `UUID`.

```python
class LoadFieldType(StrEnum):
    TEXT = "text"
    INT = "int"
    BOOL = "bool"
    COLUMN = "column"
    COLUMNS = "columns"

class LoadField(BaseModel):
    name: str            # min_length=1, уникально внутри вида
    type: LoadFieldType
    required: bool
    description: str = ""

class LoadKind(BaseModel):
    id: UUID
    name: str
    description: str = ""
    fields: tuple[LoadField, ...]

class Layer(BaseModel):
    id: UUID
    name: str

class Dataset(BaseModel):
    id: UUID
    layer_id: UUID
    name: str
    source: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    owner: str = ""

class Column(BaseModel):
    id: UUID
    dataset_id: UUID
    name: str
    type: str
    nullable: bool
    is_key: bool
    position: int        # ge=0
    description: str = ""

LoadValue = str | int | bool | UUID | tuple[UUID, ...]

class LoadSpec(BaseModel):
    kind_id: UUID
    values: Mapping[str, LoadValue]

class Flow(BaseModel):
    id: UUID
    from_dataset_id: UUID
    to_dataset_id: UUID
    load: LoadSpec
    description: str = ""

class EntityKind(StrEnum):
    LAYER = "layer"
    DATASET = "dataset"
    COLUMN = "column"
    LOAD_KIND = "load_kind"
    FLOW = "flow"

class EntityRef(BaseModel):
    kind: EntityKind
    id: UUID

class CatalogSnapshot(BaseModel):
    layers: Mapping[UUID, Layer]
    datasets: Mapping[UUID, Dataset]
    columns: Mapping[UUID, Column]
    load_kinds: Mapping[UUID, LoadKind]
    flows: Mapping[UUID, Flow]
```

`CatalogSnapshot.empty()` создаёт пустой снимок. `CatalogSnapshot.check()`
проверяет инварианты и поднимает `CatalogInvariantError` с перечнем
нарушений; `apply` зовёт её после каждой операции, хранилище зовёт её после
сборки снимка из строк.

Инварианты:

- имя слоя уникально; имя набора уникально внутри слоя; имя колонки
  уникально внутри набора; имя вида загрузки уникально;
- `Dataset.layer_id`, `Column.dataset_id`, `Flow.from_dataset_id`,
  `Flow.to_dataset_id`, `Flow.load.kind_id` ссылаются на существующие
  сущности; `from_dataset_id != to_dataset_id`;
- `Flow.load.values`: ключи только из полей вида, все `required` заполнены,
  тип значения соответствует `LoadFieldType` (`COLUMN` это один `UUID`,
  `COLUMNS` непустой кортеж `UUID`), каждая колонка принадлежит набору
  `from_dataset_id` или `to_dataset_id` этого потока;
- `Column.position` уникальна внутри набора.

### Операции (`ops.py`)

```python
class CatalogOpKind(StrEnum):
    ADD_LAYER = "add_layer"
    SET_LAYER = "set_layer"
    REMOVE_LAYER = "remove_layer"
    ADD_DATASET = "add_dataset"
    SET_DATASET = "set_dataset"
    REMOVE_DATASET = "remove_dataset"
    ADD_COLUMN = "add_column"
    SET_COLUMN = "set_column"
    REMOVE_COLUMN = "remove_column"
    ADD_LOAD_KIND = "add_load_kind"
    SET_LOAD_KIND = "set_load_kind"
    REMOVE_LOAD_KIND = "remove_load_kind"
    ADD_FLOW = "add_flow"
    SET_FLOW = "set_flow"
    REMOVE_FLOW = "remove_flow"
```

Каждая операция это модель с полем `op: Literal[CatalogOpKind.X]` и телом:
`add_*` и `set_*` несут сущность целиком (`layer: Layer` и т. д.),
`remove_*` несёт `id: UUID`. Union `CatalogOp = Annotated[..., Field(discriminator="op")]`,
`OperationList = TypeAdapter(list[CatalogOp])` для разбора JSON на границе.

Семантика:

- `add_*`: id ещё не занят, иначе ошибка;
- `set_*`: id существует, сущность заменяется целиком;
- `remove_dataset` удаляет и его колонки, но отказывает, пока на набор
  ссылается хоть один поток; `remove_layer` отказывает при наличии наборов;
  `remove_load_kind` отказывает при наличии потоков этого вида;
  `remove_column` отказывает, пока колонка упомянута в `values` какого-либо
  потока. Удаление зависимого делается явной операцией раньше в том же
  списке.

`apply(snapshot, ops) -> CatalogSnapshot` чистая функция: снимок не
меняется, возвращается новый. Ошибка `CatalogOpError(index, op, reason)`
с номером операции в списке, применение прерывается на первой ошибке.
Пустой список операций допустим и возвращает тот же снимок.

Контракт ошибок модуля: `CatalogOpError`, `CatalogInvariantError`. Оба
наследуют `CatalogError`.

### Diff (`diff.py`)

```python
class ChangeStatus(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"

class DiffEntry(BaseModel):
    ref: EntityRef
    status: ChangeStatus

class CatalogDiff(BaseModel):
    entries: tuple[DiffEntry, ...]

    def status_of(self, ref: EntityRef) -> ChangeStatus: ...
```

`diff(base, other) -> CatalogDiff`: по каждому виду сущности сравнение по
`id`; `MODIFIED`, если `model_dump()` различается. `UNCHANGED` в `entries`
не пишется, `status_of` возвращает его для отсутствующих.

### Тесты этапа 1

Данные тестов это реалистичный каталог: три слоя, пять наборов, колонки,
два вида загрузки (`full` без полей, `hashkey` с `hash_columns: columns`
и `batch: int`), три потока. Проверить:

- применение полной последовательности `add_*` даёт снимок, проходящий
  `check()`;
- каждый инвариант ловится с внятным текстом и правильным `index`;
- `remove_*` с зависимостями отказывает, после явного удаления зависимых
  проходит;
- `set_*` заменяет целиком, `diff` даёт `MODIFIED` только для изменённых;
- `values` потока: лишнее поле, пропущенное обязательное, колонка чужого
  набора, неверный тип значения;
- разбор списка операций из JSON через `OperationList` и обратная
  сериализация без потерь;
- `apply` не мутирует входной снимок.

Готовность: pyright чист по пакету, тесты зелёные, отчёт.

## 6. Этап 2: сервис `boba-catalog-service`

Пакет `packages/services/boba-catalog-service`, модуль
`boba.catalog_service`. Зависимости: `boba-catalog`, `boba-db-postgres`,
`boba-identity`, `boba-messaging`, `pydantic`, `psycopg[binary,pool]`.

### Файлы

- `config.py`: `CatalogConfig(enable: bool, db_schema: str, view_roles:
  tuple[str, ...], edit_roles: tuple[str, ...])`. Без дефолтов, кроме
  пустых кортежей ролей быть не должно: пустой список это ошибка валидации.
- `store.py`: `CatalogStore(PostgresTable)`: DDL из плана §3 в `setup()`,
  чтение снимка, публикация, черновики, виды, раскладка, шаринг. Имена
  таблиц и колонок в `StrEnum`.
- `service.py`: `CatalogService`: проверка прав по `Subject.roles`,
  сценарии из плана §6, публикация события в шину.
- `tests/test_catalog_store.py`, `tests/test_catalog_service.py`, маркер
  `integration`.

### Хранилище

- `snapshot() -> CatalogSnapshot`: собрать из таблиц, вызвать `check()`.
- `publish(draft_id, subject) -> Version`: в одной транзакции взять
  черновик `for update`, убедиться `base_version == current_version`,
  свернуть `draft_ops`, применить к снимку в памяти, перезаписать таблицы
  сущностей (удаление и вставка по diff), вставить `versions`, закрыть
  черновик. Номер версии это `max(number) + 1`, первая публикация `1`,
  пустой каталог это версия `0`.
- `append_ops(draft_id, expected_seq, author, ops) -> DraftOp`: транзакция,
  `select ... for update` строки черновика, проверка `expected_seq` равен
  последнему `seq`, проверка применимости (свёртка плюс новые операции через
  `apply`), вставка. Нарушение `seq` это `DraftConflictError(current_seq)`,
  неприменимость это `CatalogOpError` наружу как есть.
- `draft_state(draft_id) -> DraftState {snapshot, diff, seq, base_version}`.
- `rebase(draft_id)`: применить операции черновика к текущему
  опубликованному снимку; не применимые операции вернуть списком с
  причинами, черновик не менять, пока список не пуст.
- Виды, раскладка, шаринг: CRUD без затей.

### Сервис и права

- Читать каталог, черновики и виды: роль из `view_roles` или `edit_roles`,
  либо шаринг вида на роль или пользователя.
- Создавать черновики, слать операции, публиковать, править виды: роль из
  `edit_roles`. Иначе `CatalogRefusal`.
- После `append_ops`, `publish`, изменения вида: `CatalogChanged` в шину со
  scope пользователя (`Scope.user`). Сообщение объявить в
  `boba.messaging.messages`: новый член `MessageKind.CATALOG_CHANGED`,
  класс `CatalogChanged(draft_id: UUID | None, version: int | None,
  view_id: UUID | None, action: ChangeAction)`, добавить в union
  `AnyMessage` и `__all__`. Из трёх идентификаторов ровно один заполнен,
  проверка валидатором модели.

Контракт ошибок: `CatalogStoreError` (Postgres недоступен или ответ битый,
упаковывает `psycopg` ошибки через `raise ... from`), `DraftConflictError`,
`DraftStaleError`, `DraftNotFoundError`, `ViewNotFoundError`,
`CatalogRefusal`, плюс доменные `CatalogOpError` и `CatalogInvariantError`.

### Тесты этапа 2

На стенде, схема `catalog_test`, перед тестом `drop schema cascade`, затем
`setup()`. Обязательные сценарии: публикация пустого черновика даёт версию
без изменений; полный цикл «черновик, операции, публикация, снимок из
таблиц равен снимку в памяти»; гонка двух авторов с одним `expected_seq`
(два параллельных `append_ops` через `asyncio.gather`, ровно один
проходит); `publish` при отставшем `base_version` отказывает, `rebase`
чинит; права: пользователь без роли получает `CatalogRefusal`, с шарингом
вида читает вид; событие `CatalogChanged` доходит до подписчика шины.

## 7. Этап 3: JSON API в chainlit

- Секция `[catalog]` в `AppConfig` chainlit (`infra/config.py`), поле
  `catalog: CatalogConfig`. В `compose/chainlit/conf/config.toml` секция с
  явными значениями; в `stand.toml` тестовые роли.
- Модуль `boba/chainlit/catalog/api.py`: класс `CatalogApi` с методами на
  каждый маршрут из плана §4, `APIRouter` с префиксом `{prefix}/api/catalog`,
  текущий пользователь через `Depends(get_current_user)` как в
  `CanvasServing.serve`, `Subject` собирается так же, как для инструментов
  чата (найти по `Subject.of_user` в chainlit). Тела запросов и ответов это
  pydantic-модели, описанные в том же модуле.
- Регистрация в `infra/bootstrap.py` функцией `_use_catalog(c)` по образцу
  соседей; при `enable = false` не регистрировать.
- Коды: 401 без входа, 403 `CatalogRefusal`, 404 не найдено, 409
  `DraftConflictError` и `DraftStaleError` с телом `{current_seq}` или
  `{current_version}`, 422 `CatalogOpError` с `{index, reason}`, 503
  `CatalogStoreError`.
- OpenAPI: сохранить `openapi.json` маршрутов в
  `packages/agents/boba-chainlit/web/catalog/openapi.json` скриптом, как это
  делает studio для страницы workflow.
- Тесты: `tests/test_catalog_api.py` через HTTP на стенде, все коды из
  списка.

## 8. Этап 4: инструменты LLM

Модуль `boba/chainlit/catalog/tools.py`, функция `build_catalog_tools(cfg)`
по образцу `build_diagram_tools`. Секция плагина `catalog` в
`infra/plugins.py`, конфиг `compose/chainlit/conf/plugins/catalog.toml` с
`enable` и списком инструментов, такой же файл в `compose/studio/conf/plugins`.

Инструменты (имена и назначение из плана §4): `catalog_read`,
`catalog_draft`, `catalog_propose`, `catalog_diff`, `catalog_open`.
Интерфейс по правилу проекта простой: `catalog_propose` принимает
`draft_id: str` и `operations: str` (JSON-список операций), разбирает через
`OperationList`, ошибки разбора и применения возвращает `ErrorResult` с
текстом для модели, а не исключением. `catalog_read` без аргументов отдаёт
снимок с видами загрузки; с аргументом `datasets: str` (имена через
запятую) отдаёт срез с соседями по потокам. `catalog_open` кладёт в чат
элемент-ссылку (новый `CatalogLink.jsx` по образцу `CanvasLink.jsx`) на
страницу черновика или вида.

Тест: сценарий на UI-стенде инструментов (`packages/agents/boba-chainlit/tests/ui/test_tools_ui.py`,
образец `ToolFeed`/`ToolExpect`): фейковая модель зовёт `catalog_draft`,
`catalog_propose`, `catalog_diff`, в ленте видны результаты, в базе есть
операции.

## 9. Этапы 5–7: страница

Подробности в плане §5. Опорные точки:

- Каталог приложения `packages/agents/boba-chainlit/web/catalog`, стек как у
  `boba-studio/web/workflow` (vite, React 18, `@xyflow/react`,
  `react-router-dom`, `zod`, `openapi-typescript`), плюс `elkjs`. Новые
  npm-зависимости это остановка и отчёт: ставит пользователь.
- Исходники liam для переноса: клонировать
  `https://github.com/liam-hq/liam` с `--depth 1 --sparse` и
  `git sparse-checkout set frontend` в scratchpad; нужен только
  `frontend/packages/erd-core/src` и `frontend/packages/ui/src/markers`,
  таблица соответствий в плане §5. Лицензия Apache-2.0, файл лицензии
  положить рядом с перенесёнными исходниками.
- Маршрут отдачи статики страницы регистрируется в chainlit так же, как
  канвас; адреса из плана §5.
- Look-тесты на каждый виджет обязательны, образец
  `packages/agents/boba-studio/tests/ui/test_workflow_look_ui.py` и
  `boba.stand.ui.look`.

## 10. Порядок коммитов

В рабочем дереве `/app/docker/compose/boba-catalog` обычные `git add` и
`git commit` в `feature/catalog_lineage`. Один этап это один или несколько
коммитов с понятными сообщениями по образцу истории (`catalog: ...`).
Сообщение коммита завершается строкой
`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
Ничего не пушить и не мержить: это делает пользователь.

## 11. Отчёт по этапу

Список файлов, что в них появилось; команды pyright и pytest с итогом;
что не сделано и почему; вопросы, которые нужно решить до следующего этапа.
Коротко, без пересказа кода.
