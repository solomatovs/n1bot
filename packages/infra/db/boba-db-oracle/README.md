# boba-db-oracle: профиль и подключение к Oracle

`OracleConfig` — профиль соединения (`kind = "oracle"`): `host`, `port`, `service`,
`connect_timeout` (сек), `call_timeout` (мс, потолок одного вызова к серверу),
`program` (подпись сессии в `v$session.program`) и `auth` с методом `password`.
Соединение идёт по имени сервиса: одна PDB или сервис экземпляра, как `dbname` у
postgres.

```toml
oracle = { host = "oracle.example.com", port = 1521, service = "orclpdb1", connect_timeout = 10, call_timeout = 30000, auth = { method = "password", user = "SCRAPER", password = "..." } }
```

`PayloadOracle` (extra `payload`, драйвер `python-oracledb` в thin-режиме — чистый
Python, клиентские библиотеки Oracle не нужны): `opened_config(profile)` открывает
соединение на время блока, `rows(conn, text, parameters)` отдаёт имена колонок и
строки потоком. Именованные bind'ы `:name`, только скалярные значения: коллекции
thin-режим не читает в старых кодировках базы (DPY-3040 на WE8DEC). Kerberos не
поддерживается: в thin-режиме его нет, а thick-режим требует Instant Client.

Ошибки пакета: `OracleError` (до базы не достучаться) и `OracleQueryError` (сервер
отклонил запрос или оборвал чтение).
