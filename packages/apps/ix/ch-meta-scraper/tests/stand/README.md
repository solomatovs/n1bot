# Стенд скрапера ClickHouse

Тесты пакета ходят в живые контейнеры: источники `edge-ch-*` (ClickHouse 22.12, 23.12,
24.12, 25.12, 26.6 из локальных образов `dmp/clickhouse`), dev-кластер по kerberos и
база ix на `ix-test`. Адреса и учётки живут в секции `[ix_stand]` файла
`conf/stand.toml` рядом с конфигом приложения, в коде тестов их нет:

```toml
[ix_stand]
    db_schema = "ix"
    database  = "ix_stand"
    ch_sources = [
        { name = "ch-25.12", clickhouse = { host = "...", port = 8123, interface = "http", connect_timeout = 10, auth = { method = "password", user = "scraper", password = "scraper" }, settings = { readonly = 2, max_execution_time = 30 } } },
        { name = "ch-dev-krb", demo = false, clickhouse = { host = "...", port = 443, interface = "https", server_host_name = "ch01...", connect_timeout = 10, auth = { method = "kerberos_keytab", principal = "...", keytab = "..." } } },
    ]
```

`server/run.sh` поднимает контейнеры `edge-ch-<ver>` с `config.xml` и `users.xml` из
того же каталога: пользователь `scraper` с паролем `scraper` и `default` без пароля,
оба с правом управлять доступом. Образы без конфига не стартуют, поэтому каталог
монтируется в `/etc/clickhouse-server`.

`database` пересоздаётся на каждую сессию тестов: схема `db_schema` с ядром пакета
`ix-core`, затем `schema/` скрапера; оба накатывает `SchemaUpgrade`. На каждом источнике
с `demo = true` пересоздаётся база `edge_demo` из `ddl/`; источник с `demo = false`
снимается как есть и сверяется только по инвариантам.

- `ddl/`: демонстрационный набор, в файле ровно один statement, как принимает
  HTTP-интерфейс. Ворота по версии объявлены у файла в `DemoDataset` модуля
  `ch_scraper_stand.py`.
- `cons/consistency.sql`: инварианты структуры ix для узлов ClickHouse, каждая строка
  обязана дать 0.
- `cons/canon.sql`: канонический отпечаток одного источника по `%(host)s`: строки node,
  tree, edge, ch_meta_edge и всех поверхностей без host, port, scheme, id и того, что
  зависит от сервера, а не от набора (uuid, modified_at, total_*, create_query).
- `cons/golden.txt`: эталонные отпечатки по имени цели. Обновлять только после
  осознанного изменения раскладки или набора: прогнать `test_scrape_stand.py`, взять
  новые значения из падения и записать.

Общая часть стенда — раскладка каталога `stand/`, эталоны, база ix с инвариантами и
отпечатком, проверка ссылок и шторм — живёт в `boba.stand.scraper` (boba-stand);
модуль стенда пакета держит только модель источника и пересоздание набора.

Запуск из каталога compose (как остальные интеграционные тесты):

```
pytest packages/apps/ix/ch-meta-scraper/tests/test_scrape_stand.py -m integration
pytest packages/apps/ix/ch-meta-scraper/tests/test_scrape_storm.py -m load
```
