# Стенд скрапера

Тесты пакета ходят в живые контейнеры: источники `edge-pg-*` (PostgreSQL 9.0–19) и
`edge-gp-*` (Greenplum 6 и 7) плюс база ix на `ix-test`. Адреса и учётки живут в секции
`[ix_stand]` файла `conf/stand.toml` рядом с конфигом приложения, в коде тестов их нет:

```toml
[ix_stand]
    db_schema = "ix"
    ix_dsn   = "host=... port=5432 dbname=postgres user=... password=..."
    database = "ix_stand"
    sources  = [
        { name = "pg-18", dsn = "host=... port=5432 user=... password=..." },
        { name = "gp-7",  dsn = "host=... port=5432 user=gpadmin" },
    ]
```

`database` пересоздаётся на каждую сессию тестов: схема `db_schema` с ядром пакета
`pg-ix-core`, затем `schema/` скрапера; оба накатывает `SchemaUpgrade`. На каждом источнике пересоздаётся база `edge_demo` из `ddl/`.

- `ddl/`: демонстрационный набор. Файл с воротами `-- @min`, `-- @max`, `-- @only gp`,
  `-- @not gp` применяется только к подходящей версии; варианты одной таблицы различаются
  буквой в имени (identity/serial/greenplum, partition 11/10/inherits и т. д.).
- `cons/consistency.sql`: инварианты структуры ix, каждая строка обязана дать 0.
- `cons/canon.sql`: канонический отпечаток одного источника по `%(host)s`: строки node,
  tree, edge, pg_meta_edge и всех поверхностей без host, database, port, scheme и id,
  отсортированы, md5. Отпечаток набора одинаков в любой базе и при любом scope.
- `cons/golden.txt`: эталонные отпечатки по имени цели. Обновлять только после осознанного
  изменения раскладки или набора: прогнать `test_scrape_stand.py`, взять новые значения
  из падения и записать.
- `versions.md`: сколько строк каждая версия отдала на снятии и как они легли в ix.

Запуск из каталога compose (как остальные интеграционные тесты):

```
pytest packages/apps/ix/pg-meta-scraper/tests/test_scrape_stand.py -m integration
pytest packages/apps/ix/pg-meta-scraper/tests/test_scrape_storm.py -m load
```
