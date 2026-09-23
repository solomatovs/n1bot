# boba-db-oracle: тип соединения и клиент Oracle

Пакет объявляет два entry point'а: `boba.connections` (тип `oracle`: модель профиля
и probe для кнопки «Check») и `boba.addresses` (семейство адресов `OraAddresses`).
Снимок для каталога (`boba.catalog`) не объявлен.

## Профиль

`OracleConfig` (`kind = "oracle"`): `host`, `port`, `service` (имя сервиса: PDB или
сервис экземпляра, как `dbname` у postgres), `connect_timeout` (сек), `call_timeout`
(мс, потолок одного вызова к серверу), `arraysize` (строк за fetch и в пачке CSV),
`program` (подпись сессии в `v$session.program`) и `auth`.

Способы авторизации — union по `method`, производные ключи задаёт сам вариант:

| method | поля | протокол |
|---|---|---|
| `password` | `user`, `password` | tcp |
| `wallet` | `user`, `password`, `wallet_location` (каталог с `ewallet.pem`), `wallet_password`, `ssl_server_dn_match` | tcps |

```toml
oracle = { host = "db1.example.com", port = 1521, service = "orclpdb1", connect_timeout = 10, call_timeout = 30000, arraysize = 2000, auth = { method = "password", user = "SCRAPER", password = "..." } }
oracle = { host = "adb.example.com", port = 1522, service = "adb_high", connect_timeout = 10, call_timeout = 30000, arraysize = 2000, auth = { method = "wallet", user = "ADMIN", password = "...", wallet_location = "/etc/oracle/wallet", wallet_password = "...", ssl_server_dn_match = true } }
```

Kerberos и внешняя аутентификация не поддерживаются: в thin-режиме драйвера их нет, а
клиентские библиотеки Oracle (thick-режим) в проект не берутся.

## Адреса

`oracle://host:port/service?schema=HR&table=EMPLOYEES&column=EMAIL` — порт явный (в
строке может быть опущен, тогда 1521), path это сервис, роли объекта в query. Роли:
`schema`, одна из `table`, `view`, `mview`, `sequence`, `synonym`, `routine`, и
вложенные `column` (под table, view, mview), `constraint` (под table, view), `index`
(под table, mview), `trigger` (под table, view). Грамматика совпадает с формулами
ссылок `ora-meta-scraper`, поэтому адрес из каталога и адрес инструмента это одна
строка.

## Клиент

`PayloadOracle(profile)` (extra `payload`, драйвер `python-oracledb` в thin-режиме,
пакет `pyarrow`): `opened()` открывает соединение на время блока; `rows(conn, text,
parameters)` отдаёт имена колонок и строки потоком, у команды без выборки — число
затронутых строк; `csv(conn, text)` отдаёт CSV-байты пачками Arrow по `arraysize`;
`column_types(conn, text)` — семейства типов колонок по описанию курсора;
`executemany(conn, text, kinds, rows)` — пачка строк одной поездкой с явным типом
bind'а для `TIMESTAMP` и бинарных колонок; `commit(conn)`.

Именованные bind'ы `:name`, только скалярные значения: коллекции thin-режим не
читает в старых кодировках базы (DPY-3040 на WE8DEC). NUMBER приходит Decimal, LOB
строками и байтами.

Ошибки пакета: `OracleError` (до базы не достучаться), `OracleQueryError` (сервер
отклонил запрос или оборвал чтение), `AddressError` (строка не адрес Oracle).
