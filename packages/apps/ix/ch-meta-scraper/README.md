# ch-meta-scraper: снятие каталога ClickHouse в граф ix

Пакет SQL-файлов для общего загрузчика `boba.ix_core.scrape`. Обвязка на Python
выполняет файлы по порядку имён, передаёт массивы имён серверными параметрами
`{dbs:Array(String)}` и держит один HTTP-клиент на попытку. В текст SQL она не
заглядывает и ничего в нём не подменяет. Проверено на ClickHouse 22.12, 23.12, 24.12,
25.12, 26.6 (стенд в `tests/stand/`) и на dev-кластере 26.3 по kerberos.

```
schema/   DDL объектов, которыми владеет скрапер: значения surface_e и aspect_e, словари, ch_meta_edge, surface-таблицы, объявления аспектов, промпты
scrape/   запросы к источнику, один файл на system-таблицу, результат в raw_<name>
layout/   запросы к своей базе: raw_* -> stage_* -> ix
```

Схема пакета накатывается его же командой; ядро `ix` ставится до этого пакетом
`ix-core`:

```
.venv/bin/boba-ix-core upgrade --config ../../compose/apps/ix-core/conf.toml
.venv/bin/boba-ch-meta-scraper upgrade --config ../../compose/apps/ch-meta-scraper/conf.toml
```

## 0. Воркер

Настройки берутся из одного файла конфига, секция `[ix.ch_meta_scraper]`: база ix
(`db_schema`, `postgres`, `krb`, как у всех приложений ix), `attempts` и список
`sources`. У источника `name` и профиль `clickhouse` из boba-db-clickhouse: host, port,
`interface` (http или https), `auth` с методом (`no_password`, `password`,
`certificate`, `kerberos_keytab`, `kerberos_password`), `settings` сессии. Для kerberos
нужен `connect_timeout`, а хост в SPN берётся из `server_host_name`, если подключение
идёт по адресу.

```toml
[ix.ch_meta_scraper]
    db_schema = "ix"
    attempts  = 3
    sources = [
        { name = "ch-prod", clickhouse = { host = "ch01.example.com", port = 8443, interface = "https", connect_timeout = 10, client_name = "ch-meta-scraper", auth = { method = "kerberos_keytab", principal = "svc@REALM", keytab = "/etc/krb/svc.keytab" }, settings = { readonly = 2, max_execution_time = 30 } } },
    ]
```

```
.venv/bin/boba-ch-meta-scraper run --config ../../compose/apps/ch-meta-scraper/conf.toml [--source ch-prod]
```

Один прогон это один сервер: в ClickHouse подключение идёт к серверу, а не к базе,
поэтому scope источника это `scheme, host, port`, корень tree — сервер, базы под ним.
Кластер скрапер не знает: что передали адресом, то и снимает. Роль источника читает
`system.*` и должна видеть базы, которые нужно снять (`SHOW TABLES`/`SHOW COLUMNS`
на них); писать в ix ходит роль профиля `postgres` секции.

`settings.readonly = 2` держит сессию только на чтение (1 запретил бы клиенту
передавать свои настройки), `max_execution_time` ограничивает каждый запрос.
`attempts` — число попыток при изменении каталога во время чтения или занятом ix.

## 1. Прогон одного источника

Общий цикл описан в `boba.ix_core.scrape`; здесь только то, что делает источник.

1. Клиент по профилю, `select version()`; версия сравнивается с воротами файлов.
2. Файлы `scrape/`, объявленные в `ChSource.files`, по волнам. Для каждого имени
   выбирается единственный вариант, чьи ворота `min_version`/`max_version` подходят под
   версию.
   Волна 1 снимает сервер и базы и собирает массив `dbs` имён баз без `system` и
   `INFORMATION_SCHEMA`; волна 2 снимает таблицы, колонки, индексы, проекции, словари и
   SQL-функции по этому массиву. Ответ клиент отдаёт потоком `TabSeparated`
   (`raw_stream`), и блоки байт уходят в `COPY ... FROM STDIN (format text)` без разбора
   строк в Python. Сессия запроса: `session_timezone = 'UTC'` (сервер от 23),
   `output_format_tsv_crlf_end_of_line = 0`, `prefer_column_name_to_alias = 1` — иначе
   алиас `toJSONString(col) as col` подменяет колонку внутри `sipHash64`. Массивы
   (`dependencies_*`, ключи и атрибуты словарей) файлы отдают `toJSONString` в колонки
   `jsonb`, раскладка читает их `jsonb_array_elements_text`; `arrayStringConcat` для
   `sorting_key` проекций. Каждый результат целиком уходит в `raw_<name>`.
3. После последнего файла ещё раз все запросы сверки `<файл>.verify.sql`: ключ строки и
   `row_version`.
   В ClickHouse нет xmin и одного снимка на все system-таблицы, поэтому `row_version`
   это `hex(sipHash64(tuple(<структурные колонки>)))` той же строки: ALTER, RENAME,
   смена комментария или пересоздание с новым uuid между чтением и сверкой дают
   расхождение и повтор прогона. Изменчивые поля (`total_rows`, `total_bytes`,
   `metadata_modification_time`) в хэш не входят: таблица под вставками иначе никогда
   не сошлась бы.
4. Дальше стадии `layout/` и apply ровно как у pg-meta-scraper: temp-таблицы, стадии в
   autocommit, advisory-замок на scope, `50_apply.sql` в repeatable read, сводка
   planned/applied.

## 2. Контракт файлов scrape

В файле `scrape/<имя>.sql` лежит только запрос выборки (последняя колонка `row_version`),
рядом в `scrape/<имя>.verify.sql` запрос сверки (ключ и `row_version`, те же фильтры и
параметры). Остальное объявляет `ChSource.files` в `worker.py`:

```python
ScrapeFile(
    name="tables",                                  # имя таблицы: raw_tables
    wave=2,                                         # порядок выполнения
    query="2_tables.sql",                           # файл запроса, сверка в 2_tables.verify.sql
    params=("dbs",),                                # какие массивы нужны: {dbs:Array(String)}
    collect=Collect(name="dbs", column="name"),     # массив dbs из колонки name результата
    min_version=(24, 4),                            # версия сервера, с которой вариант применим
    max_version=(26, 5),                            # и по которую; 26.5.3 проходит (26, 5)
)
```

Варианты одного имени различаются суффиксом `__…` файла и воротами; подходит ровно один.
Все варианты отдают одинаковые колонки: чего нет в старой версии, приходит
`cast(null, 'Nullable(String)')`. Новая версия ClickHouse с новыми колонками это новый
вариант файла и, если колонка нужна поверхности, новая колонка raw-таблицы и
surface-таблицы; обвязку править не нужно.

Колонка `table` system-таблиц приходит как `table_name`, а `key.names` словаря как
`key_names`: в raw-таблицах эти имена не нужно квотировать.

## 3. Что снимается

| system-таблица | node | surface |
|---|---|---|
| `version()` | сервер, корень | `ch_meta_server`: host, port, version |
| `databases` | база | `ch_meta_database`: engine, engine_full, uuid, comment |
| `tables`, кроме View*/Dictionary | таблица | `ch_meta_table`: engine, engine_full, ключи partition/sorting/primary/sampling, storage_policy, total_rows, total_bytes, comment, create_query, modified_at |
| `tables` с движком View, MaterializedView, LiveView, WindowView | представление | `ch_meta_view`: kind, engine_full, as_select, ключи, target_database/target_table (с 26.6), comment, create_query, modified_at |
| `columns` | колонка таблицы, представления или словаря | `ch_meta_column`: ordinal, data_type, default_kind, default_expression, codec, флаги in_*_key, comment |
| `data_skipping_indices` | индекс под таблицей | `ch_meta_index`: kind, kind_full, expr, granularity |
| `projections` (с 24.4) | проекция под таблицей | `ch_meta_projection`: kind, sorting_key, query |
| `dictionaries` с непустой database | словарь | `ch_meta_dictionary`: origin, layout, key_*, attribute_*, source, lifetime, comment, create_query; layout, source и lifetime сервер заполняет только у загруженного словаря, до первого обращения они пусты, полное объявление всегда в create_query |
| `functions` с origin SQLUserDefined | функция под сервером | `ch_meta_function`: create_query |

Тексты каталога лежат как есть: `create_table_query`, `as_select`, выражения ключей и
индексов, источник словаря. По ним объект восстанавливается, а связи, которых нет в
system-таблицах структурно (цель `TO` у представления до 26.6, таблица у `Distributed`,
таблицы в тексте запроса), достаёт из текста описатель, не скрапер.

`total_rows` и `total_bytes` сервер берёт из метаданных частей, а не сканом, поэтому они
снимаются; из-за них строка surface активной таблицы меняется на каждом прогоне, это
удаление плюс вставка одной строки.

## 4. Рёбра

Направление: src зависит от tgt. У каждого ребра роль в `ix.ch_meta_edge` с ключом
`(edge_id, role)`: позиции колонки в ключе каталог не отдаёт, только флаги.

| роль | src | tgt | откуда |
|---|---|---|---|
| partition_key, sorting_key, primary_key, sampling_key | таблица или представление | колонка | `columns.is_in_*_key` |
| dependency | материализованное представление | таблица, которую оно читает | `tables.dependencies_*` |
| loading | объект | без чего он не загрузится: целевая таблица представления, таблица-источник словаря | `tables.loading_dependencies_*` |
| target | материализованное представление | целевая таблица | `tables.target_*` (с 26.6) |

Ссылки в списках зависимостей на объекты вне снятых баз (system) отбрасываются: у
ребра обе вершины должны быть в стадии.

## 5. Что не снимается

- Словари из xml-конфига (`database = ''`): у них нет адреса под базой.
- Временные таблицы, `system` и `INFORMATION_SCHEMA`.
- Колонки индексов и проекций: в каталоге только текст выражения.
- Настройки таблиц (`SETTINGS`), TTL, кодеки на уровне таблицы отдельно от
  `create_table_query`: они есть в его тексте.
- Права, квоты, профили, размеры частей, статистика запросов.

## 6. Стенд

`tests/stand/server/run.sh` поднимает `edge-ch-<ver>` из локальных образов
`dmp/clickhouse` с конфигом из того же каталога (пользователь `scraper`/`scraper`,
`default` без пароля). Адреса целей в `[ix_stand].ch_sources` файла
`compose/chainlit/conf/stand.toml`; источник с `demo = false` (dev-кластер по kerberos)
снимается как есть, без пересоздания набора. Набор `edge_demo` в `tests/stand/ddl/`,
инварианты и отпечатки в `tests/stand/cons/`.

```
pytest packages/apps/ix/ch-meta-scraper/tests/test_scrape_stand.py -m integration
pytest packages/apps/ix/ch-meta-scraper/tests/test_scrape_storm.py -m load
```

Шторм: 60 одновременных задач по 3 прогона по случайным целям (все шесть источников,
включая kerberos) в одну базу ix, поверх задача, каждые 200 мс обрывающая две случайные
сессии ix. Итог на стенде: 180 прогонов, около трети оборваны killer'ом (ошибки только
`AdminShutdown` и `server closed the connection`), deadlock 0, инварианты после шторма и
после контрольного прохода 0, отпечатки всех scope равны эталону.
