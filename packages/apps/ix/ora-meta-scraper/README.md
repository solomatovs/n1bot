# ora-meta-scraper: снятие словаря Oracle в граф ix

Пакет SQL-файлов для общего загрузчика `boba.ix_core.scrape`. Обвязка на Python
выполняет файлы по порядку имён и держит одно thin-соединение python-oracledb на
попытку. В текст SQL она не заглядывает и ничего в нём не подменяет. Проверено на
Oracle 12.2 (EE), 18 и 21 (XE), 23 (Free) — стенд в `tests/stand/`.

```
schema/   DDL объектов, которыми владеет скрапер: значения surface_e и aspect_e, словари, ora_meta_edge, surface-таблицы, объявления аспектов, промпты
scrape/   запросы к источнику, один файл на системную таблицу словаря, результат в raw_<name>
layout/   запросы к своей базе: raw_* -> stage_* -> ix
```

Схема пакета накатывается его же командой; ядро `ix` ставится до этого пакетом
`ix-core`:

```
.venv/bin/boba-ix-core upgrade --config ../../compose/apps/ix-core/conf.toml
.venv/bin/boba-ora-meta-scraper upgrade --config ../../compose/apps/ora-meta-scraper/conf.toml
```

## 0. Воркер

Настройки берутся из одного файла конфига, секция `[ix.ora_meta_scraper]`: база ix
(`db_schema`, `postgres`, `krb`, как у всех приложений ix), `attempts` и список
`sources`. У источника `name` и профиль `oracle` из boba-db-oracle: `host`, `port`,
`service` (PDB или сервис экземпляра), `connect_timeout` (сек), `call_timeout` (мс,
потолок одного вызова к серверу), `program` (подпись в `v$session.program`) и `auth`
с методом `password`. Kerberos не поддерживается: thin-режим драйвера его не умеет, а
клиентские библиотеки Oracle в проект не берутся.

```toml
[ix.ora_meta_scraper]
    db_schema = "ix"
    attempts  = 3
    sources = [
        { name = "ora-prod", oracle = { host = "db01.example.com", port = 1521, service = "orclpdb1", connect_timeout = 10, call_timeout = 30000, program = "ora-meta-scraper", auth = { method = "password", user = "scraper", password = "..." } } },
    ]
```

```
.venv/bin/boba-ora-meta-scraper run --config ../../compose/apps/ora-meta-scraper/conf.toml [--source ora-prod]
```

Один прогон это один сервис: соединение с Oracle идёт в PDB или сервис экземпляра,
как в базу у postgres, поэтому scope источника это `scheme, host, port, database`
(`database` = имя сервиса), корень tree — база, схемы под ней. Две PDB одного
сервера это два источника.

## 1. Права источника

Словарь читается не через `dba_*` или `all_*`, а напрямую из системных таблиц
`SYS.*$`, как pg-meta-scraper читает `pg_catalog`: без ролей, представлений и
функций, только точечные гранты на каждую таблицу. Пользователю скрапера нужны
`create session` и `select` на 21 таблицу (`tests/stand/server/grants.sql`):

```sql
create user scraper identified by "...";
grant create session to scraper;
grant select on sys.user$ to scraper;
grant select on sys.obj$ to scraper;
grant select on sys.tab$ to scraper;
grant select on sys.col$ to scraper;
grant select on sys.com$ to scraper;
grant select on sys.con$ to scraper;
grant select on sys.cdef$ to scraper;
grant select on sys.ccol$ to scraper;
grant select on sys.ind$ to scraper;
grant select on sys.icol$ to scraper;
grant select on sys.view$ to scraper;
grant select on sys.snap$ to scraper;
grant select on sys.seq$ to scraper;
grant select on sys.syn$ to scraper;
grant select on sys.trigger$ to scraper;
grant select on sys.dependency$ to scraper;
grant select on sys.partobj$ to scraper;
grant select on sys.partcol$ to scraper;
grant select on sys.ts$ to scraper;
grant select on sys.registry$ to scraper;
grant select on sys.props$ to scraper;
```

`dba_*` такому пользователю недоступны (ORA-00942), к данным таблиц доступа нет.
В CDB гранты выдаются в той PDB, к которой идёт соединение.

## 2. Прогон одного источника

Общий цикл описан в `boba.ix_core.scrape`; здесь только то, что делает источник.

1. Соединение по профилю, версия словаря из `sys.registry$` (`cid = 'CATALOG'`);
   версия сравнивается с воротами объявлений `min_version`/`max_version` по длине ворот.
2. Файлы `scrape/`, объявленные в `OraSource.files`, по волнам. Параметров у файлов нет:
   границы записаны в самом запросе. Схемы — подзапрос по `sys.user$`: все пользователи,
   не принадлежащие Oracle (`bitand(user$.spare1, 256) = 0`, так же считает
   `dba_users.oracle_maintained`); объекты — обычные объекты `sys.obj$`, без подобъектов
   и удалённых (`subname`, `linkname`, `remoteowner` пусты, `bitand(flags, 128) = 0`).
   Файл читается как есть, и запрос, и его сверка. Ответ едет пачками Arrow
   по `arraysize` строк (`fetch_df_batches`: драйвер декодирует ответ в Cython, минуя
   Python-объекты), pyarrow пишет пачку в CSV, и байты уходят в
   `COPY ... FROM STDIN (format csv, null '')` без разбора строк в Python. Типы колонок
   берутся у драйвера пустой пробой запроса, NUMBER без объявленной точности (все
   колонки `sys.*$`) запрашивается `decimal128(38, 0)` — целые точны при любой ширине,
   дробный NUMBER без точности запрос обязан привести сам (`to_char`, `number(p, s)`).
   DATE и TIMESTAMP в ISO, NULL пустым полем, RAW запрос отдаёт `rawtohex`. Сессия в
   `time_zone = 'UTC'` и с `nls_numeric_characters = '.,'`, чтобы `to_char` дат и чисел в
   запросах не зависел от NLS сервера. Каждый результат целиком уходит в `raw_<name>`.
3. После последнего файла ещё раз все запросы сверки `<файл>.verify.sql`: ключ строки и
   `row_version`.
   xmin у Oracle нет, поэтому `row_version` это `standard_hash` структурных колонок
   той же строки; у объекта в хэш входит `obj$.mtime` (last_ddl_time), поэтому любой
   ALTER даёт расхождение и повтор, даже если поменялось поле LONG, которое в хэш не
   влезает (default колонки, текст представления, условие check).
4. Дальше стадии `layout/` и apply ровно как у pg-meta-scraper: temp-таблицы, стадии в
   autocommit, advisory-замок на scope, `50_apply.sql` в repeatable read, сводка
   planned/applied.

## 3. Контракт файлов scrape

В файле `scrape/<имя>.sql` лежит только запрос выборки (последняя колонка `row_version`),
рядом в `scrape/<имя>.verify.sql` запрос сверки (ключ и `row_version`, те же фильтры).
Остальное объявляет `OraSource.files` в `worker.py`:

```python
ScrapeFile(
    name="objects",              # имя таблицы: raw_objects
    wave=2,                      # порядок выполнения
    query="2_objects.sql",       # файл запроса, сверка в 2_objects.verify.sql
    min_version=(12, 1),         # версия словаря, с которой вариант применим
    max_version=(19,),           # и по которую; по длине ворот: 19.3 проходит (19,)
)
```

Варианты одного имени различаются суффиксом `__…` файла и воротами; подходит ровно один.
Колонки словаря с `#` и `$` в имени приходят под своими именами: `obj#` -> `obj_id`,
`col#` -> `col_id`, `intcol#` -> `intcol_id`, `type#` -> `type_id`, `default$` ->
`default_text`, `comment$` -> `comment_text`. Числа драйвер отдаёт Decimal, битовые
поля (`property`, `flags`) шире bigint и в raw лежат numeric.

## 4. Контракт файлов layout

| файл | где выполняется | что делает |
|---|---|---|
| `00_raw_schema.sql` | до COPY, autocommit | `create temp table raw_<name>` под каждый scrape-файл и `raw_source` |
| `10_stage.sql` | autocommit | temp `stage_node`, `stage_tree`, `stage_edge` |
| `20_nodes.sql` | autocommit | адреса всех node из `raw_*`; ключ (kind, obj_id, sub_id) -> address; `stage_alias` obj# -> address |
| `30_tree.sql` | autocommit | родитель каждого node |
| `40_edges.sql` | autocommit | все рёбра с role, side, ordinal, is_key |
| `45_surfaces.sql` | autocommit | temp `stage_ora_meta_<surface>`: свойства node из `raw_*` через `stage_node`, битовые поля разложены |
| `48_lock.sql` | autocommit, до begin | `pg_advisory_lock` на хэш адреса сервиса |
| `50_apply.sql` | в транзакции, единственный | замок на срез `ix` по scope, delete лишних и изменившихся, insert недостающих; собран из `schema/30_ora_meta_surfaces.sql` |
| `55_unlock.sql` | autocommit | `pg_advisory_unlock_all` |

Адрес node это jsonb: scheme, host, port, database (сервис), schema и один из table,
view, mview, sequence, synonym, routine; column, constraint, index, trigger добавляются
к адресу владельца. Ключ node это address. Направление ребра: src зависит от tgt.
Роли рёбер в `ix.ora_meta_edge (edge_id, role, side, ordinal, is_key)` как у
pg_meta_edge: index и constraint перечисляют колонки по позиции (`side = 1` у целевых
колонок FK), partition_key перечисляет колонки ключа партиционирования, dependency
(по `dependency$`) и synonym позиции не имеют.

Битовые поля раскладываются формулами из определений `dba_*` того же сервера: бит k
поля x это `mod(floor(x / 2^k), 2) = 1`. Что именно: `tab$.property` 32 partitioned,
64 IOT, 512 IOT overflow, 8192 nested, 2^26 контейнер mview; `obj$.flags` 2 temporary,
128 dropped (recycle bin); `col$.property` 8 virtual, 32 hidden, 2^37 и 2^38 identity,
131072 DESC в индексе; `ind$.property` 1 unique, 4 reverse, 16 function-based;
`ind$.flags` 1 unusable; `cdef$.defer` 1 deferrable, 4 validated; `icol$.spare1` 1
выражение; `user$.spare1` 256 oracle_maintained.

## 5. Что снимается

| таблица словаря | node | surface |
|---|---|---|
| `registry$`, `props$`, `sys_context` | база, корень | `ora_meta_database`: host, port, service, con_name, db_name, version, charset |
| `user$` без бита 256 | схема | `ora_meta_schema`: name, created |
| `obj$` type# 2 + `tab$` (кроме overflow IOT, вложенных и контейнеров mview) | таблица | `ora_meta_table`: tablespace, partitioned, partition_type, temporary, iot, num_rows, comment, status, created, last_ddl_time |
| `obj$` type# 4 + `view$` | представление | `ora_meta_view`: text, comment, status, created, last_ddl_time |
| `snap$` + контейнерная таблица | mview одной node | `ora_meta_mview`: query, refresh_mode, comment, status, created, last_ddl_time |
| `col$` без скрытых (бит 32) + `com$` | колонка таблицы, представления или mview | `ora_meta_column`: ordinal, data_type, data_length, data_precision, data_scale, nullable, default_text, virtual, identity, comment |
| `con$` + `cdef$` type# 1–6 + `ccol$` | constraint под таблицей или представлением | `ora_meta_constraint`: kind (C/P/U/R/V/O), search_condition, ref_schema, ref_constraint, delete_rule, enabled, validated, is_deferrable |
| `ind$` кроме LOB (8) и nested IOT (5) + `icol$` | индекс под таблицей или mview | `ora_meta_index`: index_type, is_unique, tablespace, columns, status, created, last_ddl_time |
| `seq$` | последовательность | `ora_meta_sequence`: min_value, max_value, increment_by, cycle, ordered, cache_size |
| `syn$` | синоним | `ora_meta_synonym`: target_schema, target_name, db_link |
| `trigger$` | триггер под таблицей или представлением | `ora_meta_trigger`: trigger_type, event, enabled, status |
| `obj$` type# 7, 8, 9, 13 | подпрограмма или тип под схемой | `ora_meta_routine`: kind (PROCEDURE, FUNCTION, PACKAGE, TYPE), status, created, last_ddl_time |
| `partobj$`, `partcol$` | — | роли рёбер partition_key |
| `dependency$` | — | роли рёбер dependency между объектами схем |

Не снимаются: тела пакетов и функций, аргументы подпрограмм, партиции поимённо, права,
размеры сегментов, статистика колонок, db links, not null как constraint (type# 7).
`num_rows` берётся из `tab$.rowcnt` (статистика, не скан) и в `row_version` не входит.

## 6. Стенд и golden

Стенд, инварианты, отпечатки и шторм описаны в `tests/stand/README.md`. Матрица того,
что отдали версии, в `tests/stand/versions.md`.

Потоковость проверяет `tests/test_ora_streaming_memory.py` (`-m load`): демо-набор и тот же
набор с двумя тысячами таблиц по десять колонок, ключом, комментарием и индексом
снимаются каждый в своём процессе, и пик RSS процесса не должен расти с объёмом словаря.
Пик берётся из `VmHWM` в `/proc/self/status`: `ru_maxrss` spawn-потомок наследует от
родителя на момент fork и для сравнения детей не годится. Замер 2026-09-22 на 12.2, 18,
21 и 23: 139 MiB при демо-словаре и 142 MiB при 106 000 применённых строках. Строки идут
из курсора Oracle пачками по `arraysize` профиля (на стенде 2000) в `COPY` CSV-блоками, в память они не
собираются. Драйвер — python-oracledb `26.0.0+boba.1`: колесо с патчем из
`build/chainlit/scripts/oracledb-26.0.0-arrow-duplicates.patch` (два дефекта Arrow-пути
в 26.0.0: разбор строки после `OutOfPackets` с колонкой-дубликатом и decimal-ветка
`append_last_value`), собирает стадия `oracledb-wheel` (`make fetch`), в `.venv` его
ставит `dev.sh`; разбор — `docs/bulk-copy-formats.md` §4.
