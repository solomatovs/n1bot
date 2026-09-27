# boba-tool-oracle: инструменты Oracle

Плагин `boba.tools` с секцией `[tool.ora]`, соединение пользователя приходит
профилем `OracleConfig` из `boba-db-oracle` (параметр `Annotated[OracleConfig,
UserConnection]`). Тело инструмента живёт в песочнице: драйвер `python-oracledb` и
`pyarrow` объявлены в extra `payload`, в окружении приложения их нет, модуль
импортирует их только внутри вызова.

## Права учётки источника

Словарь читается через представления `all_*`: видно то, к чему у учётки есть
доступ, и никаких грантов сверх `create session` не нужно. Обратная сторона: объект
без `grant select` в выдаче не появится. Для полной картины словаря независимо от
прав есть скрапер `ora-meta-scraper`, он читает `SYS.*$` по точечным грантам.

## Инструменты

| инструмент | что делает |
|---|---|
| `ora_query` | одна команда SQL или блок PL/SQL; выборка окном offset/limit, DML и DDL — счётчик строк, фиксация сразу |
| `ora_list_tables`, `ora_describe_table` | быстрый осмотр: таблицы, представления и mview схемы; колонки таблицы с комментариями |
| `ora_database_describe` | сервис, контейнер, баннер версии, кодировка; грантов не требует |
| `ora_schema_describe`, `ora_table_describe`, `ora_column_describe`, `ora_constraints_describe`, `ora_indexes_describe`, `ora_routines_describe`, `ora_sequences_describe`, `ora_types_describe` | описание словаря по `all_*`; `*` в фильтре схемы скрывает служебные схемы Oracle |
| `ora_address` | базовый url соединения `oracle://host:port/service`, роли объекта в query |
| `ora_stream_out` | насос выгрузки: строки запроса потоком Arrow IPC в выходной порт с контрактом колонок для приёмника |
| `ora_stream_in` | насос загрузки: поток Arrow любого источника в таблицу со стратегиями схемы, удаления и вставки |

Окно `offset`/`limit` режется на стороне инструмента (`RowPage`), поэтому `offset
... fetch` в запрос подставлять не нужно и оно работает на любой версии сервера.
Команда пишется без хвостовой `;` (Oracle её не принимает), у блока PL/SQL `;`
часть синтаксиса; инструмент в текст не заглядывает.

## Насосы перекачки

У Oracle нет серверного текстового потока, поэтому оба насоса работают на Arrow:
драйвер python-oracledb отдаёт и принимает пачки Arrow напрямую, значения в
Python не разбираются.

- `ora_stream_out(sql, columns)` разбирает стейтмент на сервере (`parse`, без
  выполнения) и шлёт первым кадром контракт колонок: типы, точность, `null_ok`,
  тексты типов Oracle; декларации `columns` ложатся поверх. Дальше пачки
  драйвера по `arraysize` строк уходят в порт как есть. Типы, которые драйвер в
  Arrow не отдаёт или отдаёт с потерей (INTERVAL, XMLTYPE, JSON, VECTOR, ROWID,
  TIMESTAMP WITH TIME ZONE), отвергаются до выполнения с подсказкой, чем их
  привести в `select`. Имена колонок — заглавные, как у Oracle; строчные — алиас
  в кавычках.
- `ora_stream_in(schema_name, table_name, schema_strategy, delete_strategy,
  insert_strategy, rules, unknown_types, create_table, chunk_bytes, before, after)`
  сверяет контракт потока с таблицей по `all_tab_columns`, создаёт или
  пересоздаёт её по шаблону `create_table` и кладёт пачки одной командой
  `executemany` на пачку. Целые ложатся `NUMBER(p)`, decimal — `NUMBER(p, s)`,
  строки — `VARCHAR2(n CHAR)` или `CLOB`, моменты — `TIMESTAMP(p)`, uuid и time
  — строками, boolean — `BOOLEAN` на 23 и `NUMBER(1)` раньше. Колонки LOB в
  insert ставятся последними сами (ORA-24816), сессия переводится в UTC. DDL
  Oracle фиксирует сам; удаление и вставка вместе с `before` и `after` — одна
  транзакция.

Скорость на стенде: выгрузка 360–400 тысяч строк в секунду, загрузка 150 тысяч
на Oracle 23 и 30 тысяч на 12.2.

Матрицы всех типов против PostgreSQL и ClickHouse, стратегии приёмника и отказы
источника — в `packages/testing/boba-pump-stand` (`test_ora_sync.py`,
`test_pg_arrow.py`, `test_ch_arrow.py`, `test_arrow_ch_sync.py`), сводка в
`docs/etl_skill.md`.

## Стенд

Тесты ходят в источники `[ix_stand].ora_sources` файла `conf/stand.toml`: профиль
`oracle` с минимальными правами и профиль `admin`, которым пересоздаётся схема
`TOOL_DEMO` (таблицы с ключами и комментариями, представление, последовательность,
функция, таблица-приёмник). Запуск из каталога compose:

```
pytest packages/tools/boba-tool-oracle/tests -m integration
```
