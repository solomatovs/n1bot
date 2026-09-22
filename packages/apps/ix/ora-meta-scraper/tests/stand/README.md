# Стенд скрапера Oracle

Тесты пакета ходят в живые контейнеры: источники `edge-ora-18`, `edge-ora-21`,
`edge-ora-23` (образы `gvenzl/oracle-xe` и `gvenzl/oracle-free`), контейнер `oracle`
12.2 из `compose/oracle` и база ix на `ix-test`. Адреса и учётки живут в секции
`[ix_stand]` файла `conf/stand.toml` рядом с конфигом приложения, в коде тестов их нет:

```toml
[ix_stand]
    db_schema = "ix"
    database  = "ix_stand"
    ora_sources = [
        { name = "ora-23", oracle = { host = "...", port = 1521, service = "FREEPDB1", connect_timeout = 10, call_timeout = 30000, auth = { method = "password", user = "scraper", password = "scraper" } }, admin = { host = "...", port = 1521, service = "FREEPDB1", connect_timeout = 10, call_timeout = 120000, auth = { method = "password", user = "system", password = "oracle" } } },
    ]
```

`oracle` это профиль скрапера: пользователь `scraper` с точечными грантами на
системные таблицы словаря (`server/grants.sql`, никаких ролей и `dba_*`). `admin` это
профиль администратора, которым тест пересоздаёт схему `EDGE_DEMO` (`system`);
источник с `demo = false` снимается как есть и сверяется только по инвариантам.

`server/run.sh` поднимает контейнеры `edge-ora-<ver>` с пользователем `scraper` и
выдаёт ему гранты; контейнеру `oracle` 12.2 гранты выдаются тем же `grants.sql`
через `sqlplus / as sysdba` в PDB `ORCLPDB1`.

`database` пересоздаётся на каждую сессию тестов: схема `db_schema` с ядром пакета
`ix-core`, затем `schema/` скрапера; оба накатывает `SchemaUpgrade`.

- `ddl/`: демонстрационный набор в схеме `EDGE_DEMO`: таблицы с identity, виртуальной
  колонкой, PK/UK/FK/check, партиционированная таблица с локальными индексами, IOT,
  временная таблица, представления (с check option), mview с индексом, синонимы,
  триггеры (в том числе instead of), функция, процедура, пакет, тип. Statement'ы
  разделены строкой `/`, как в sqlplus; файл с воротами `-- @min`, `-- @max`
  применяется только к подходящей версии.
- `cons/consistency.sql`: инварианты структуры ix для узлов Oracle, каждая строка
  обязана дать 0.
- `cons/canon.sql`: канонический отпечаток одного источника по `%(host)s`: строки node,
  tree, edge, ora_meta_edge и всех поверхностей без host, port, scheme, database, id и
  того, что зависит от сервера, а не от набора (версия, кодировка, даты, статистика).
- `cons/golden.txt`: эталонные отпечатки по имени цели. Обновлять только после
  осознанного изменения раскладки или набора: прогнать `test_scrape_stand.py`, взять
  новые значения из падения и записать.

Запуск из каталога compose (как остальные интеграционные тесты):

```
pytest packages/apps/ix/ora-meta-scraper/tests/test_scrape_stand.py -m integration
pytest packages/apps/ix/ora-meta-scraper/tests/test_scrape_storm.py -m load
```
