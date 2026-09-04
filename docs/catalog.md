# Каталог данных и диаграмма потоков

Что такое каталог, как в нём живут черновики и версии, кто что может, как
устроены страница, JSON API, события и инструменты LLM, где лежит конфиг и как
собирается страница. План и решения — в `catalog-lineage-plan.md`, задание
исполнителю — в `catalog-lineage-brief.md`.

Содержание:

1. Модель
2. Черновики, версии, публикация
3. Виды, раскладка, шаринг
4. Права
5. Страница
6. JSON API
7. Живые события
8. Инструменты LLM
9. Конфиг и развёртывание
10. Пакеты и слои
11. Тесты

---

## 1. Модель

Каталог — единственный источник правды о хранилище. Это пять таблиц
сущностей, каждая с `id` типа UUID:

| Сущность | Поля | Смысл |
|---|---|---|
| `Layer` | `name` | слой хранилища: raw, stg, dm; порядок дорожек — порядок слоёв в каталоге |
| `Dataset` | `layer_id`, `name`, `source`, `owner`, `tags`, `description` | набор данных (таблица, файл, топик) в слое |
| `Column` | `dataset_id`, `name`, `type`, `nullable`, `is_key`, `position` | колонка набора |
| `LoadKind` | `name`, `fields[]`, `description` | вид загрузки: full, hashkey, period; `fields` описывают, что заполняется у потока |
| `Flow` | `from_dataset_id`, `to_dataset_id`, `load{kind_id, values}`, `description` | поток из набора в набор с правилом загрузки |

Виды загрузки не зашиты в код. Каждый вид объявляет поля, а поток хранит
значения по этим полям. Тип поля задаёт форму значения:

| Тип поля | Значение у потока | Пример |
|---|---|---|
| `text` | строка | `note = "nightly"` |
| `int` | число | `batch = 7` |
| `bool` | флаг | `full_refresh = true` |
| `column` | id колонки любого из концов потока | `key_column = <uuid>` |
| `columns` | список id колонок | `hash_columns = [<uuid>, <uuid>]` |

Пример: вид `hashkey` с полем `hash_columns: columns, required`. Поток
`orders_raw → orders_stg` этого вида хранит `{"hash_columns": [id колонки
orders_raw.id]}`. Панель страницы показывает не id, а имя колонки.

Снимок каталога (`CatalogSnapshot`) проверяет инварианты целиком: имена слоёв
и видов уникальны, имя набора уникально в слое, имя и позиция колонки
уникальны в наборе, поток ссылается на существующие наборы и вид, значения
полей соответствуют типам, ссылки на колонки указывают на колонки концов
потока.

Правки — это операции. Их пятнадцать, по три на сущность:
`add_layer`, `set_layer`, `remove_layer`, `add_dataset`, `set_dataset`,
`remove_dataset`, `add_column`, `set_column`, `remove_column`,
`add_load_kind`, `set_load_kind`, `remove_load_kind`, `add_flow`, `set_flow`,
`remove_flow`. `set_*` заменяет сущность целиком по `id`. `remove_dataset`
уносит колонки набора, но отказывает, пока на набор ссылается поток;
`remove_layer` отказывает, пока в слое есть наборы; `remove_load_kind` —
пока есть потоки этого вида. Инварианты проверяются после каждой операции,
поэтому порядок операций в порции важен: сначала удаления, потом изменения,
потом добавления.

Список операций в JSON:

```json
[
  {"op": "add_layer", "layer": {"id": "…", "name": "raw"}},
  {"op": "add_dataset", "dataset": {"id": "…", "layer_id": "…", "name": "orders"}},
  {"op": "add_column", "column": {"id": "…", "dataset_id": "…", "name": "id",
                                  "type": "text", "nullable": false, "is_key": true, "position": 0}},
  {"op": "add_load_kind", "load_kind": {"id": "…", "name": "full", "fields": []}},
  {"op": "add_flow", "flow": {"id": "…", "from_dataset_id": "…", "to_dataset_id": "…",
                              "load": {"kind_id": "…", "values": {}}}}
]
```

## 2. Черновики, версии, публикация

Опубликованный каталог живёт версиями `v1, v2, …`. Версия — это применённый к
предыдущей список операций. Черновик — ветка операций над базовой версией:
он создаётся над текущей версией и накапливает порции операций. Порция
принимается только с ожидаемым номером `expected_seq`: если кто-то успел
положить порцию раньше, сервер отвечает `409` с текущим `current_seq`, и
клиент перечитывает черновик и повторяет. Так человек на странице и LLM в
чате правят один черновик, не затирая друг друга: порции только добавляются.

Публикация в одной транзакции применяет операции черновика к таблицам,
пишет новую версию и закрывает черновик. Если за время работы над черновиком
каталог ушёл вперёд (опубликована другая версия), публикация отвечает `409`
с `current_version`. Тогда черновик перебазируется: его операции
проигрываются поверх новой версии. Операция, которая больше не применима
(например, `set_dataset` набора, который уже удалили), попадает в список
`issues` с номером порции и индексом; по просьбе пользователя такие операции
вычёркиваются (`drop_conflicts`), и черновик встаёт над новой версией.

Статусы черновика: `open` → `published` или `discarded`. Закрытый черновик
на странице показывается только для чтения.

## 3. Виды, раскладка, шаринг

Вид (`View`) — сохранённая диаграмма над опубликованным каталогом: имя и
фильтр. Фильтр — список слоёв (`layer_ids`, слои целиком) плюс список
отдельных наборов (`dataset_ids`). Пустой фильтр значит весь каталог. Срез
по фильтру считает домен: наборы из фильтра, их слои и колонки, потоки строго
между ними и только задействованные виды загрузки.

Раскладка (`Layout`) — сохранённые позиции узлов вида. Узел без позиции
раскладывает ELK по замеренным размерам карточек. Владелец перетаскивает
узлы, раскладка пишется отложенно; «прибрать» перекладывает ELK заново и
тоже сохраняет.

Шаринг (`Share`) открывает вид на просмотр роли по имени или пользователю
по id. Шаринг даёт только просмотр: правки каталога и черновики требуют роли
из `edit_roles`.

## 4. Права

Секция `[catalog]` задаёт две группы ролей: `view_roles` читают весь каталог,
`edit_roles` правят его. Владелец вида — тот, кто его создал.

| Действие | Роль из `edit_roles` | Роль из `view_roles` | Никакой роли, вид расшарен |
|---|---|---|---|
| снимок, версии, список черновиков | да | да | нет (`403`) |
| создать черновик, порции, публикация, rebase | да | нет | нет |
| создать вид | да | нет | нет |
| открыть вид (`GET /views/{id}/state`) | да | да | да, только срез |
| править вид, раскладку, шаринг, удалить вид | только владелец | нет | нет |
| события `GET /events` | да | да | нет |

`GET /access` отдаёт `user_id`, `login`, `can_view`, `can_edit`: страница по
этому решает, какие формы и кнопки показывать.

## 5. Страница

Адреса под префиксом chainlit:

| Адрес | Что показывает |
|---|---|
| `{prefix}/catalog/` | индекс: виды и открытые черновики, формы нового вида и черновика |
| `{prefix}/catalog/views/{view_id}` | диаграмма вида над опубликованным каталогом |
| `{prefix}/catalog/drafts/{draft_id}` | диаграмма черновика с правками |

Состояние страницы живёт в адресе: `?active=<dataset_id>` — выбранный набор,
`mode=ALL_FIELDS|KEY_ONLY|TABLE_NAME` — режим карточек, `hidden=<id,id>` —
скрытые наборы, `diff=0` — выключенная подсветка изменений черновика.
Ссылку можно отправить коллеге, он увидит то же самое.

Три панели: список наборов по слоям слева (поиск, глаз скрывает набор на
холсте), холст посередине (дорожки слоёв слева направо, карточки наборов,
рёбра потоков с ярлыком вида загрузки, тулбар: зум, вписать, прибрать,
режим карточек), панель деталей справа (паспорт набора, колонки, входящие
и исходящие потоки со значениями полей). Клик по набору выбирает его и
подсвечивает соседей, наведение подсвечивает без выбора. На узком экране
список закрыт по умолчанию.

На черновике (`data-editable="true"`) добавляются правки:

| Где | Что | Операция |
|---|---|---|
| список, низ | «layer» | `add_layer` через подсказку имени |
| заголовок слоя | плюс, карандаш, корзина (только у пустого слоя) | `add_dataset`, `set_layer`, `remove_layer` |
| панель, шапка | карандаш, корзина | форма набора → `set_dataset`; `remove_flow…` + `remove_dataset` |
| панель, колонки | «edit columns» | редактор строк → `remove_column`, `set_column`, `add_column` |
| панель, исходящие | «flow» | форма потока с выбором приёмника → `add_flow` |
| панель, поток | карандаш; ребро на холсте | форма потока → `set_flow` или `remove_flow` |
| холст | соединение handle'ов двух узлов | форма потока → `add_flow` |
| шапка | «diff» | подсветка added/modified/removed относительно базовой версии |
| шапка | «publish», «discard», «update to vN» | публикация, отмена, перебазирование |

Каждое действие уходит порцией в `POST drafts/{id}/ops`. Страница держит
очередь порций строго по одной; на `409` перечитывает черновик и повторяет
до трёх раз. Отказ сервера (нарушен инвариант) показывается тостом с номером
операции и причиной.

Публикация при устаревшем черновике открывает диалог «the catalog has moved
on»: «update the draft» перебазирует; если операции не применимы, диалог
перечисляет их (`portion N · operation #i: причина`) и предлагает «drop the
conflicts and update» или «keep the draft as is».

На виде владелец видит в шапке карандаш (имя и фильтр: галочки слоёв и
наборов по полному каталогу), «share» (список выдач, добавление роли или
пользователя, отзыв), корзину (удаление с подтверждением). Узлы у владельца
перетаскиваются.

Атрибуты для тестов и отладки: `data-testid="catalog-page"` с
`data-source=view|draft`, `data-editable`, `data-owned`, `data-seq` (номер
последней принятой порции), `data-layout-saves`; холст `data-testid="canvas"`
с `data-ready` и счётчиком `data-layouts`; узлы `data-testid="dataset-node"`
с `data-dataset`, `data-status`, `data-active`, `data-highlighted`; дорожки
`data-testid="layer-lane"` с `data-layer`; диалоги `data-dialog=<mark>`.

## 6. JSON API

Все пути под `{prefix}/api/catalog`, контракт — OpenAPI (`GET
{prefix}/openapi.json`, снимок в `web/catalog/openapi.json`). Субъект — из
cookie входа chainlit. Ответы — модели `boba.catalog_service.records`.

| Метод и путь | Тело | Ответ | Ошибки |
|---|---|---|---|
| `GET /access` | — | `CatalogAccess` | `401` |
| `GET /snapshot` | — | `CatalogSnapshot` | `403` |
| `GET /versions` | — | `Version[]` | `403` |
| `GET /drafts` | — | `Draft[]` открытые | `403` |
| `POST /drafts` | `{name}` | `Draft` | `403` |
| `GET /drafts/{id}` | — | `DraftState{draft, snapshot, diff, seq}` | `403`, `404` |
| `DELETE /drafts/{id}` | — | `Draft` со статусом `discarded` | `403`, `404`, `409` закрыт |
| `POST /drafts/{id}/ops` | `{expected_seq, operations[]}` | `DraftState` | `409 {message, current_seq}`, `422 {message, index, reason}`, `409` закрыт |
| `POST /drafts/{id}/publish` | — | `Version` | `409 {message, current_version}` |
| `POST /drafts/{id}/rebase` | `{drop_conflicts}` | `RebaseResult{draft, issues[]}` | `403`, `404` |
| `GET /views` | — | `View[]` доступные субъекту | `401` |
| `POST /views` | `ViewSpec{name, dataset_ids, layer_ids}` | `View` | `403` |
| `GET /views/{id}` | — | `View` | `403`, `404` |
| `GET /views/{id}/state` | — | `ViewState{view, version, snapshot, layout, owned}` | `403`, `404` |
| `PUT /views/{id}` | `ViewSpec` | `View` | `403` не владелец |
| `DELETE /views/{id}` | — | `{deleted}` | `403` |
| `GET /views/{id}/layout` | — | `ViewLayout{positions[]}` | `403` |
| `PUT /views/{id}/layout` | `{positions[{dataset_id, x, y}]}` | `ViewLayout` | `403` |
| `GET /views/{id}/shares` | — | `ViewShare[]` | `403` |
| `POST /views/{id}/shares` | `{kind: role\|user, target}` | `204` | `403` |
| `DELETE /views/{id}/shares/{kind}/{target}` | — | `{deleted}` | `403` |
| `GET /events` | — | `text/event-stream` | `403` |

Отключённый сервис (`enable = false` или нет соединения) отвечает `503`.

Пример цикла черновика:

```http
POST {prefix}/api/catalog/drafts            {"name": "new sources"}
→ {"id": "d1", "base_version": 3, "status": "open", …}

POST {prefix}/api/catalog/drafts/d1/ops     {"expected_seq": 0, "operations": [...]}
→ {"draft": {...}, "snapshot": {...}, "diff": {"entries": [...]}, "seq": 1}

POST {prefix}/api/catalog/drafts/d1/ops     {"expected_seq": 0, "operations": [...]}
→ 409 {"detail": {"message": "draft moved", "current_seq": 1}}

POST {prefix}/api/catalog/drafts/d1/publish
→ {"number": 4, "author": {"user_id": "…", "via": "user"}, "published_at": "…"}
```

## 7. Живые события

После каждой правки сервис публикует `CatalogChanged{draft_id | version |
view_id, action}` в шину сообщений в область пользователя. `GET /events`
отдаёт их server-sent events: кадр `data: {json}` на событие и `: ping` раз в
15 секунд. Страница подписана через `EventSource`:

- событие по своему черновику — дотянуть новые порции (чужие правки, в том
  числе от LLM, появляются без перезагрузки);
- новая версия — перечитать вид и показать на черновике кнопку «update to
  vN»;
- правка вида — перечитать вид.

Ответ загрузки, совпадающий с уже показанным (эхо своего же события), граф не
перекладывает.

## 8. Инструменты LLM

Плагин `catalog` chainlit (секция `[tool.catalog]`, файл
`conf/plugins/catalog.toml`) даёт модели пять инструментов. Субъект вызова —
пользователь чата, права те же, что на странице.

| Инструмент | Аргументы | Что делает |
|---|---|---|
| `catalog_read` | `datasets` — имена через запятую, пусто — весь каталог | снимок или срез с именами рядом с id и видами загрузки |
| `catalog_draft` | `name` | новый черновик над текущей версией; в ответе id |
| `catalog_propose` | `draft_id`, `operations` — JSON-список операций | порция в черновик; `expected_seq` инструмент перечитывает сам, до трёх попыток; отказ называет индекс операции и причину |
| `catalog_diff` | `draft_id` | изменения черновика относительно базовой версии словами |
| `catalog_open` | `kind` = `draft` или `view`, `entity_id` | элемент-ссылка `CatalogLink` в чат на страницу |

Сценарий «LLM правит, страница показывает»: пользователь открыл черновик на
странице, в чате попросил модель добавить набор; `catalog_propose` положил
порцию, событие дошло до страницы, узел появился с пометкой `added`.

Конфиг плагина:

```toml
enable = true
tools  = ["catalog_read", "catalog_draft", "catalog_propose", "catalog_diff", "catalog_open"]
```

Элемент `CatalogLink.jsx` лежит в `assets/public/elements/` пакета и в
`app_root/public/elements/` развёртывания.

## 9. Конфиг и развёртывание

Секция `[catalog]` в `config.toml` chainlit:

```toml
[env]
    catalog_page = "built"

[catalog]
    enable     = true
    connection = "${postgres}"
    db_schema  = "catalog"
    view_roles = ["read"]
    edit_roles = ["wrt"]
    page       = "${env.catalog_page}"
    dist       = "${env.app_root}/public/catalog"
```

| Ключ | Смысл |
|---|---|
| `enable` | создавать таблицы схемы при старте; `false` — API отвечает `503` |
| `connection` | Postgres-профиль ссылкой |
| `db_schema` | схема таблиц каталога: `versions`, `drafts`, `draft_ops`, `layers`, `datasets`, `columns`, `load_kinds`, `flows`, `views`, `view_layout`, `view_shares` |
| `view_roles`, `edit_roles` | роли чтения и правок |
| `page` | `built` — раздавать сборку из `dist`; адрес — проксировать vite dev-сервер |
| `dist` | каталог сборки страницы (`index.html`, `assets/`) |

Оверрайд средой: `BOBA_CATALOG_PAGE`.

Страница `web/catalog` (vite, React 18, @xyflow/react, elkjs, react-router,
zod, типы из OpenAPI) собирается node'ом образа:

```sh
make -C build/chainlit web-catalog        # npm ci, generate, check, build → assets/catalog
make -C build/chainlit catalog-openapi    # обновить web/catalog/openapi.json из приложения
cp -a packages/agents/boba-chainlit/assets/catalog compose/chainlit/app_root/public/catalog
```

В образ страницу собирает стадия `catalog-build` Dockerfile. После правок
фронта dev-стенд показывает старую сборку, пока dist не скопирован заново.

## 10. Пакеты и слои

| Пакет | Слой | Что внутри |
|---|---|---|
| `boba-catalog` | core | модели, инварианты, операции, diff, срез; без I/O |
| `boba-catalog-service` | services | `CatalogStore` на Postgres, `CatalogService` с правами и событиями, записи API |
| `boba-messaging` | core | `CatalogChanged` |
| `boba-chainlit` | agents | `catalog/api.py` (маршруты, SSE), `catalog/tools.py` (инструменты), `catalog/schema.py` (OpenAPI), страница `web/catalog` |
| `boba-stand` | testing | учётки `admin/ADM`, `dev/DEV`, `guest/GST`, сброс схемы каталога |

Домен не знает про транспорт и базу, сервис не знает про chainlit, chainlit
зовёт сервис через провайдеры хоста.

## 11. Тесты

| Набор | Что проверяет |
|---|---|
| `packages/core/boba-catalog/tests` | инварианты, операции, diff, срез на примере каталога |
| `packages/services/boba-catalog-service/tests` (`-m integration`) | хранилище, гонка двух авторов, публикация, rebase, виды, шаринг, события шины |
| `boba-chainlit/tests/test_catalog_api.py`, `test_catalog_tools.py` | маршруты через ASGI, инструменты in-process |
| `tests/ui/test_catalog_api_ui.py` (`-m ui`) | API и SSE на живом стенде |
| `tests/ui/test_catalog_look_ui.py` | внешний вид: дорожки, карточки, рёбра, режимы, diff, узкий экран; цвета из токенов, геометрия из DOM |
| `tests/ui/test_catalog_edit_ui.py` | правки черновика: подсказки имени, формы, колонки, потоки из панели и соединением, удаление, чужие порции, публикация с rebase |
| `tests/ui/test_catalog_views_ui.py` | виды: права на индексе, фильтр, раскладка перетаскиванием, шаринг гостю, удаление |
| `tests/ui/test_catalog_widgets_ui.py` | каждая кнопка и виджет: тулбар, шапка, поиск, подсветка, закрытие диалогов, галочки колонок, все поля форм, тосты, отмена черновика, rebase с конфликтами, навигация, аноним, узкий экран |
| `tests/ui/test_tools_ui.py::TestCatalogTools` | инструменты через чат с фейковой моделью, в том числе «LLM правит, открытая страница показывает» |
| `web/catalog/src/model/editor.test.ts` (vitest) | очередь порций и повтор по 409 |

Общие помощники браузерных тестов — `tests/ui/catalog_ui.py`: селекторы,
клиент API от имени учётки, сид каталога с префиксом `ed_` и его снос.
Опубликованный каталог стенда один на все модули, поэтому модуль, который
публикует своё, на выходе публикует удаление.
