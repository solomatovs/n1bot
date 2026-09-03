# Каталог данных и диаграмма потоков: план

## 1. Цель и границы

Появляется каталог данных хранилища: источники, слои хранения, наборы данных с
колонками и потоки между наборами с правилом загрузки (full, period, hashkey).
Каталог показывается диаграммой потоков на отдельной странице, открываемой из
чата chainlit. Диаграмму правят и человек на странице, и LLM через инструменты в
чате, работая над одним черновиком. Каталог хранится в Postgres реляционными
таблицами.

Каталог единственный источник правды. Диаграмма это сохранённый вид над
каталогом: подмножество наборов, раскладка, фильтры. Черновик это ветка правок
над каталогом, а не копия картинки.

Не делаем: генерацию DDL, проверку схемы в PGlite, парсеры чужих форматов,
размещение в boba-studio. Из liam (`@liam-hq/erd-core`) берём только слой
отрисовки и модель «правки как операции поверх версии», остальное пишем сами.

## 2. Модель предметной области

Все сущности имеют суррогатный `id`, имена только атрибут. Переименование не
ломает ссылки и журнал операций.

```
Layer     id, name
Dataset   id, layer_id, name, source, description, tags, owner
Column    id, dataset_id, name, type, nullable, is_key, position, description
LoadKind  id, name, description, fields[]
Flow      id, from_dataset_id, to_dataset_id, load, description
```

Слои задаются пользователем как имена, никакой стандартизации. Порядок слоёв
на диаграмме это порядок их создания.

Связи между наборами не выводятся из ключей, как FK в liam, а объявляются
явно потоками. Ключи колонок нужны только карточке набора.

### Правило загрузки потока

Правила загрузки это дискриминирующие классы со своими наборами параметров,
и даже внутри одного класса состав параметров меняется от потока к потоку.
Поэтому виды загрузки не зашиваются enum'ом, а живут в каталоге как
`LoadKind`, который пользователь или LLM заводят сами. `LoadKind` описывает
свои поля:

```json
{
  "name": "hashkey",
  "fields": [
    {"name": "hash_columns", "type": "columns", "required": true},
    {"name": "compare_columns", "type": "columns", "required": false},
    {"name": "batch", "type": "int", "required": false}
  ]
}
```

Тип поля это enum `LoadFieldType {TEXT, INT, BOOL, COLUMN, COLUMNS}`.
Поток хранит `load = {kind_id, values}`, где `values` это словарь по именам
полей вида; ссылки на колонки хранятся по `id` колонок, поэтому
переименование колонки не ломает правило. Значения проверяются против
`fields` вида на границе (в операции), а не в местах использования: лишнее
поле, пропущенное обязательное, колонка не из набора-источника дают
`CatalogOpError`.

Так один вид `period` у одного потока заполняется полем даты, у другого
полем номера партии, а новый вид загрузки появляется без правки кода.
Страница строит форму потока по `fields`, LLM получает `fields` в
`catalog_read` и знает, что заполнять.

### Снимок и операции

`CatalogSnapshot` это полное состояние каталога одной версии: словари сущностей
по `id`. Правка описывается списком операций над снимком:

```json
[
  {"op": "add_dataset", "dataset": {"id": "…", "layer_id": "…", "name": "stg_orders"}},
  {"op": "set_column", "column": {"id": "…", "dataset_id": "…", "name": "order_id", "type": "int", "is_key": true}},
  {"op": "add_load_kind", "load_kind": {"id": "…", "name": "hashkey", "fields": [{"name": "hash_columns", "type": "columns", "required": true}]}},
  {"op": "add_flow", "flow": {"id": "…", "from_dataset_id": "…", "to_dataset_id": "…", "load": {"kind_id": "…", "values": {"hash_columns": ["…"]}}}},
  {"op": "remove_dataset", "id": "…"}
]
```

Набор операций фиксирован enum'ом `CatalogOp` (add/set/remove на каждую из
пяти сущностей: слой, набор, колонка, вид загрузки, поток); каждая операция
это pydantic модель, union по полю `op`. Применение к снимку чистое: `apply(snapshot, ops)
-> snapshot`, при нарушении инварианта (поток на несуществующий набор,
дубликат имени в слое) `CatalogOpError` с номером операции.

Diff двух снимков даёт `ChangeStatus {ADDED, REMOVED, MODIFIED, UNCHANGED}` на
каждую сущность по `id`. Этим же diff'ом страница подсвечивает черновик
относительно опубликованного.

### Версии и черновики

- Опубликованное состояние лежит в реляционных таблицах и читается SQL'ом.
- Каждая публикация фиксируется как `Version {number, operations, author,
  published_at}`; история воспроизводится сворачиванием операций.
- Черновик `Draft {id, name, base_version, status, created_by}` копит
  операции порциями `DraftOp {draft_id, seq, author, operations}`. `author`
  это `{user_id, via: USER | LLM}`: от чьего имени и кем внесена порция.
  Черновик и вид ничего не знают о чатах и тредах: это самостоятельные
  объекты каталога, которые создаёт человек или LLM, а чат лишь показывает
  их элементом-ссылкой.
- Порция принимается только с `expected_seq` равным текущему последнему `seq`
  черновика; иначе `DraftConflictError`, клиент перечитывает и повторяет. Это
  единственный механизм совместной работы человека и LLM.
- Публикация применяет свёрнутые операции черновика к реляционным таблицам
  одной транзакцией и создаёт `Version`. Если `base_version` черновика
  отстал, публикация отказывает `DraftStaleError`; перебазирование делается
  явно: операции черновика применяются к новому снимку, конфликты
  показываются пользователю.

### Виды, раскладка, доступ

- `View {id, name, owner_id, dataset_ids, layer_ids, created_at}` это
  сохранённая диаграмма. Пустой фильтр значит весь каталог.
- `Layout {view_id, dataset_id, x, y}` позиции узлов. Без сохранённой позиции
  узел раскладывает ELK.
- `Share {view_id, target: role | user, mode: VIEW}`. Право править каталог
  задаётся ролями через `boba-access`, шаринг вида даёт только просмотр.

## 3. Хранение в Postgres

Схема `catalog` (имя из конфига, как `db_schema` у соединений), таблицы
создаются `setup()` по образцу `ConnectionStore` на `PostgresTable`:

```
catalog.layers      (id uuid pk, name text unique, created_at timestamptz)
catalog.datasets    (id uuid pk, layer_id uuid fk, name text, source text,
                     description text, tags text[], owner text,
                     unique (layer_id, name))
catalog.columns     (id uuid pk, dataset_id uuid fk, name text, type text,
                     nullable bool, is_key bool, position int, description text,
                     unique (dataset_id, name))
catalog.load_kinds  (id uuid pk, name text unique, description text,
                     fields jsonb)
catalog.flows       (id uuid pk, from_dataset_id uuid fk, to_dataset_id uuid fk,
                     load_kind_id uuid fk, load_values jsonb, description text)
catalog.versions    (number int pk, operations jsonb, author jsonb,
                     published_at timestamptz)
catalog.drafts      (id uuid pk, name text, base_version int, status text,
                     created_by uuid, created_at timestamptz)
catalog.draft_ops   (draft_id uuid fk, seq int, author jsonb, operations jsonb,
                     created_at timestamptz, pk (draft_id, seq))
catalog.views       (id uuid pk, name text, owner_id uuid, dataset_ids uuid[],
                     layer_ids uuid[], created_at timestamptz)
catalog.view_layout (view_id uuid fk, dataset_id uuid, x float, y float,
                     pk (view_id, dataset_id))
catalog.view_shares (view_id uuid fk, target_kind text, target text, mode text,
                     pk (view_id, target_kind, target))
```

Состояние черновика не материализуется в таблицах: сервис читает
опубликованный снимок, сворачивает `draft_ops` и отдаёт снимок черновика
вместе с diff. Черновиков мало, операций в них сотни, сворачивание дешёвое.

## 4. Пакеты и направление зависимостей

```
packages/core/boba-catalog            домен: модели, операции, apply, diff, снимок
        ▲
packages/services/boba-catalog-service
        хранилище catalog.*, черновики, публикация, виды, доступ, события шины
        ▲                          ▲
packages/tools/boba-tool-catalog    packages/agents/boba-chainlit
        инструменты LLM             маршруты JSON API, маршрут страницы,
                                    элемент ссылки в чате, web/catalog (страница)
```

- `boba-catalog` не знает про I/O: pydantic-модели, `CatalogOp`, `apply`,
  `diff`. Ошибки наружу: `CatalogOpError`, `CatalogInvariantError`.
- `boba-catalog-service` зависит от `boba-db-postgres`, `boba-identity`,
  `boba-access`, `boba-messaging`. Ошибки наружу: `CatalogStoreError`
  (хранилище недоступно), `DraftConflictError`, `DraftStaleError`,
  `CatalogRefusal` (нет прав). Публикует в шину событие `CatalogChanged
  {draft_id | version}` со scope пользователя-владельца и scope черновика,
  чтобы открытая страница и чат узнавали о правках друг друга.
- `boba-tool-catalog` объявляет манифест `boba.tools` с секцией `catalog`,
  конфиг `conf/plugins/catalog.toml`. Инструменты: `catalog_read` (снимок или
  срез по наборам вместе с видами загрузки, для контекста модели),
  `catalog_draft` (создать черновик или перечислить открытые), `catalog_propose`
  (операции в указанный черновик, ответ diff и `seq`), `catalog_diff`
  (черновик против опубликованного), `catalog_open` (элемент-ссылка на вид
  или черновик в чат). `draft_id` всегда передаётся явно: у инструментов нет
  «черновика треда». Тела инструментов не нуждаются в песочнице: это вызовы
  сервиса.
- Маршруты в chainlit тонкие: разбирают запрос в модель и зовут сервис,
  как `WorkflowApi` в studio. Субъект берётся из cookie входа через
  `ApiSubject`. Если позже каталог переезжает в studio, переезжают только
  маршруты и страница.

Секция конфига `[catalog]`: `db_schema`, ссылка на соединение postgres, флаг
страницы. Регистрация пакетов в корневом `pyproject.toml` (workspace, sources,
pyright include) и в образе.

### JSON API

```
GET    {prefix}/api/catalog/snapshot                 опубликованный снимок
GET    {prefix}/api/catalog/versions
POST   {prefix}/api/catalog/drafts                   {name, base_version}
GET    {prefix}/api/catalog/drafts/{id}              снимок черновика + diff + seq
POST   {prefix}/api/catalog/drafts/{id}/ops          {expected_seq, operations} → {seq, diff} | 409
POST   {prefix}/api/catalog/drafts/{id}/publish      → {version} | 409 stale
POST   {prefix}/api/catalog/drafts/{id}/rebase
GET    {prefix}/api/catalog/views, POST, PUT, DELETE
PUT    {prefix}/api/catalog/views/{id}/layout        позиции узлов
POST   {prefix}/api/catalog/views/{id}/shares
```

Контракт описывается OpenAPI, типы для страницы генерируются
`openapi-typescript`, как на странице workflow. Инструменты LLM и страница
зовут один и тот же сервис, отдельной логики у маршрутов нет.

## 5. Страница диаграммы

Отдельное vite-приложение `packages/agents/boba-chainlit/web/catalog`
(React 18, `@xyflow/react`, `elkjs`, `zod`, `react-router-dom`), собранная
статика отдаётся маршрутом chainlit по образцу канваса. Адрес страницы:
`{prefix}/catalog/views/{view_id}` и `{prefix}/catalog/drafts/{draft_id}`.
Из чата открывается элементом-ссылкой по образцу `CanvasLink`.

### Что переносится из liam

| Из `erd-core` | Роль у нас |
|---|---|
| `convertSchemaToNodes` | переписывается под снимок каталога: узел на набор, ребро на поток, группа на слой |
| `computeAutoLayout`, `getElkLayout`, конвертеры ELK | как есть; добавляется `elk.direction: RIGHT` и партиции по `Layer.position`, чтобы источники всегда слева |
| `highlightNodesAndEdges` | как есть: подсветка цепочки при hover и выборе |
| `TableNode`, `TableHeader`, `TableColumnList`, `TableColumn` | карточка набора; иконки ключа и nullable остаются, FK-иконка убирается |
| три режима показа `ALL_FIELDS / KEY_ONLY / TABLE_NAME` | как есть |
| `RelationshipEdge` | ребро потока: стрелка вместо маркеров кардинальности, подпись режима загрузки, частицы при подсветке |
| `NonRelatedTableGroupNode` | группа-слой (swimlane) |
| `LeftPane`, `useTableVisibility`, `TableNameMenuButton` | список наборов, видимость, мультивыбор |
| `Toolbar`: zoom, fit, tidy up, show mode | как есть |
| `TableDetail`: секции, якоря `#dataset__columns__col`, `RelatedTables` | панель набора: колонки, потоки входящие и исходящие, мини-канвас соседей |
| `DiffIcon`, `useDiffStyle`, состояние `showDiff` | подсветка diff черновика; статусы приходят из API, per-field `getChangeStatus` не переносится |
| состояние в URL `active`, `showMode`, `hidden` | через search params `react-router` |

Не переносится: `CardinalityMarkers`, `constraintsToRelationships`,
`CommandPalette` (позже, отдельно), `AppBar`, `gtm`, cookie-утилиты,
`VersionProvider`, `neverthrow`, `ts-pattern`, `valibot`, `nuqs`, кит
`@liam-hq/ui` кроме иконок.

### Что пишется заново

- Редактирование на канвасе: форма набора и колонки в панели деталей,
  соединение handle'ов создаёт поток с выбором режима, удаление через
  контекстное меню. Каждое действие становится операцией и уходит в
  `POST drafts/{id}/ops`; при `409` страница перечитывает черновик,
  накладывает ожидающие локальные операции заново и повторяет.
- Переключатель «опубликовано / черновик» и diff-режим поверх снимка.
- Сохранение раскладки: перетаскивание узла пишет `Layout` с задержкой.
- Живое обновление: подписка на `CatalogChanged` через socket.io-слой
  chainlit; при событии по своему черновику страница дотягивает новые
  операции после известного `seq`.
- Виды: создание, фильтр по слоям и наборам, шаринг на просмотр.
- Look-тесты на каждый виджет по образцу `test_*_look_ui.py` и стенд
  `pytest -m ui` со сценарием «LLM правит, страница показывает».

## 6. Сценарии

**Открыть из чата.** Пользователь просит показать витрину. Инструмент
`catalog_open` находит вид или создаёт временный по наборам и отдаёт в чат
элемент-ссылку. Страница грузит снимок и раскладку, ELK докладывает узлы без
сохранённых позиций.

**LLM предлагает изменения.** Пользователь в чате: «добавь в stg поток из
источника orders с hashkey по order_id». Модель зовёт `catalog_read` для
контекста, при отсутствии подходящего черновика создаёт его через
`catalog_draft`, потом шлёт `catalog_propose` с операциями и `draft_id`.
Сервис принимает порцию, отвечает diff'ом, публикует `CatalogChanged`.
Открытая страница черновика подсвечивает новый набор и поток как `ADDED`. В
чат уходит краткий diff и элемент черновика, по которому страница
открывается; в следующем чате этот же черновик показывается тем же элементом
по `draft_id`.

**Человек правит на странице.** Пользователь переименовывает колонку в
панели. Страница шлёт операцию с `expected_seq`. На следующем ходу LLM
читает черновик уже с этой правкой, потому что источник один.

**Конфликт.** Человек и LLM отправили порции с одним `expected_seq`.
Вторая получает `409`, страница или инструмент перечитывают `seq` и
повторяют; если операция стала невалидной (сущность удалена), клиент
показывает причину и снимает операцию.

**Публикация.** Кнопка на странице или инструмент по просьбе пользователя.
Сервис в одной транзакции применяет операции к таблицам, пишет `Version`,
закрывает черновик. Все открытые виды получают событие и перечитывают снимок.

**Шаринг.** Владелец вида добавляет роль в `Share`. Пользователь с этой ролью
открывает страницу в режиме просмотра: правки и черновики недоступны, hover и
панель деталей работают.

## 7. Этапы

Каждый этап заканчивается отчётом и проверкой; коммиты делает пользователь.
Критерий готовности этапа: pyright без ошибок, интеграционные тесты этапа
зелёные.

1. **Домен `boba-catalog`.** Модели, `LoadMode`, `CatalogOp`, `apply`,
   `diff`, снимок. Тесты на применение операций и инварианты на реальных
   примерах каталога.
2. **Сервис `boba-catalog-service`.** `CatalogStore` на `PostgresTable`,
   черновики с `expected_seq`, публикация транзакцией, перебазирование, виды,
   раскладка, шаринг, проверка прав через `boba-access`, события шины. Тесты
   на стенде с реальным Postgres, включая гонку двух авторов.
3. **JSON API в chainlit.** Секция `[catalog]`, маршруты, OpenAPI, субъект из
   cookie. Тесты через HTTP на стенде.
4. **Инструменты LLM `boba-tool-catalog`.** Манифест, конфиг плагина,
   четыре инструмента, элемент-ссылка в чат. Сценарий на UI-стенде
   инструментов.
5. **Страница, просмотр.** Порт отрисовки из liam под снимок каталога:
   узлы, рёбра, слои, раскладка, подсветка, панель деталей, список, тулбар.
   Look-тесты.
6. **Страница, правки.** Черновики, операции, diff-режим, конфликты, живое
   обновление, публикация. UI-тест «LLM правит, страница показывает».
7. **Виды и шаринг.** Создание видов, фильтры, сохранение раскладки,
   просмотр по роли.
8. **Приёмка.** Полный прогон `-m integration` и `-m ui`, документация в
   `docs/`, обновление `adding-connections-and-tools.md` по плагину.

## 8. Принятые решения

- Слои это только имена, заводятся пользователем, порядок по созданию.
- Виды загрузки живут в каталоге как `LoadKind` с описанием полей, поток
  хранит значения по полям вида; enum видов в коде нет.
- Черновик и вид не привязаны к треду; чат показывает их элементом по `id`.
- Авторасстановка узлов на странице через `elkjs`: он умеет вложенные
  группы для слоёв и закрепление узлов по колонкам-партициям. `dagre`,
  который уже есть на странице workflow, групп не умеет. Это деталь страницы,
  на модель и API не влияет.
