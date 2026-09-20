# pg-llm-describer: описания объектов от LLM

Независимый пакет поверх схемы `ix`. Читает `ix.node`, `ix.tree`, `ix.edge`, `ix.pg_meta_edge` и
surface-таблицы скрапера, пишет только в `ix.pg_llm_description`. Со скрапером и индексаторами не
связан ни вызовами, ни внешними ключами. Модель зовёт Python через порт проекта
`boba.chat.generation.StructuredGenerator` (`boba-llm`), SQL знает только текст.

```
schema/   DDL таблицы ix.pg_llm_description
prompt/   системный промпт, шаблон входа, json-схема ответа
run/      SQL шагов цикла
worker.py оркестратор
```

## Запуск

Описатель работает по всей базе `ix` сразу, источники ему не задаются: описываются объекты
всех поверхностей, у которых объявлен аспект класса `describer_input` (у скрапера PostgreSQL это
таблицы и view). Подписка задаётся в конфиге: `classes = ["describer_input"]`. Порядок в цепочке: `pg-meta-scraper` по каждому
источнику, затем описатель, затем `pg-idx-fts` и `pg-idx-vector`, чтобы описания
попали в поиск. Роль в DSN читает `ix.*` и пишет в `ix.pg_llm_description`.

Настройки берутся из одного файла конфига, секция [ix.llm_describer]. Конфиг приложения на dev-стенде лежит в `compose/apps/pg-llm-describer/conf.toml`
(каталог вне git, в нём креды). Одним файлом можно запускать и несколько приложений:
каждое читает только свою секцию.

```
.venv/bin/boba-pg-llm-describer --config ../../compose/apps/pg-llm-describer/conf.toml
```

Схема хранения задаётся полем `db_schema` секции (по умолчанию `ix`): в sql-файлах она
стоит плейсхолдером `{schema}`, имя берётся из конфига, а квотирует его psycopg.

Провайдер выбирается ключом `provider` секции: `openai` (`base_url`, `api_key`, `model`) или
`local` (`model_dir`, например `compose/chainlit/models/onnx-genai/qwen3-4b-int4`). Остальные
поля секции: `max_tokens`, `temperature`, `tool_choice` (auto по умолчанию: deepseek в
thinking mode через роутер проекта другого не принимает), `batch`. Воркер всегда идёт до
пустой очереди и заканчивает prune.

## Что уходит в модель

Вход собирает `run/10_queue.sql` из `ix`, для таблиц и view. Пример для `dm.orders`:

```
Table dm.orders
Comment: customer orders, one line per position
Columns:
  id bigint not null default nextval('dm.orders_id_seq'::regclass)
  customer_id bigint not null
  amount numeric not null -- line amount in order currency
  status dm.order_status not null default 'open'::dm.order_status
  ...
Foreign keys:
  FOREIGN KEY (customer_id) REFERENCES dm.customers(id)
Referenced by:
  dm.order_items: FOREIGN KEY (order_id, line_no) REFERENCES dm.orders(id, line_no) ON DELETE CASCADE
Indexes:
  orders__open (created_at) where (status = 'open'::dm.order_status)
  orders_pkey (id) unique
```

Для view вместо ключей и индексов блок `Reads:` с таблицами, которые она читает. Данные
таблиц в модель не идут никогда, только структура. Что в тексте участвует: имя и вид,
комментарий, оценка строк (если таблица анализировалась), граница партиции, колонки с
типом, not null, default и комментарием, внешние ключи наружу и внутрь, индексы.

## Промпт

Три файла в `prompt/`, все правятся без кода:

- `system.md`: системный промпт (роль, правила, язык, объём, запрет выдумывать).
- `user.md`: шаблон пользовательского сообщения с плейсхолдером `{input}`, куда подставляется
  текст структуры.
- `schema.json`: json-схема ответа (`SchemaSpec`: name, description, body); модель отвечает
  вызовом функции с этой схемой, воркер берёт поле `description`.

md5 трёх файлов и имени модели это `indexer_hash` в каждой строке `pg_llm_description`: правка
любого из них переводит все объекты в очередь на переописание при следующем запуске.
Смена структуры объекта меняет `input_hash`, и переописывается только он.

## Цикл воркера

1. `run/10_queue.sql` с `%(batch)s` и `%(indexer_hash)s`: объекты без описания или с другим
   `input_hash` или `indexer_hash`, с сессионными advisory-захватами по `node_id`, чтобы два
   воркера не описывали одно и то же.
2. На каждый объект `generate(user, schema)`, ответ проверяется по схеме, `run/20_write.sql`.
3. `run/90_unlock.sql` после пачки и при любой ошибке.
4. `run/30_prune.sql`: удалить описания объектов, для которых больше нет входа: node удалён
   или поверхность перестала отдавать аспект класса `describer_input`.

## Как описание попадает в поиск

Текст структуры объекта описатель не собирает сам: его отдаёт владелец поверхности аспектом
класса `describer_input` (у скрапера это `meta_describer_input`), а воркер при старте читает
объявления этого класса из `ix.surface_aspect` и подставляет их вместо `{sources}` в
`run/10_queue.sql` и `run/30_prune.sql`.

В обратную сторону описатель сам владелец аспекта: значение `llm_description` класса
`description` он добавляет в словарь `ix.aspect` при `upgrade`, а объявления в
`ix.surface_aspect` кладёт в начале каждого цикла (`run/05_declare.sql`) для каждой поверхности,
у которой есть вход. Индексаторы `pg-idx-fts` и `pg-idx-vector` подписаны на класс
`description` и подхватывают описания следующим прогоном (вес в fts задаётся в его конфиге),
`pg-idx-trgm` нет, там только `ident` и `words`. Никаких вызовов между пакетами. На стенде запрос «где хранятся позиции
заказов» до описаний не находил ничего в полнотексте, после них отдаёт `order_items` и
`orders` по аспекту llm_description; «бронирование комнат без пересечений по времени» находит
`bookings` через вектор описания.

## Первая очередь и что дальше

Сейчас описываются `pg_table` и `pg_view`. Колонки, функции и остальное добавляются тем же
механизмом: свой блок входа в `10_queue.sql` и, скорее всего, свой промпт. Цена: один вызов
модели на объект; 20 объектов стенда через deepseek заняли 67 секунд.
