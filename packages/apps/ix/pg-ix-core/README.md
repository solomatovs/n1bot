# pg-ix-core: ядро графа ix

Схема, на которую опираются все остальные пакеты ix: сама схема `ix`, тип `ix.surface_e`,
словарь `ix.surface`, три таблицы графа — `ix.node`, `ix.tree`, `ix.edge` — и реестр аспектов
`ix.aspect`, `ix.surface_aspect`, через который владельцы поверхностей объявляют тексты для
индексаторов и описателя. Пакет ничего не
исполняет в рантайме: у него нет ни воркера, ни очередей, только DDL и команда наката.

```
schema/   DDL ядра: 00_schema, 10_surface, 15_aspect, 20_node, 30_tree, 40_edge
```

## Установка

Ядро ставится первым, до любого скрапера, описателя и индексатора. Команда идемпотентна:
файлы применяются по порядку имён, каждая команда отдельной транзакцией, повторный запуск
ничего не ломает.

Настройки берутся из одного файла конфига, секция `[ix.core]`. Конфиг приложения на dev-стенде лежит в `compose/apps/pg-ix-core/conf.toml`
(каталог вне git, в нём креды). Одним файлом можно запускать и несколько приложений:
каждое читает только свою секцию.

База задаётся подсекцией `[ix.core.postgres]` — профилем boba-db-postgres: host, dbname,
`auth` с методом (`password`, `certificate`, `kerberos_keytab`, `kerberos_password`),
`options` с `lock_timeout` и `statement_timeout` сессии, `pool` с размерами пула. Подсекция
`[ix.core.krb]` даёт krb5.conf и каталог кэшей билетов для kerberos-профиля. Соединения
берутся из AsyncPostgresPool, воркер async.

```
.venv/bin/boba-ix-core upgrade --config ../../compose/apps/pg-ix-core/conf.toml
```

Схема хранения задаётся полем `db_schema` секции: в sql-файлах она
стоит плейсхолдером `{schema}` (`{schema}.node`), имя берётся из конфига, а квотирует его
psycopg (`sql.Identifier`).

Роль профиля должна иметь право создавать схему и расширения (`pg_trgm`, `vector`,
`btree_gin`) в базе. Дальше каждый пакет накатывает свою схему своей же командой:

```
.venv/bin/boba-pg-meta-scraper upgrade --config ../../compose/apps/pg-meta-scraper/conf.toml
.venv/bin/boba-pg-idx-fts      upgrade --config ../../compose/apps/pg-idx-fts/conf.toml
```

Пакет без накаченного ядра отказывается работать с внятным сообщением, а не падает на
первом внешнем ключе.

## Что лежит в ядре

`ix.surface_e` — пустой enum: имена поверхностей добавляют пакеты-владельцы
(`alter type ... add value if not exists`), удалить значение postgres не позволяет.
`ix.surface` — словарь поверхностей с описанием; строки тоже добавляют владельцы.

`ix.node` — узел графа: поверхность и адрес (`address` jsonb, он же ключ). `ix.tree` —
принадлежность узла родителю, ровно одна строка на узел. `ix.edge` — прямая связь двух
узлов с весом; позиционные детали связи живут в surface-таблицах пакетов
(`ix.pg_meta_edge` у скрапера PostgreSQL).

`ix.aspect_class_e` — четыре класса аспектов, единственное, что в реестре фиксировано:
`ident` (имя, путь), `words` (слова имени), `description` (проза), `describer_input` (вход
описателя). `ix.aspect_e` — пустой enum имён аспектов, значения добавляют владельцы так же,
как в `surface_e`. `ix.aspect` — словарь: аспект, его класс, описание, владелец.
`ix.surface_aspect` — объявления: на пару «поверхность, аспект» запрос `body`, возвращающий
`node_id bigint` и `content varchar`. Потребитель подписан на классы, читает объявления
(`AspectDeclarations`) и получает один источник (`AspectSources.union`), не зная поверхностей.

Описание модели целиком, вместе с решениями и примерами, лежит в
`docs/knowledge-schema.sql`; исполняемый DDL ядра — здесь.

## Для пакетов

Пакеты ix зовут накат своей схемы тем же кодом:

```python
from boba.pg_ix_core.upgrade import SchemaUpgrade, UpgradeConfig

report = SchemaUpgrade(package_dir / "schema").run(cfg)
```

`SchemaUpgrade` подставляет схему в `{schema}` и отдаёт каждый файл серверу одной командой
psycopg, а перед накатом проверяет наличие `{schema}.node`, если пакет на ядро опирается.
Поэтому файл обязан быть самодостаточным: значения enum нельзя использовать в своей же
транзакции, и пакет кладёт `alter type ... add value` отдельным файлом, а словарь и
таблицы следующим.

После файлов накат проверяет все объявления `{schema}.surface_aspect`: каждое тело
выполняется как `select * from (<body>) s limit 0`, имена и типы колонок сверяются с
контрактом, и ошибка называет поверхность и аспект. Тело хранит схему плейсхолдером
`{schema}`, который подставит потребитель, поэтому в файле владельца он пишется удвоенным,
`{{schema}}`, по правилу `sql.SQL.format`; сами тела удобно писать dollar-quoted строками:

```sql
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('pg_meta_table', 'meta_name', $body$
    select x.node_id, x.name as content from {{schema}}.pg_meta_table x
    $body$);
```

Потребитель собирает источник при старте цикла и подставляет его в свои файлы
вместе со схемой:

```python
declarations = AspectDeclarations.of_classes(conn, cfg.db_schema, cfg.classes)
sources = AspectSources.union(declarations, cfg.db_schema)
query = SchemaName.render(text, cfg.db_schema, sources=sources)
```
