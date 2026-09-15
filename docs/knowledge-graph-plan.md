# Граф знаний над корпусами: план слоя хранения

## 1. Цель и границы

Создать поисковую систему на базе графовых связей, создать индексатор, которые способны индексировать разнородные данные: web (confluence), базы данных (PostgreSQL, ClickHouse, Oracle, MySQL).
Текущий индексатор `boba-indexing` остается как есть, не трогается. Вся разработка ведется с нуля и не затрагивает уже созданные компоненты в части изменений

## 2. Ядро (boba-graph)

Ядро системы описывает всего несколько таблиц, которые хранят связи между остальными компонентами системы в виде графа (nodes, edges)
Ядро не знает что за сущности (entities) в нем храняться, но знает их уникальный адрес и то, как эти сущности связаны.
Также в ядро входят метрики узлов графа (ranks) и общая таблица синхронизации (sync), помогающая определить актуальность данных в графе.

Ядро не знает, что такое страница, таблица или индекс. Каждая строка в системе графа ядро просто хранит и никогда не толкует смысл сохраненного.

Смысл каждой записи, хранимой в nodes разнесен по отдельным таблицам за пределы ядра системы. Это позволяет проектировать индексацию любого объекта, который может потребоваться. Структуры хранящие смысл называются - корпус.

Каждый корпус имеет свой собственный список видов данных (`kind`).
Корпус определяет этот список самостоятельно и ядро ничего не знает об этих видах.
У каждого вида данных свой
- addres
- reader
- content

Общего перечисления видов данных не существует, каждый корпус вводит собственные виды данных, иначе Confluence, Warehouse корпуса нельзя было бы развести по отдельным пакетам

Ядро задает спецификацию общения между компонентами системы (protocol class, generic, models), но не хранит их специфику. Вся специфика разведена по корпусам

Слои по коду:

- **доменное ядро** (`boba-graph`) — модели узла, ребра, сущности, адреса;
  порты хранения; конвейер. О СУБД, DDL и SQL оно не знает: хранение
  приходит реализацией портов;
- **реализация хранения** (`boba-db-pggraph`) — порты ядра на Postgres,
  sql схема хранения ядра, модели на python, классы протоколы
- **слой корпусов** — Каждый корпус размещается в пакетах, где уже реализованы классы для работы с источником. К примеру
- `boba-db-postgres` размещает корпус для хранения postgres данных
- `boba-db-clickhouse` размещает корпус хранения данных clickhouse
- `boba-transport-http` размещает корпус для хранения confluence
Новые пакеты делаем только в случае если негде разместить корпус, к примеру когда захочется сделать oracle корпус
- **инструменты для работы с графом** (`boba-tool-graph`) — поиск, обход, глобальная стадия, установка схемы;
- работают с любым корпусом через реестр и о конкретных корпсах ничего не знает

Каждый корпус будем хранить в отдельных схемах, к примеру:
- `corpus_confluence` - здесь размещаем корпусные данные по confluence
- `corpus_postgres` - здесь размещаем корпусные данные заиндексированных postgres баз
- `corpus_clickhouse` - здесь размещаем корпусные данные заиндексированных clickhouse баз
- `corpus_entity` - здесь размещаем корпус данных, которые будут называться `entity` (сущности). Это некий абстрактный набор данных, который выявляет LLM в процессе индексации. К примеру есть сущность `oil`, `soft`, `gaz` или что-то другое. Эти объекты также будут храниться в графе и будут связываться с индексируемыми объектами, что позволит усиливать/ослабевать связь между индексируемыми объектами

### 2.1 Обзор

Ядро - это система из 4-х таблиц которые описывают граф, синхронизатор и ранжировани.

Все узлы и связи ходят по суррогатному `bigint`

| таблица | что хранит
|---|---|
| `nodes` | узел: только идентичность — знает только вид и адрес объекта
| `sync` | учёт обхода: что качать, что разбирать, что удалить
| `edges` | рёбра графа. указано как направление от node до node, так и вид связи, вес, обоснование связи (простая llm text summary)
| `ranks` | метрики узла по алгоритмам типа pagerank

#### 2.2.1 Sql core

```sql
-- узел графа (node)
create table nodes (
    id            bigserial primary key,
    kind          varchar not null,   -- полный дискриминатор: confluence_page | pg_table | ch_column — перечисление корпуса, раздел 2
    address       jsonb       not null,   -- адрес по ролям, включая scheme, раздел 2.2.1; идентичность узла
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create unique index on nodes (address);   -- идентичность: одна строка на адрес
create index on nodes (kind);
create index on nodes using gin (address jsonb_path_ops);

-- ребра графа (edges)
create table edges (
    source_id     bigint      not null references nodes on delete cascade,
    target_id     bigint      not null references nodes on delete cascade,
    kind          varchar     not null,   -- вид связи из перечисления корпуса
    weight        real        not null,   -- 0..1
    evidence      jsonb       not null default '{}',   -- Evidence.dump() модели вида ребра: факты и параметры расчёта
    computed_at   timestamptz not null default now(),
    primary key (source_id, target_id, kind)          -- прямой обход: index-only по (source_id, …)
);
create index on edges (target_id, source_id, kind) include (weight);   -- обратный обход adjacency: index-only

-- глобальные метрики node'ов (rank)
create table ranks (
    node_id       bigint      not null references nodes on delete cascade,
    metric        text        not null,   -- имя метрики: pagerank | betweenness | closeness | degree_in | degree_out | community | hits_hub | hits_authority
    value         double precision not null,   -- значение; у community — номер сообщества
    computed_at   timestamptz not null,
    run_id        text        not null,   -- прогон глобальной стадии, давший значение
    primary key (node_id, metric)
);
create index on ranks (metric, value desc);

-- рабочий журнал синхронизации (sync)
create table sync (
    node_id           bigint      primary key references nodes on delete cascade,
    -- version
    hk uuid not null,
    -- хэш исходного объекта (node_id)
    -- позволяет не анализировать объект если его хеш не менялся с предыдущего раза
    cs uuid not null,
    -- способы обработки, которыми был разобран объект
    -- один и тот же элемент может быть разобран несколькими методами:
    -- картинка может быть разобрана по смыслу содержания, по написанному на ней тексту, по размеру или еще какими-то параметрами
    applied_methods   text[]      not null default '{}',
    -- hash настроек, с помощью которых был разобран node.
    -- к примеру embedding модели имеют разную размерность и при смене модели
    -- нужно проводить реиндексацию. Соответственно меняется штамп
    pipeline_stamp    text        not null default '',
    -- crawl_scope и last_seen_run — нужны для обнаружения удалений
    -- к примеру confluence страница исчезла, или была удалена таблица с колонками, как понять что произошло удаление?
    crawl_scope       text        not null default '',
    -- прогон, в списке которого узел был в последний раз
    last_seen_run     text        not null default '',
    last_seen_at      timestamptz,
    last_indexed_at   timestamptz,
    -- почему узел не индексируется правилами; пусто — индексируется
    skip_reason       text        not null default ''
);
create index on sync (crawl_scope, last_seen_run);

create extension if not exists pg_trgm;

create extension if not exists unaccent;

create extension if not exists vector;

create function immutable_unaccent(text) returns text language sql immutable parallel safe strict as $ $
select
    public.unaccent('public.unaccent', $1) $$;

create table pg_table_list (
    id                  bigserial primary key,
    node_id             bigint not null references nodes on delete cascade,
    database_name       varchar(64) not null,
    schema_name         varchar(64) not null,
    table_name          varchar(64) not null,
    tablespace_name     varchar(64) null,
    owner               varchar(64) not null,
    comment             varchar null,
    c__name             varchar(500) not null generated always as (
        database_name || '.' || schema_name || '.' || table_name
    ) stored,
    c__full_tsv         tsvector not null generated always as (
        setweight(to_tsvector('russian', table_name), 'A')
        || setweight(to_tsvector('russian', schema_name || ' ' || database_name), 'B')
        || setweight(to_tsvector('russian', owner || ' ' || coalesce(tablespace_name, '')), 'C')
        || setweight(to_tsvector('russian', immutable_unaccent(coalesce(comment, ''))), 'D')
    ) stored,
    unique (node_id, database_name, schema_name, table_name)
);


-- точное совпадение без учёта регистра.
-- where lower(c__full) = lower('Order')
-- Даёт мгновенный ответ на "есть ли объект с именем Orders или orders или ORDERS или orDers"
create index pg_table_list__table_name__btree       on pg_table_list using btree (lower(table_name));
create index pg_table_list__schema_name__btree      on pg_table_list using btree (lower(schema_name));
create index pg_table_list__database_name__btree    on pg_table_list using btree (lower(database_name));
create index pg_table_list__tablespace_name__btree  on pg_table_list using btree (lower(tablespace_name));

-- подстрока, опечатки, ранжирование (KNN search, trigram similarity): ilike '%abc%', %, <->
-- опечатка в имени, ближайшие выдаются первыми
-- select   id, c__name
-- from     pg_table_list
-- where    c__name % 'ordrs'
-- order by c__name <-> 'ordrs'
-- limit    20;
create index pg_table_list__c__name__gist on pg_table_list using gist (c__name gist_trgm_ops);

-- полнотекстовый индекс по имени: c__full_tsv @ @ to_tsquery('english', 'customer & orders')
-- слова из имени и комментария, имя весит больше
-- select id, c__name, ts_rank(c__full_tsv, q) as rank
-- from
--     pg_table_list,
--     to_tsquery('russian', 'customer & orders') q
-- where c__full_tsv @ @ q
-- order by rank desc
-- limit 20;
create index pg_table_list__c__full__gin on pg_table_list using gin (c__full_tsv);

-- отдельная таблица для векторного поиска по имени и комментарию (e5 embedding, 1024 размерность)
create table pg_table_list__vector_e5_1024 (
    id              bigint primary key references pg_table_list on delete cascade,
    c__full_emb     vector(1024) null,
    comment_emb     vector(1024) null
);

create index on pg_table_list__vector_e5_1024 using hnsw (c__full_emb vector_cosine_ops);

create index on pg_table_list__vector_e5_1024 using hnsw (comment_emb vector_cosine_ops);

-- хранит информацию о том, что было проиндексировано в источнике и что нужно проиндексировать в источнике
create table pg_index_runner (
    id                  bigint not null primary key,
    -- версия в источнике, которая была найдена в прошлый прогон
    -- берется из источника и сохраняется в таблице, чтобы при следующем прогоне можно было определить,
    -- что объект в источнике изменился и начать скачивание и анализ исходника
    s__source_version   varchar null,
    -- хэш сумма по node объекта в источнике
    -- по ней определяем изменился ли источник объекта и нужно ли его индексировать
    s__source_checksum  varchar not null,
    -- хэш сумма по индексатору объекта в источнике
    -- например хэш сумма по параметрами embeding модели, которые использовались для индексирования объекта в источнике
    s__indexer_checksum varchar not null,
    -- время обновления scope
    s__upd_ts           timestamptz not null,
);
```

#### 2.2.2 Python core

```python
# boba-graph
class Node(BaseModel):
    """Узел графа: абстрактный объект в системе связей (документа, чанк, картинка, часть картинки, foreain_key, название таблицы, да все что угодно)
    
    Все это добро храниться в виде:
    - id: equence номер объекта в этой таблице
    - kind: вид объекта в этой таблице (pg_table, ch_column, confluence_page, page_section, foreign_key, ...)
    - address: json поле определяющее уникальный адрес, по которому можно добраться до объекта (самая креативная часть системы)

    Ничего лишнего здесь не храниться, только адрес и вид
    а где храниться контент, на который указывает node?
    В отдельных contant tables, которые описывают все атрибуты привязанные к этому адресу
    Таким образом для разных индексируемых объектов, создается своя собственная
    система хранения с атрибутами, контентом, индексами и прочим
    А связь между node (core слой) и content (слой хранения) идет через node_id
    """

    id: int
    kind: str
    address: Mapping[str, str | int]

class Edge(BaseModel):
    """Связь между двумя узлами, прочитанная из графа: вид, вес, обоснование.

    Выходит наружу через kb_related и kb_node, в глобальную стадию — через
    export(); обоснование — jsonb как есть, разбирает его тот, кто знает вид.
    """

    source_id: int
    target_id: int
    kind: str
    weight: float
    evidence: Mapping[str, object]

class Address(BaseModel, ABC):
    """Адрес node'ы — определяет уникальный адрес элемента в системе
    По адресу индексатор определяет, был ли ранее заиндексирован объект
    По адресу происходит обращение к найденным элементам

    Адрес харинться в виде json поля для удобства манипутяции внутри sql запросов
    По задумке любой адрес можно преобразовать в URL вид и обратно в json
    Все конвертеры адресов будут храниться в content части, отдельно от core слоя приложения
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: str

    def parts(self) -> Mapping[str, str | int]:
        return self.model_dump(by_alias=True)

    @abstractmethod
    def render(self) -> str: ...

# обоснование ребра - почему вообще считается, что эта связь существует?
class Evidence(BaseModel):
    """Обоснование ребра: факты, по которым связь посчитана, и параметры, при которых она построена.

    Нужно, чтобы связи можно было проверять, а не только верить весу:
    человек и модель видят, почему узлы связаны («see FLIP-458», косинус
    0.87), kb_graph_check находит рёбра, построенные при других порогах, а
    стадия edges пересчитывает только их. У каждого вида ребра своя модель
    (3.5, 4.6, 5.4); ядро кладёт dump() в edges.evidence, не разбирая.
    """

    def dump(self) -> Mapping[str, object]:
        return self.model_dump()

# базовые классы для хранения векторов
class Vector(BaseModel):
    """Числовое представление текста или картинки, по которому сравнивают смысл, а не слова.

    Считается моделью эмбеддинга, и сравнимы только векторы одной модели,
    поэтому имя модели идёт вместе с числами. Подклассы — форма чисел:
    плотный и разреженный.
    """

    model: str                              # имя модели из [encoders.models]; ставит энкодер

class DenseVector(Vector):
    """Плотный вектор: число на каждую координату пространства модели.

    Столько координат, сколько у модели размерность (dim в её конфиге):
    у e5-large — 1024 числа, у text-embedding-3-large — 3072. Сравнивается
    косинусом или скалярным произведением; в базе — pgvector vector(dim).
    """

    values: Sequence[float]                 # координаты по порядку, len(values) == dim модели

class SparseVector(Vector):
    """Разреженный вектор: почти все координаты — нули, хранятся только ненулевые.

    Координата — слово (токен) словаря модели, значение — его вес в тексте;
    словарь SPLADE — десятки тысяч токенов, в тексте ненулевых — десятки.
    Поэтому хранятся пары «номер координаты — вес», а не весь ряд нулей;
    в базе — pgvector sparsevec, текстовая форма {i1:v1,i2:v2,…}/dim.
    """

    indices: Sequence[int]                  # номера ненулевых координат по возрастанию: id токенов словаря модели
    values: Sequence[float]                 # веса тех же координат, len(values) == len(indices)
    dim: int                                # полная размерность пространства — размер словаря модели; нужна sparsevec

# generic'и для Vector
V = TypeVar("V", bound=Vector)
V_co = TypeVar("V_co", bound=Vector, covariant=True)

class VectorEncoder(Protocol[V_co]):
    """Порт к модели эмбеддинга: текст в вектор.
    """
    async def encode(self, text: str) -> V_co: ...

class VectorEncoderRegistry(Protocol):
    """Реестр энкодеров: VectorEncoder по имени модели из [encoders.models].
    """

    def dense(self, model: str) -> VectorEncoder[DenseVector]: ...
    def sparse(self, model: str) -> VectorEncoder[SparseVector]: ...
```

#### 2.2.3 Примеры адресации разных источников

| kind | `address` (jsonb) | строка |
|---|---|---|
| `confluence_space` | `{"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/rest/api/space/FLINK"}` | `https://cwiki.apache.org/confluence/rest/api/space/FLINK` |
| `confluence_page` | `{"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/rest/api/content/307136992"}` | `https://cwiki.apache.org/confluence/rest/api/content/307136992` |
| `confluence_attachment` | `{"scheme": "https", "host": "cwiki.apache.org", "port": 443, "path": "/confluence/download/attachments/307136992/design.pdf"}` | `https://cwiki.apache.org/confluence/download/attachments/307136992/design.pdf` |
| `pg_database` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh"}` | `postgresql://dwh.local:5432/dwh` |
| `pg_schema` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm"}` | `postgresql://dwh.local:5432/dwh?schema=dm` |
| `pg_table` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "table": "fact_orders"}` | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders` |
| `pg_column` таблицы | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "table": "fact_orders", "column": "amount"}` | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&column=amount` |
| `pg_view` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "view": "v_orders_daily"}` | `postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily` |
| `pg_column` представления | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "view": "v_orders_daily", "column": "day"}` | `postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily&column=day` |
| `pg_matview` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "matview": "mv_orders_month"}` | `postgresql://dwh.local:5432/dwh?schema=dm&matview=mv_orders_month` |
| `pg_index` — уникален в схеме, без `table` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "index": "fact_orders_customer_idx"}` | `postgresql://dwh.local:5432/dwh?schema=dm&index=fact_orders_customer_idx` |
| `pg_sequence` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "sequence": "fact_orders_order_id_seq"}` | `postgresql://dwh.local:5432/dwh?schema=dm&sequence=fact_orders_order_id_seq` |
| `pg_function` — сигнатура в `args` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "function": "calc_total", "args": "bigint,numeric"}` | `postgresql://dwh.local:5432/dwh?schema=dm&function=calc_total&args=bigint%2Cnumeric` |
| `pg_function` — перегрузка, другой узел | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "function": "calc_total", "args": "bigint"}` | `postgresql://dwh.local:5432/dwh?schema=dm&function=calc_total&args=bigint` |
| `pg_function` — без аргументов, `args` пуст | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "function": "now_utc", "args": ""}` | `postgresql://dwh.local:5432/dwh?schema=dm&function=now_utc&args=` |
| `pg_procedure` | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "procedure": "close_orders", "args": "date,text"}` | `postgresql://dwh.local:5432/dwh?schema=dm&procedure=close_orders&args=date%2Ctext` |
| `pg_constraint` — внутри таблицы | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "table": "fact_orders", "constraint": "fact_orders_customer_fkey"}` | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&constraint=fact_orders_customer_fkey` |
| `pg_trigger` — внутри таблицы | `{"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh", "schema": "dm", "table": "fact_orders", "trigger": "trg_orders_audit"}` | `postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&trigger=trg_orders_audit` |
| `ch_database` — схем нет | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs"}` | `clickhouse://ch1:9000/logs` |
| `ch_table` | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events"}` | `clickhouse://ch1:9000/logs?table=events` |
| `ch_column` | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "column": "user_id"}` | `clickhouse://ch1:9000/logs?table=events&column=user_id` |
| `ch_view` | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "view": "v_events_hourly"}` | `clickhouse://ch1:9000/logs?view=v_events_hourly` |
| `ch_matview` | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "matview": "mv_events_daily"}` | `clickhouse://ch1:9000/logs?matview=mv_events_daily` |
| `ch_index` — skip-индекс, внутри таблицы | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "index": "events_ts_minmax"}` | `clickhouse://ch1:9000/logs?table=events&index=events_ts_minmax` |
| `ch_projection` — внутри таблицы | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "table": "events", "projection": "events_by_user"}` | `clickhouse://ch1:9000/logs?table=events&projection=events_by_user` |
| `ch_dictionary` | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "dictionary": "dict_users"}` | `clickhouse://ch1:9000/logs?dictionary=dict_users` |
| `ch_function` — перегрузок нет, без `args` | `{"scheme": "clickhouse", "host": "ch1", "port": 9000, "database": "logs", "function": "to_rub"}` | `clickhouse://ch1:9000/logs?function=to_rub` |
| `mssql_table` — именованный инстанс ролью | `{"scheme": "mssql", "host": "sql01.corp", "port": 1433, "database": "erp", "instance": "ERP", "schema": "dbo", "table": "Orders"}` | `mssql://sql01.corp:1433/erp?instance=ERP&schema=dbo&table=Orders` |
| `mssql_procedure` | `{"scheme": "mssql", "host": "sql01.corp", "port": 1433, "database": "erp", "schema": "dbo", "procedure": "usp_CloseOrder"}` | `mssql://sql01.corp:1433/erp?schema=dbo&procedure=usp_CloseOrder` |
| `oracle_table` — сервис вместо базы | `{"scheme": "oracle", "host": "ora1", "port": 1521, "database": "ORCL", "schema": "SALES", "table": "ORDERS"}` | `oracle://ora1:1521/ORCL?schema=SALES&table=ORDERS` |
| `mysql_table` — схем нет | `{"scheme": "mysql", "host": "db1", "port": 3306, "database": "shop", "table": "orders"}` | `mysql://db1:3306/shop?table=orders` |
| `entity` | `{"scheme": "entity", "name": "kraft"}` | `entity://kraft` |
| `entity` — имя с пробелом, reg-name | `{"scheme": "entity", "name": "arenadata quickmarts"}` | `entity://arenadata%20quickmarts` |

**Адрес** — это идентификатор node'ы: то, по чему один и тот же объект источника узнаётся при каждой индексации, находится в графе и называется наружу.
Ядро о составе адреса не знает ничего: роли объявляет модель адреса
корпуса (`PgAddress`, `ChAddress`, `ConfluenceAddress`, `EntityAddress`),
ядро только хранит части, сравнивает их и ищет по ним.

**Хранение** Адрес лежит в `nodes.address` как jsonb-объект: `{ "роль": значение }`.
Схема адреса (pg_table, pg_column, ch_database, confluence_page и прочие) лежит внутри jsonb-объекта. Через схему строиться целевой плоский адрес в URL виде (к примеру `clickhouse://ch1:9000/logs?function=to_rub`)

**Идентичность** Уникальный индекс стоит на `address` целиком. 
Ключи внутри jsob поля имеют типы, которые задаются моделями в корпусе, а не в core слое. Каждая модель в корпусе должно типизировать свои значения и проверять эти значения в процессе сериализации/десериализации, никаких "общих" проверок со стороны core не должно быть. core не знает о том, как записываются адреса, он лишь умеет воспользоваться реестром типов для сериализации/десериализации.

**Поиск по частям** По `address` стоит GIN-индекс с `jsonb_path_ops`. Поэтому поиск по параметрам внутри будет выполняться через оператор вхождения: `@>`, например:
- дай все адреса PostgreSQL: `address @> '{"scheme": "postgresql"}'`
- дай все адреса на хосте: `address @> '{"host": "dwh.local"}'`
- дай все адреса схемы:
`address @> '{"schema": "dm"}'`.
Так можно проверить наличие параметрам: `?`:
Так можно проверить все адреса, где есть table: `address ? 'table'`.

**Строка адреса.** Нужна человеку, модели и параметрам инструментов для простой адресации к объекту, они будут передавать node строкой. Часть address строится по стандарту:
- libpq URI для PostgreSQL
- JDBC для MSSQL, Oracle и MySQL
- clickhouse-connect для ClickHouse
- RFC 3986 для web.
Стандарты заканчиваются на базе данных — адреса таблицы. А вот что не описывают стандарты дак это колонки, индексы, sequence'ы и прочие объекты внутри базы данных, поэтому такие объект внутри
базы задаётся query-параметрами, в порядке объявления параметров в модели: `…/dwh?schema=dm&table=fact_orders&column=amount`. Роль
в имени снимает позиционную неоднозначность (`table=daily` и `view=daily`), а строка остаётся валидным URL, который разбирает
`urllib.parse` из stdlib.
Примеры всех вариантов — в таблице выше.

**Канон строки** — правила, которым подчиняются `render()` и `parse()`
каждой модели адреса (3.2, 4.2, 5.2):

- учётных данных в строке нет никогда;
- параметры подключения (`sslmode`, `application_name`) — не часть
  идентичности и в адрес не попадают;
- порт в частях обязателен; в строке web-адреса порт по умолчанию схемы
  опускается, как делает `httpx.URL`;
- порядок query-параметров — порядок объявления ролей в модели, значения
  кодируются по RFC 3986;
- обе стороны одной грамматики — один класс в пакете источника; других
  мест сборки и разбора нет, ядро строки не знает. Строку, о которой
  неизвестно, какой объект она называет, разбирают `PgAddresses.parse` и
  `ChAddresses.parse` по составу ролей в query.

### 2.3 Учёт обхода: таблица `sync`

Индексация — не разовая загрузка, а повторяющийся обход источника. по сути, это периодически запускаемый процесс, каждый прогон которого должен разбирать только то, что изменилось, и убирать из индекса то, что в источнике исчезло.

Запуск процесса будет происходить из разных инициаторов, с разным набором фильтраций, но это не меняет общий корень программы. Нам даже не особо важно кто стартанул программу на индексацию. Это может быть:
- запуск через специальный tools. Я не уверен здесь, но вероятно для разных корпусов, будут разные tools на запуск (сейчас требуется именно этот вариант)
- запуск через scheduler внешней системой. Может быть даже rest api на запуск сделать (сейчас не требуется)
- запуск через cli (сейчас не требуется)

Строка `sync` есть у каждой node'ы, а так как node'а это любой объект: страница, параграф, чанк текста, вложение, имя таблицы, ddl , колонки, sequence, view, ... 
Соответственно sync описывает некую метаинформацию о синхронизации любого элемента, который используется в графе

**Аспекты.** Node'ы указывают на 
У одной и той-же node'ы может быть несколько независимых сторон изменения. К примеру таблицы, там две стороны изменения:
- структура DDL, колонки, ограничения, индексы, комментарии
- содержимое таблиц (пример строк), число строк.
Если изменилась структура то надо перечитать метаданные, пересобрать тексты `ddl` и `columns`
Если изменились данные то необходимо перепрофилировать, обновить пример.

Аспекты изменения node ядро не знает, их знает только корпус, именно в нем заложена логика обновления.
Прогон сравнивает версии по каждому аспекту отдельно и запускает обновление только по изменившимся аспектам. К пирмеру ddl изменился, значит нужно обновить метаданные всей таблицы

**`source_versions` и `body_hashes` — два разных вопроса.** Первый: «надо
ли качать?» — отвечается по списку или каталогу, до скачивания. Второй:
«надо ли разбирать?» — отвечается по скачанному, после. Они не выводятся
друг из друга.

| корпус, узел, аспект           | что берётся                                                                 | пример |
|--------------------------------|-----------------------------------------------------------------------------|--------|
| страница Confluence, content   | номер версии                                                                | `v10` |
| вложение Confluence, content   | версия, дата, размер, тип                                                   | `v3:2026-05-01T09:12:00Z:184320:application/pdf` |
| таблица PostgreSQL, structure  | `xmin` строк каталога: `pg_class`, `pg_attribute`, `pg_description`, `pg_constraint`, `pg_index` — сводятся в хэш | `3f9a1c…` |
| таблица PostgreSQL, data       | `pg_stat_user_tables`: `n_tup_ins`, `n_tup_upd`, `n_tup_del`, `last_autoanalyze`; сброс статистики меняет счётчики — лишняя перепрофилировка, не пропуск | `ins=12401233;upd=88102;del=0;analyzed=2026-09-11T03:00:00` |
| таблица ClickHouse, structure  | `system.tables.metadata_modification_time`                                  | `2026-09-11T22:40:03` |
| таблица ClickHouse, data       | `system.parts` по активным партам: `max(modification_time)`, `sum(rows)`    | `2026-09-12T01:10:44;rows=9812004411` |
| таблица MSSQL, structure       | `sys.objects.modify_date`                                                   | `2026-09-10T14:02:11` |
| таблица MSSQL, data            | `sys.dm_db_index_usage_stats.last_user_update`, `sys.partitions.rows`       | `2026-09-12T00:05:19;rows=4410233` |
| таблица Oracle, structure      | `ALL_OBJECTS.LAST_DDL_TIME`                                                 | `2026-09-09T08:15:00` |
| таблица Oracle, data           | `DBA_TAB_MODIFICATIONS` (`inserts`, `updates`, `deletes`), `LAST_ANALYZED`  | `ins=1200;upd=0;del=0;analyzed=2026-09-08` |
| таблица MySQL, structure       | `CREATE_TIME` при перестройке; иначе версии нет — решает хэш DDL            | `2026-08-30T12:00:00` |
| таблица MySQL, data            | `information_schema.TABLES.UPDATE_TIME` — у InnoDB ненадёжно; иначе `count(*)` выборкой | `2026-09-12T02:00:00;rows=88012` |

`body_hashes` — хэш скачанного по аспекту: `content` — HTML страницы
вместе с заголовком или байты вложения; `structure` — нормализованный
DDL с комментариями; `data` — профиль колонок и пример строк.

Страница, аспект `content`:

| прогон | `source_versions` | `body_hashes` | что случилось в источнике | что делает прогон |
|---|---|---|---|---|
| 1 | `{content: v10}` | `{content: cc4f…}` | первая индексация | качать, разбирать, писать |
| 2 | `{content: v10}` | `{content: cc4f…}` | ничего | список совпал — не качать |
| 3 | `{content: v11}` | `{content: cc4f…}` | пересохранили без правок | версия новая — качать; хэш совпал — не разбирать, обновить версию |
| 4 | `{content: v12}` | `{content: 9b1e…}` | текст изменили | качать, разбирать, писать |
| 5 | `{content: v13}` | `{content: 7a20…}` | переименовали, текст прежний | хэш считается с заголовком — разбирать: заголовок в content tables и в связях |

Таблица, аспекты `structure` и `data`:

| прогон | `source_versions` | что случилось | что делает прогон |
|---|---|---|---|
| 1 | `{structure: 3f9a…, data: ins=12.40M…}` | первая индексация | обе группы стадий |
| 2 | `{structure: 3f9a…, data: ins=12.41M…}` | ночная загрузка, структура прежняя | только `data`: профили, `sample`, `inferred_key` |
| 3 | `{structure: 7b21…, data: ins=12.41M…}` | добавили колонку | только `structure`: DDL, `columns`, явные рёбра |
| 4 | `{structure: 7b21…, data: ins=12.41M…}` | ничего | ничего |

Случай 3 у страницы — самый частый в Confluence (сохранение без
изменений, правка метки, перестановка в дереве) и самый дорогой без
хэшей: без `body_hashes` каждая такая версия шла бы в OCR вложений и в
модель саммари заново. Случай 2 у таблицы — самый частый в Warehouse:
ежедневная загрузка меняет данные, но не структуру, и перечитывать
каталог с пересборкой рёбер незачем.

У PostgreSQL времени изменения объекта в каталоге нет, но есть системная
колонка `xmin` — номер транзакции, последней изменившей строку каталога.
`ALTER TABLE`, переименование, смена владельца обновляют строку
`pg_class`; добавление, удаление и смена типа колонки — `pg_attribute`;
комментарии — `pg_description`; ключи и индексы — `pg_constraint` и
`pg_index`. Хэш от их `xmin` и есть версия структуры:

```sql
select
    md5(concat_ws(':',
        c.xmin::text,
        (select max(a.xmin::text::bigint) from pg_attribute a where a.attrelid = c.oid),
        (select max(d.xmin::text::bigint) from pg_description d where d.objoid = c.oid),
        (select max(k.xmin::text::bigint) from pg_constraint k where k.conrelid = c.oid),
        (select max(i.xmin::text::bigint) from pg_index i where i.indrelid = c.oid)
    )) as structure_version
from
    pg_class c
where
    c.oid = 'dm.fact_orders'::regclass
```

`xmin` 32-битный и после ~4 млрд транзакций идёт по кругу; здесь он
сравнивается только на равенство, и совпадение старого значения после
оборота при неизменном хэше структуры безвредно — DDL сверяется всё
равно. OID для этого не годится: он стабилен на всё время жизни объекта и
при `ALTER` не меняется. Изменение данных `xmin` не ловит — это аспект
`data` со своим сигналом из таблицы выше.

**`applied_methods`** — какими способами узел уже обработан. Способы — не
уровни: OCR, распознавание смысла картинок, саммари, извлечение сущностей
независимы и включаются в любом сочетании; перечисление объявляет корпус:

```python
class ConfluenceParseMethod(StrEnum):
    TEXT = "text"          # текст страницы и текстовый слой вложений — всегда
    OCR = "ocr"            # текст с картинок и сканов
    CAPTION = "caption"    # описание содержимого картинок моделью зрения
    SUMMARY = "summary"    # саммари узла языковой моделью
    ENTITIES = "entities"  # сущности из текста
```

Прогон запрашивает набор способов флагами вызова; узел переразбирается,
если запрошенное множество не входит в применённое — даже при совпавших
версиях и хэшах. Применённое множество не сокращается: прогон без `ocr`
не стирает распознанное. Способ дописывается после успеха стадии; сбой
стадии его не дописывает, и следующий прогон повторит её.

| узел | `applied_methods` | запрошено | что делает прогон |
|---|---|---|---|
| 17 | `{text}` | `{text, summary}` | саммари не применялось — считать, дописать `summary` |
| 17 | `{text, summary}` | `{text}` | всё запрошенное есть — ничего |
| 18 | `{text, ocr}` | `{text, caption}` | нет `caption` — описать картинку моделью зрения; `ocr` остаётся |

**`pipeline_stamp`** — отпечаток настроек, которыми узел разобран. Конвейер
— цепочка стадий индексации (раздел 7); у каждой стадии есть параметры, от
которых зависит результат, и штамп — их сводка одной строкой:

```
reader=confluence:3;chunk=4000/0;embed=multilingual-e5-large;ner=gliner_multi-v2.1
```

Штамп в конфиге не совпал со штампом узла — узел переиндексируется
целиком, что бы ни говорили версии и хэши. Без штампа после смены модели
эмбеддинга в индексе лежали бы векторы двух несовместимых моделей.

**`crawl_scope` и `last_seen_run` — удаление исчезнувшего.** Confluence
не сообщает об удалении страницы: REST отдаёт список того, что есть.
Единственный способ узнать об удалении — увидеть, что в списке страницы
больше нет:

- каждый прогон получает идентификатор, например `run-2026-09-12-0912a`;
- прогон обходит область — `crawl_scope`: пространство `space:FLINK`,
  схему `schema:dm` — и каждому узлу из списка пишет `last_seen_run`;
- когда список области прочитан до конца без ошибок, узлы этой области с
  другим `last_seen_run` — которых в списке не было — удаляются вместе со
  всем, что на них ссылается (внешние ключи с каскадом);
- удалённая таблица уносит колонки не через себя: колонок тоже нет в
  списке, и они удаляются тем же правилом;
- если список оборвался (сеть, лимиты), очистка не запускается: неполный
  список не доказывает, что чего-то нет.

Область нужна, чтобы прогон по одному пространству не удалил страницы
других; идентификатор прогона, а не время, — чтобы очистка опиралась на
факт «этот обход завершился», а не на давность.

Прогон `run-0912a` по `space:FLINK`: в списке страницы 17, 21 и вложение 18;
страница 19 и её вложение 20 удалены неделю назад.

| node_id | kind | `crawl_scope` | `last_seen_run` | после списка | после очистки |
|---|---|---|---|---|---|
| 17 | `confluence_page` | `space:FLINK` | `run-0912a` | в списке | остаётся |
| 18 | `confluence_attachment` | `space:FLINK` | `run-0912a` | в списке | остаётся |
| 21 | `confluence_page` | `space:FLINK` | `run-0912a` | в списке | остаётся |
| 19 | `confluence_page` | `space:FLINK` | `run-0905c` | не в списке | удаляется |
| 20 | `confluence_attachment` | `space:FLINK` | `run-0905c` | не в списке | удаляется сама, не «вслед за 19» |
| 33 | `confluence_page` | `space:KAFKA` | `run-0905c` | область не листалась | остаётся |

**`skip_reason`** — узел есть в источнике, но правила его не индексируют:
вложение вне allowlist, страница-черновик. Он отмечается увиденным, иначе
очистка приняла бы его за исчезнувший.

### 2.4 Рёбра: таблица `edges`

```sql
create table edges (
    source_id     bigint      not null references nodes on delete cascade,
    target_id     bigint      not null references nodes on delete cascade,
    kind          text        not null,   -- вид связи из перечисления корпуса
    weight        real        not null,   -- 0..1
    evidence      jsonb       not null default '{}',   -- Evidence.dump() модели вида ребра: факты и параметры расчёта
    computed_at   timestamptz not null default now(),
    primary key (source_id, target_id, kind)          -- прямой обход: index-only по (source_id, …)
);
create index on edges (target_id, source_id, kind) include (weight);   -- обратный обход adjacency: index-only
```

Один вид — одно ребро. Симметричные виды (`similar`, `same_column`,
`co_queried`) хранятся один раз, `source_id < target_id`.
Обход идёт по представлению `adjacency`, где каждое ребро развёрнуто в обе
стороны: расширение по графу (раздел 8) направления не различает —
страница, на которую ссылаются найденные, не менее важна, чем та, на
которую ссылаются они.

```sql
create view adjacency as
    select source_id, target_id, kind, weight from edges
    union all
    select target_id, source_id, kind, weight from edges;
```

Ядро само считает два вида для любого корпуса — от узла к сущностям,
которые упоминает его текст (раздел 5), и по близости векторов тем
способом, который корпус назначил роли `similarity` (2.8); имена этим
рёбрам корпус даёт таблицей `computed_edge_kinds()`. Всё остальное, включая
вложенность, объявляет и считает корпус. Ни один список ниже не закрыт:
новый признак связи — новый член перечисления корпуса и его вычислитель,
ядро не меняется. Множители веса при обходе — по `kind` в конфиге
корпуса (раздел 8).

Виды рёбер объявляет слой: перечисления `ConfluenceEdgeKind` и
`WarehouseEdgeKind` — в разделах 3.5 и 4.6, ребро `entity` — в 5.4. Ядро
знает только строку `kind`.

**Обоснование ребра.** `weight` — число для обхода, `evidence` — почему
оно такое: факты, по которым ребро посчитано, и параметры расчёта, при
которых оно построено. Пишется вместе с ребром стадией `edges`; при
переиндексации узла его рёбра удаляются в обе стороны и строятся заново
вместе с обоснованием. У каждого вида ребра своя модель — наследник
`Evidence` ядра; словаря общего вида нет.

Обоснования явных рёбер слоёв — рядом с их перечислениями (3.5, 4.6, 5.4).
Как считаются рёбра ядра:

- `similar` — ядро: способом роли `similarity` ищет похожие на текст
  самого узла (`Corpus.similar_text`, 2.8), верх `similar_top_k`, косинус не
  ниже `similar_min_cos`; сам узел из выдачи отбрасывается.

Обход (раздел 8) берёт из ребра только `weight` и множитель по `kind`;
три потребителя обоснования названы в docstring `Evidence`.

Пример рёбер разных слоёв в одной таблице `edges`:

| source_id | target_id | kind | weight | evidence |
|---|---|---|---|---|
| 17 | 18 | `has_attachment` | 1.00 | `{}` |
| 41 | 42 | `contains` | 1.00 | `{}` |
| 17 | 21 | `link` | 1.00 | `{"anchor": "FLIP-458", "phrase": "see FLIP-458 for the API", "section_id": 905}` |
| 17 | 21 | `series` | 0.50 | `{"prefix": "FLIP", "numbers": [457, 458]}` |
| 17 | 61 | `entity` | 0.60 | `{"count": 3, "type": "product"}` |
| 17 | 33 | `similar` | 0.87 | `{"cosine": 0.87, "lookup": "summary/vector", "model": "multilingual-e5-large", "min_cos": 0.80}` |
| 41 | 44 | `inferred_key` | 0.99 | `{"column": "customer_id", "target_column": "dim_customer.customer_id", "coverage": 0.998, "sample": 100000}` |
| 41 | 44 | `same_column` | 0.70 | `{"column": "customer_id", "type": "bigint"}` |
| 41 | 52 | `view_source` | 1.00 | `{"view": "dm.v_orders_daily"}` |
| 41 | 45 | `co_queried` | 0.63 | `{"queries": 118, "window": "30d"}` |

Ребро от корпуса приходит черновиком с адресом цели, из графа читается
моделью `Edge`; виды, которые ядро строит само, — `ComputedEdge`, имена
им даёт корпус (2.8). Порт — `GraphStore`, реализации — 2.9.3.

### 2.5 Метрики: таблица `ranks`

Метрика — число про один `Node`, которое нельзя узнать, глядя на него
одного: оно зависит от всего графа. PageRank говорит, насколько на узел
ссылаются те, на кого ссылаются сами; `betweenness` — как часто узел
лежит на кратчайших путях между другими, то есть связывает ли он разные
части корпуса; `degree_in` — сколько связей в него входит; `community` —
номер плотной группы, в которую он попал. Это не связи и не содержимое,
а третья вещь: производное свойство узла, которое считает глобальная
стадия по всему графу сразу (NetworkX) и которое поиск добавляет к счёту
(раздел 8), чтобы среди равных по тексту поднять центральный.

Таблица в длинном формате — одна строка на пару «узел, метрика», — потому
что метрик много, набор открыт, и колонка в `nodes` на каждый алгоритм
меняла бы схему при каждом новом.

```sql
create table ranks (
    node_id       bigint      not null references nodes on delete cascade,
    metric        text        not null,   -- имя метрики: pagerank | betweenness | closeness | degree_in | degree_out | community | hits_hub | hits_authority
    value         double precision not null,   -- значение; у community — номер сообщества
    computed_at   timestamptz not null,
    run_id        text        not null,   -- прогон глобальной стадии, давший значение
    primary key (node_id, metric)
);
create index on ranks (metric, value desc);
```

Глобальная стадия считает набор метрик из конфига; добавление алгоритма —
новая функция NetworkX и новое имя метрики, схема не меняется. Ранжирование
использует те метрики, что названы в его конфиге (раздел 8).

### 2.6 Способы поиска

Способ поиска — и объявление, и исполнитель: он знает свои таблицу и
колонки и сам собирает по ним запрос. У способа две координаты, и они
независимы: **что** ищем — вид содержимого узла (заголовок, раздел,
саммари, DDL, картинка), это перечисление корпуса; **чем** ищем —
полнотекст, BM25, триграммы, точное совпадение, плотный или разреженный
вектор, это перечисление ядра, общее для всех корпусов. Пара даёт подпись
способа (`title/exact`, `section/vector`), она же ключ веса в конфиге и
она же в выдаче отвечает, чем узел найден.

Ищут всегда текстом: его печатает пользователь, а служебные поиски ядра
берут текст узла — заголовок для ребра упоминания, саммари для ребра
похожести. Ядро объявляет протокол без привязки к драйверу: готовый
запрос — параметр типа, ничего постгресового в нём нет; реализации для
Postgres живут в `boba-db-pggraph` (2.9.1), корпус создаёт их при старте из
своего конфига (`[storage] pg_schema`) и отдаёт одним списком (2.8).

### 2.7 Энкодеры

Энкодеры живут в `boba-llm` и одинаково служат индексации (вектор
документа) и поиску (вектор запроса). Модель эмбеддинга целиком описана
конфигом корпуса, секцией `[encoders.models."<имя>"]`: что она такое и где
её веса или endpoint. Таблицы для этого нет: моделей единицы, все их
настройки и так в конфиге, а ни одна таблица не ссылается на модель иначе
как по имени. Что при этом держит базу и конфиг согласованными: таблицы
векторов создаются установкой по этим секциям, по таблице на модель, и
размерность фиксирована typmod `vector(dim)`; `pipeline_stamp` узла
хранит имя и ревизию модели, поэтому смена модели в конфиге ведёт к
переиндексации, а не к смешению векторов; `kb_graph_check` сверяет typmod
таблиц с `dim` в конфиге.

```python
class EmbeddingModel(BaseModel):
    """Секция [encoders.models."<имя>"]: что за модель и где она — провайдер, форма вектора, размерность, префиксы, веса или endpoint.

    Из неё реестр строит энкодер, установка — таблицы векторов, конвейер —
    штамп; имя секции — имя модели, которым на неё ссылаются способы
    поиска, обоснования и штампы.
    """

    slug: str                               # суффикс имён таблиц векторов: e5
    provider: str                           # local | openai
    modality: str                           # text | image | sparse — какой VectorEncoder строит реестр
    revision: str                           # версия весов: то же имя с другими весами — другой штамп и пересчёт
    dim: int                                # размерность; typmod таблиц векторов и HNSW
    index_distance: str                     # cosine | dot | l2 — класс операторов индекса и оператор запроса
    normalize: bool
    max_tokens: int                         # где резать вход
    query_prefix: str                       # e5: 'query: '
    passage_prefix: str                     # e5: 'passage: '

class TextEmbedder(VectorEncoder[DenseVector]):
    """Реализация VectorEncoder для текстовых моделей: e5, bge, text-embedding-3.

    Префикс запроса, обрезка по max_tokens и нормировка — из секции модели
    в конфиге; сам расчёт — существующий порт boba.llm.embedding
    (fastembed локально или openai по HTTP).
    """

    def __init__(self, spec: EmbeddingModel, backend: Embedder[str]) -> None: ...

    async def encode(self, text: str) -> DenseVector:
        prefixed = self._spec.query_prefix + self._truncate(text)
        values = await self._backend.embed_query(prefixed)
        if self._spec.normalize:
            values = self._normalized(values)
        return DenseVector(values=values)

class ClipTextEncoder(VectorEncoder[DenseVector]):
    """Реализация VectorEncoder для моделей картинок (SigLIP, CLIP): текст в пространство картинок.

    Нужен, чтобы искать картинки словами: текстовая башня модели на
    onnxruntime даёт вектор, сравнимый с векторами картинок, которые стадия
    индексации вложений считает парным ImageEncoder той же модели.
    """

    def __init__(self, spec: EmbeddingModel, session: OnnxSession, tokenizer: Tokenizer) -> None: ...

    async def encode(self, text: str) -> DenseVector: ...

class SparseEncoder(VectorEncoder[SparseVector]):
    """Реализация VectorEncoder для разреженных моделей (SPLADE, BM42): текст в веса термов словаря модели, форма sparsevec."""

    def __init__(self, spec: EmbeddingModel, session: OnnxSession, tokenizer: Tokenizer) -> None: ...

    async def encode(self, text: str) -> SparseVector: ...

class PgVectorEncoderRegistry(VectorEncoderRegistry):
    """Реализация VectorEncoderRegistry: энкодеры по секциям [encoders.models] конфига корпуса.

    Собирается при старте, чтобы ошибка конфига вылезла сразу, а модели
    прогрелись один раз и жили весь прогон.
    """

    def dense(self, model: str) -> VectorEncoder[DenseVector]:
        if model not in self._dense:                    # собраны при старте по (modality, provider) строки
            raise EncoderConfigError(f"[encoders.models]: dense model {model!r} is not declared: known {sorted(self._dense)}")

        return self._dense[model]

    def sparse(self, model: str) -> VectorEncoder[SparseVector]:
        if model not in self._sparse:
            raise EncoderConfigError(f"[encoders.models]: sparse model {model!r} is not declared: known {sorted(self._sparse)}")

        return self._sparse[model]
```

Три модели в конфиге — локальная текстовая, картиночная и HTTP:

```toml
[encoders.models."multilingual-e5-large"]
    slug           = "e5"
    provider       = "local"
    modality       = "text"
    revision       = "2024-02"
    dim            = 1024
    index_distance = "cosine"
    normalize      = true
    max_tokens     = 512
    query_prefix   = "query: "
    passage_prefix = "passage: "
    model_dir      = "${env.models}/fastembed/multilingual-e5-large"
[encoders.models."siglip-so400m"]
    slug           = "siglip"
    provider       = "local"
    modality       = "image"
    revision       = "2024-01"
    dim            = 1152
    index_distance = "cosine"
    normalize      = true
    max_tokens     = 64
    model_dir      = "${env.models}/onnx/siglip-so400m"
[encoders.models."text-embedding-3-large"]
    slug           = "oai3large"
    provider       = "openai"
    modality       = "text"
    dim            = 3072
    index_distance = "cosine"
    normalize      = true
    max_tokens     = 8191
    http           = "${http}"
    base_url       = "${site.llm_url}"
    api_key        = "${site.llm_token}"
```

Векторы одной поверхности и одной модели лежат в своей таблице —
`page_section_vectors__e5`, `page_section_vectors__bge` — с колонкой
`vector(dim)` фиксированной размерности и обычным HNSW. Общая таблица с
именем модели в колонке и частичными индексами не работает: HNSW требует
typmod, у моделей он разный, а частичный индекс планировщик берёт лишь
при буквальном совпадении предиката, чего ни join по имени, ни параметр
в generic-плане psycopg не дают. Таблица на модель делает запрос чистым
index scan без фильтров.

Способ поиска или стадия `embed`, ссылающиеся на модель, которой нет в
`[encoders.models]`, — ошибка старта с именем модели: реестр не угадывает.

### 2.8 Протокол корпуса и документ узла

Протокол корпуса — вопросы, которые ядро задаёт источнику, и модели,
которыми источник отвечает. Ребро приходит черновиком `EdgeDraft` (2.4)
с адресом цели (наследник `Address`, 3.2, 4.2) и обоснованием
(наследник `Evidence`): id цели ядро находит само по `parts()`. Узел
целиком корпус отдаёт документом в виде для большой модели:
целиком корпус отдаёт документом в виде для большой модели (`NodeDocument`, 2.2.2).

**Документ узла.** Инструмент `kb_node` зовёт `Corpus.document(node_id, limit)`;
документ собирается из content tables, а не из источника — версия на
момент индексации. Что в него входит у каждого слоя — 3.6 и 4.7.

### 2.9 Реализация в Postgres

Пакет `boba-db-pggraph` — порты ядра на Postgres: DDL graph tables
(2.2–2.5), способы поиска, хранилище поиска и два бэкенда графа.
Пул и курсоры — из `boba-db-postgres`.

#### 2.9.1 Способы поиска

```python
# boba-db-pggraph — реализации Pg*Lookup: S = PgStatement, схема Postgres —
# поле schema каждой реализации, способ и шаблон — свойства класса, остальные поля — экземпляра.
# Объявления — frozen dataclass с явным наследованием протокола (правило §14:
# pydantic-модель протокол наследовать не может).
# Ошибки пакета наружу:
# SearchIndexError — у способа нет веса в конфиге или его таблица не найдена.
# EncoderConfigError — модели нет в [encoders.models] или её modality не сходится с запросом.
# SearchStoreError — запрос упал в Postgres; текст — подпись способа, таблица, ошибка psycopg.

@dataclass(frozen=True)
class PgStatement:
    """Готовый запрос psycopg: подзапрос, его параметры и подготовка сессии.

    Собирают Pg*Lookup.statement(), исполняет PgSearchStore; ядро видит
    его только как параметр типа S. setup — команды, которые надо выполнить
    в той же транзакции до запроса (set local …): их знает только способ,
    хранилище исполняет, не разбирая.
    """

    query: sql.Composed
    params: Mapping[str, object]
    setup: Sequence[sql.Composed] = ()

@dataclass(frozen=True)
class PgFtsLookup(IndexLookup[PgStatement]):
    """Способ поиска полнотекстом: tsvector с GIN, ранг ts_rank_cd, запрос на двух языках через websearch_to_tsquery."""

    METHOD: ClassVar[LookupMethod] = LookupMethod.FTS

    content: str                          # вид содержимого корпуса: title, section, summary — левая половина подписи
    schema: str                           # схема корпуса из [storage]: confluence | confluence_test — деталь реализации
    table: str                            # таблица content tables: page_sections
    node_column: str                      # колонка со ссылкой на nodes.id
    row_column: str                       # ключ строки: id у page_sections, node_id у pages
    text_column: str                      # колонка текста, отдаваемого в выдачу
    tsv_column: str

    def content_kind(self) -> str:
        return self.content

    def method(self) -> LookupMethod:
        return self.METHOD

    TEMPLATE: ClassVar[LiteralString] = """
        with q as (
            select
                websearch_to_tsquery('russian', unaccent(%(text)s))
                || websearch_to_tsquery('english', unaccent(%(text)s)) as tsq
        )
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as snippet,
            ts_rank_cd(t.{tsv}, q.tsq) as score
        from
            {schema}.{table} t,
            q
        where
            t.{tsv} @@ q.tsq
        order by
            score desc
        limit %(limit)s
    """

    async def statement(self, probe: Probe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column), tsv=sql.Identifier(self.tsv_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

class VectorText:
    """Текстовые формы pgvector: одна точка сборки литералов вектора для запросов."""

    @classmethod
    def dense(cls, vector: DenseVector) -> str:
        """'[v1,v2,…]' — форма vector."""
        ...

    @classmethod
    def sparse(cls, vector: SparseVector) -> str:
        """'{i1:v1,i2:v2,…}/dim' — форма sparsevec."""
        ...

@dataclass(frozen=True)
class PgVectorLookup(IndexLookup[PgStatement]):
    """Способ поиска по близости смысла: текст запроса кодируется энкодером модели таблицы, ближайшие векторы — через HNSW.

    Таблица векторов — на одну модель, поэтому фильтра по модели в запросе
    нет. Им же ищутся картинки: у SigLIP текст и картинка живут в одном
    пространстве, отличие только в энкодере и в том, что в выдачу идёт имя
    файла, а не текст. Отдельного класса под картинки нет.
    """

    METHOD: ClassVar[LookupMethod] = LookupMethod.VECTOR

    content: str
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str                      # что показать как цитату: текст раздела, имя файла у картинки
    vector_table: str                     # таблица векторов этого содержимого и этой модели: page_section_vectors__e5
    ref_column: str                       # ссылка на row_column
    encoder: VectorEncoder[DenseVector]   # энкодер модели этой таблицы; корпус взял его из реестра при старте

    def content_kind(self) -> str:
        return self.content

    def method(self) -> LookupMethod:
        return self.METHOD

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as snippet,
            1 - (v.embedding <=> {vector}::vector) as score   -- косинусная близость: больше — лучше
        from
            {schema}.{vectors} v
            join {schema}.{table} t on
                t.{row} = v.{ref}
        order by
            v.embedding <=> {vector}::vector
        limit %(limit)s
    """

    async def statement(self, probe: Probe) -> PgStatement:
        vector = await self.encoder.encode(probe.text)
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), vectors=sql.Identifier(self.vector_table),
            table=sql.Identifier(self.table), node=sql.Identifier(self.node_column),
            row=sql.Identifier(self.row_column), text=sql.Identifier(self.text_column),
            ref=sql.Identifier(self.ref_column), vector=sql.Literal(VectorText.dense(vector)),
        )
        return PgStatement(query=composed, params={"limit": probe.limit}, setup=(self._ef_search(probe.limit),))
        # вектор — литерал, а не параметр: ветки разных моделей в одном запросе (8.1) не делят имя параметра

    EF_SEARCH_DEFAULT: ClassVar[int] = 40    # HNSW отдаёт не больше ef_search строк за скан; дефолт pgvector

    def _ef_search(self, limit: int) -> sql.Composed:
        """Иначе limit 50 молча вернёт 40 строк."""
        return sql.SQL("set local hnsw.ef_search = {ef}").format(ef=sql.Literal(max(limit, self.EF_SEARCH_DEFAULT)))

@dataclass(frozen=True)
class PgTrigramLookup(IndexLookup[PgStatement]):
    """Способ поиска по триграммам (pg_trgm, GiST): опечатки, склонения, части имён.

    GiST, а не GIN: только он отдаёт top-N по оператору <-> прямо из
    индекса; оператор % отсекает мусор по pg_trgm.similarity_threshold
    (%% в шаблоне — экранированный %).
    """

    METHOD: ClassVar[LookupMethod] = LookupMethod.TRIGRAM

    content: str
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str

    def content_kind(self) -> str:
        return self.content

    def method(self) -> LookupMethod:
        return self.METHOD

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as snippet,
            1 - (t.{text} <-> %(text)s) as score
        from
            {schema}.{table} t
        where
            t.{text} %% %(text)s
        order by
            t.{text} <-> %(text)s
        limit %(limit)s
    """

    async def statement(self, probe: Probe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

@dataclass(frozen=True)
class PgExactLookup(IndexLookup[PgStatement]):
    """Способ поиска точным совпадением по lower(text) через btree: имена узлов для роли naming, коды вида FLIP-457."""

    METHOD: ClassVar[LookupMethod] = LookupMethod.EXACT

    content: str
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str

    def content_kind(self) -> str:
        return self.content

    def method(self) -> LookupMethod:
        return self.METHOD

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as snippet,
            1.0 as score
        from
            {schema}.{table} t
        where
            lower(t.{text}) = lower(%(text)s)
        limit %(limit)s
    """

    async def statement(self, probe: Probe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

@dataclass(frozen=True)
class PgBm25Lookup(IndexLookup[PgStatement]):
    """Способ поиска BM25 через pg_search (ParadeDB): полнотекст с нормировкой по длине; объявляется, только если расширение стоит."""

    METHOD: ClassVar[LookupMethod] = LookupMethod.BM25

    content: str
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str
    index_name: str                        # индекс bm25 над таблицей; нужен установке, запрос идёт через оператор @@@

    def content_kind(self) -> str:
        return self.content

    def method(self) -> LookupMethod:
        return self.METHOD

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as snippet,
            paradedb.score(t.{row}) as score
        from
            {schema}.{table} t
        where
            t.{text} @@@ %(text)s
        order by
            score desc
        limit %(limit)s
    """

    async def statement(self, probe: Probe) -> PgStatement:
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), table=sql.Identifier(self.table),
            node=sql.Identifier(self.node_column), row=sql.Identifier(self.row_column),
            text=sql.Identifier(self.text_column),
        )
        return PgStatement(query=composed, params={"text": probe.text, "limit": probe.limit})

@dataclass(frozen=True)
class PgSparseLookup(IndexLookup[PgStatement]):
    """Способ поиска разреженным вектором (SPLADE, BM42): pgvector sparsevec с HNSW, таблица на модель.

    Оператор <#> — отрицательное скалярное произведение: меньше — ближе,
    поэтому score берётся с минусом.
    """

    METHOD: ClassVar[LookupMethod] = LookupMethod.SPARSE

    content: str
    schema: str
    table: str
    node_column: str
    row_column: str
    text_column: str
    vector_table: str
    ref_column: str
    encoder: VectorEncoder[SparseVector]

    def content_kind(self) -> str:
        return self.content

    def method(self) -> LookupMethod:
        return self.METHOD

    TEMPLATE: ClassVar[LiteralString] = """
        select
            t.{node} as node_id,
            t.{row} as row_id,
            t.{text} as snippet,
            -(v.embedding <#> {vector}::sparsevec) as score    -- <#> отрицательно: минус даёт «больше — лучше»
        from
            {schema}.{vectors} v
            join {schema}.{table} t on
                t.{row} = v.{ref}
        order by
            v.embedding <#> {vector}::sparsevec
        limit %(limit)s
    """

    async def statement(self, probe: Probe) -> PgStatement:
        vector = await self.encoder.encode(probe.text)
        composed = sql.SQL(self.TEMPLATE).format(
            schema=sql.Identifier(self.schema), vectors=sql.Identifier(self.vector_table),
            table=sql.Identifier(self.table), node=sql.Identifier(self.node_column),
            row=sql.Identifier(self.row_column), text=sql.Identifier(self.text_column),
            ref=sql.Identifier(self.ref_column), vector=sql.Literal(VectorText.sparse(vector)),
        )
        return PgStatement(query=composed, params={"limit": probe.limit}, setup=(self._ef_search(probe.limit),))

    EF_SEARCH_DEFAULT: ClassVar[int] = 40

    def _ef_search(self, limit: int) -> sql.Composed:
        return sql.SQL("set local hnsw.ef_search = {ef}").format(ef=sql.Literal(max(limit, self.EF_SEARCH_DEFAULT)))
```

Имена таблиц и колонок подставляются как `sql.Identifier`, значения — как
параметры: инъекции через объявление нет по построению, а `LiteralString`
в `ClassVar` не даёт собрать шаблон из строк на лету. Один класс
обслуживает любое содержимое: `PgFtsLookup` для `pages.title_tsv` и для
`page_sections.tsv` — два экземпляра с разным `content`. Новый способ
поиска — новый класс с полями и `TEMPLATE`; ни ядро, ни `SearchStore` не
меняются. Общего класса местоположения нет: у полнотекстового способа своя
колонка `tsvector`, у векторного — своя таблица векторов и энкодер, и
каждый объявляет свои поля сам.

#### 2.9.2 Хранилище поиска

Реализация `SearchStore` над psycopg. Главное в ней — `candidates`: не
двенадцать запросов и слияние в Python, а один SQL, в который подзапросы
способов вложены ветками `union all`, а слияние RRF, отбор фрагментов,
`kind`, адрес и метрики считаются в базе. В Python приходят только строки,
которые пойдут в выдачу. Сам алгоритм и полный текст запроса — в разделе 8;
здесь то, как хранилище его собирает.

```python
class PgSearchStore(SearchStore[PgStatement]):
    """Реализация SearchStore над psycopg: один запрос кандидатов на все способы корпуса.

    Одно хранилище на корпус: схема и параметры поиска — из его конфига.
    Пул — общий пул приложения.
    """

    BRANCH: ClassVar[LiteralString] = """
        select
            {label} as lookup,
            {content} as content_kind,
            {weight}::real as weight,
            q.node_id,
            q.row_id,
            q.snippet,
            row_number() over (order by q.score desc) as rank
        from
            ({body}) q
    """

    def __init__(self, pool: AsyncConnectionPool, schema: str, cfg: SearchConfig) -> None:
        self._pool = pool
        self._schema = schema
        self._cfg = cfg

    async def candidates(self, corpus: Corpus[PgStatement], query: str) -> Sequence[Candidate]:
        """Запрос 1 алгоритма поиска: ветки способов -> RRF -> фрагменты -> nodes, ranks.

        Подзапросы способов собираются параллельно: векторные считают
        вектор своим энкодером здесь. Параметры text и limit у всех веток
        общие — зонд один; вектор способ подставляет литералом, поэтому
        имена параметров между ветками не сталкиваются. Подготовку сессии
        (setup) веток хранилище исполняет в той же транзакции до запроса.
        """
        weights = corpus.search_weights()
        probe = Probe(text=query, limit=self._cfg.candidates)
        lookups = corpus.lookups()

        builds: list[Awaitable[PgStatement]] = []
        for lookup in lookups:
            builds.append(lookup.statement(probe))

        statements = await asyncio.gather(*builds)
        branches: list[sql.Composed] = []
        params: dict[str, object] = {
            "rrf_k": self._cfg.rrf_k,
            "seed_k": self._cfg.seed_k,
            "per_node": self._cfg.snippets_per_node,
            "metrics": list(self._cfg.metrics),
        }
        for lookup, statement in zip(lookups, statements, strict=True):
            label = self._label(lookup, weights)
            branches.append(
                sql.SQL(self.BRANCH).format(
                    label=sql.Literal(label),
                    content=sql.Literal(lookup.content_kind()),
                    weight=sql.Literal(weights[label]),
                    body=statement.query,
                )
            )
            params.update(statement.params)

        composed = sql.SQL(CandidatesQuery.TEMPLATE).format(
            schema=sql.Identifier(self._schema),
            branches=sql.SQL("\n        union all\n").join(branches),
        )
        setup: list[sql.Composed] = []
        for statement in statements:
            setup.extend(statement.setup)

        async with self._pool.connection() as conn, conn.transaction():
            for command in setup:                    # подготовка сессии от способов: что в ней, хранилище не знает
                await conn.execute(command)

            cursor = await conn.execute(composed, params)
            rows = await cursor.fetchall()

        found: list[Candidate] = []
        for row in rows:
            found.append(Candidate.model_validate(row))

        return found

    def _label(self, lookup: IndexLookup[PgStatement], weights: Mapping[str, float]) -> str:
        """Подпись способа; заодно проверка, что вес для неё объявлен — пропуск в конфиге молча обнулил бы ветку."""
        label = LookupMethod.label_of(lookup.content_kind(), lookup.method())
        if label not in weights:
            raise SearchIndexError(
                f"lookup {label!r}: no weight in [search.weights]: known {sorted(weights)}"
            )

        return label

    async def rows(self, statement: PgStatement) -> Sequence[LookupRow]:
        """Один способ сам по себе: для рёбер similar и mention, которые строит ядро."""
        async with self._pool.connection() as conn, conn.transaction():
            for command in statement.setup:
                await conn.execute(command)

            cursor = await conn.execute(statement.query, statement.params)
            rows = await cursor.fetchall()

        found: list[LookupRow] = []
        for row in rows:
            found.append(LookupRow.model_validate(row))

        return found

# ребро similar: тем способом, который корпус назначил роли, по тексту самого узла; score строки — косинус для обоснования
roles = corpus.role_lookups()
statement = await roles[LookupRole.SIMILARITY].statement(Probe(text=corpus.similar_text(node_id), limit=similar_top_k))
neighbours = await store.rows(statement)

# ребро mention: заголовок другого узла, точное совпадение
statement = await roles[LookupRole.NAMING].statement(Probe(text=title, limit=1))
named = await store.rows(statement)
```

#### 2.9.3 Бэкенды графа

Всё, кроме рёбер, всегда реляционное. Бэкенд выбирает
только, где живут рёбра и как выполняется обход; порт — `GraphStore` (2.4).

**Реляционный бэкенд** — таблица `edges` (2.4), представление `adjacency`,
рекурсивный CTE (раздел 8). Целостность — внешними ключами.

**Бэкенд AGE.** Граф AGE физически — отдельная схема Postgres с именем
графа, поэтому граф зовётся `<схема>_graph`: `confluence_graph` рядом с
`confluence`. Внутри AGE создаёт по таблице на метку:

| таблица AGE | что в ней |
|---|---|
| `confluence_graph._ag_label_vertex` | все вершины |
| `confluence_graph._ag_label_edge` | все рёбра |
| `confluence_graph.node` | вершины метки `node`: `id graphid`, `properties agtype` |
| `confluence_graph.link` | рёбра метки `link`: `id`, `start_id`, `end_id`, `properties agtype` |
| `confluence_graph.mention` | по таблице на каждый вид ребра корпуса |

Раскладка ядра на AGE:

- одна метка вершин `node`, свойства `{node_id, kind}`; `node_id` —
  мост к реляционной части, по нему индекс
  `create index on confluence_graph.node (ag_catalog.agtype_access_operator(properties, '"node_id"'))`;
- метка ребра = вид ребра корпуса; свойства `{weight, evidence, computed_at}`;
  симметричные виды хранятся одним ребром, обход идёт без направления;
- вершина создаётся при upsert узла и удаляется `DETACH DELETE` при
  удалении узла — внешних ключей между `nodes` и вершинами нет, за
  согласованность отвечает `GraphStore.age`, а инструмент `kb_graph_check`
  сверяет число узлов и вершин и чинит расхождение;
- обход — `cypher()` внутри того же SQL, что и pgvector; текст запроса
  расширения для обоих бэкендов — в разделе 9.

Что даёт AGE сверх реляционного: обход переменной длины и паттерны путей
(«таблицы, к которым от этой ведёт цепочка `view_source` любой длины»)
пишутся одной строкой Cypher вместо рекурсивного CTE на каждый вопрос.
Что стоит: нет внешних ключей, свой тип `agtype` на границе, отдельная
схема на корпус. Ранжирование получает от порта одни и те же `scores` и о
бэкенде не знает.

Выбор — `[graph] backend = "relational" | "age"`; установка проверяет
наличие расширения и падает с внятной ошибкой, если выбранного нет.

## 3. Слой Confluence

Content tables — таблицы слоя в той же схеме; ниже слой Confluence, затем Warehouse (раздел 4) и Entity (раздел 5). Здесь лежит всё содержимое узла:
структурные атрибуты, тексты со своими полнотекстовыми индексами и
векторные таблицы к ним. Каждая текстовая таблица — со своей структурой:
у раздела страницы — оригинал и markdown, у саммари — модель и промпт, у
комментария колонки — тип и позиция; ничего не сплющивается в общую
строку. Строки content tables ссылаются на `nodes.id` с каскадом.

### 3.1 Обзор

Узлы слоя — пространство, страница, вложение. Таблицы слоя:

| таблица | что хранит |
|---|---|
| `pages` | страница: заголовок, версия, автор, крошки, метки, оглавление |
| `page_sections`, `page_section_vectors__<slug>` | разделы и таблицы страницы: оригинал, markdown, векторы |
| `page_summaries`, `page_summary_vectors__<slug>` | саммари страницы языковой моделью |
| `attachments` | вложение: файл, тип, размер, версия |
| `attachment_texts`, `attachment_text_vectors__<slug>` | текстовый слой или OCR вложения по страницам документа |
| `attachment_captions`, `attachment_caption_vectors__<slug>` | описание картинки моделью зрения |
| `attachment_images`, `attachment_image_vectors__<slug>` | картинка вложения и её вектор SigLIP |
| `pending_links` | ссылки на страницы, которых в корпусе ещё нет |

Модели слоя — в `boba-confluence`: узлы и адреса (3.2), виды содержимого
(3.3), корпус (3.4), рёбра (3.5); аспекты и способы обработки — 2.3.

### 3.2 Модели узлов и адресов

Виды узлов, адреса и узлы страниц — в `boba-confluence`, рядом с остальным
знанием о Confluence; протокол `Address` ядра (2.2) они наследуют явно,
сборка и разбор строки — `httpx.URL`, который в пакете уже есть.

```python
# boba-confluence: boba/confluence/nodes.py
class ConfluenceNodeKind(StrEnum):
    SPACE = "confluence_space"
    PAGE = "confluence_page"
    ATTACHMENT = "confluence_attachment"

class WebScheme(StrEnum):
    """Схемы web-адресов Confluence; у каждой свой порт по умолчанию, который в строке опускается."""

    HTTP = "http"
    HTTPS = "https"

    def default_port(self) -> int:
        if self is WebScheme.HTTP:
            return 80

        return 443

class ConfluenceAddress(Address):
    """Адрес объекта Confluence: REST-путь на сервере.

    Части — схема, хост, порт, путь; query, фрагмент и учётные данные в
    адрес не входят (как у SourceId ридера). Порт в частях всегда, в строке
    httpx опускает порт по умолчанию схемы: https://host/path. Сборка и
    разбор — httpx.URL, единственное место для адресов Confluence.
    """

    scheme: WebScheme
    host: str
    port: int
    path: str

    def render(self) -> str:
        url = httpx.URL(scheme=self.scheme.value, host=self.host, port=self.port, path=self.path)
        return str(url)

    @classmethod
    def parse(cls, text: str) -> Self:
        try:
            url = httpx.URL(text)
        except httpx.InvalidURL as exc:
            raise AddressError(f"confluence address {text!r}: {exc}") from exc

        if url.userinfo:
            raise AddressError(f"confluence address {text!r}: credentials are not part of an address")

        if url.query:
            raise AddressError(f"confluence address {text!r}: query is not part of an address")

        if url.fragment:
            raise AddressError(f"confluence address {text!r}: fragment is not part of an address")

        if not url.host:
            raise AddressError(f"confluence address {text!r}: host is required")

        try:
            scheme = WebScheme(url.scheme)
        except ValueError as exc:
            raise AddressError(f"confluence address {text!r}: expected scheme http or https, got {url.scheme!r}") from exc

        port = url.port
        if port is None:
            port = scheme.default_port()

        try:
            return cls(scheme=scheme, host=url.host, port=port, path=url.path)
        except ValidationError as exc:
            raise AddressError(f"{cls.__name__}: address {text!r} is not valid: {exc}") from exc

class SpaceAddress(ConfluenceAddress):
    PATH_RE: ClassVar[re.Pattern[str]] = re.compile(r"/rest/api/space/[^/?#]+$")

    @field_validator("path")
    @classmethod
    def _space_path(cls, value: str) -> str:
        if cls.PATH_RE.search(value) is None:
            raise ValueError(f"confluence space address expects /rest/api/space/<key>, got {value!r}")

        return value

class PageAddress(ConfluenceAddress):
    PATH_RE: ClassVar[re.Pattern[str]] = re.compile(r"/rest/api/content/[^/?#]+$")   # тот же шаблон, что у SourceId.page_id_of

    @field_validator("path")
    @classmethod
    def _content_path(cls, value: str) -> str:
        if cls.PATH_RE.search(value) is None:
            raise ValueError(f"confluence page address expects /rest/api/content/<id>, got {value!r}")

        return value

class AttachmentAddress(ConfluenceAddress):
    PATH_RE: ClassVar[re.Pattern[str]] = re.compile(r"/download/attachments/[^/?#]+/[^/?#]+$")

    @field_validator("path")
    @classmethod
    def _download_path(cls, value: str) -> str:
        if cls.PATH_RE.search(value) is None:
            raise ValueError(f"confluence attachment address expects /download/attachments/<page>/<file>, got {value!r}")

        return value

class SpaceNode(BaseModel):
    kind: Literal[ConfluenceNodeKind.SPACE]
    address: SpaceAddress

class PageNode(BaseModel):
    kind: Literal[ConfluenceNodeKind.PAGE]
    address: PageAddress

class AttachmentNode(BaseModel):
    kind: Literal[ConfluenceNodeKind.ATTACHMENT]
    address: AttachmentAddress

ConfluenceNode = Annotated[SpaceNode | PageNode | AttachmentNode, Field(discriminator="kind")]
```

### 3.3 Content tables

Виды узлов и адреса — `ConfluenceNodeKind` из `boba-confluence` (3.2);
корпус объявляет виды своих текстов, по таблице на вид:

```python
# boba-confluence: boba/confluence/graph.py — подмодуль за extra graph
class ConfluenceContentKind(StrEnum):
    TITLE = "title"
    OUTLINE = "outline"
    SECTION = "section"
    ATTACHMENT_TEXT = "attachment_text"   # текстовый слой и OCR
    CAPTION = "caption"                   # описание картинки моделью зрения
    IMAGE = "image"                       # сама картинка: вектор SigLIP, текста нет
    SUMMARY = "summary"
```

#### 3.3.1 `pages`

```sql
create table pages (
    node_id       bigint      primary key references nodes on delete cascade,
    page_id       text        not null unique,
    space         text        not null,
    title         text        not null,
    version       int         not null,
    modified_at   timestamptz,
    author        text        not null default '',
    url           text        not null,           -- webui, для человека и модели
    breadcrumb    text[]      not null default '{}',
    labels        text[]      not null default '{}',
    outline       jsonb       not null default '[]',   -- [{level, text, anchor}]
    outline_text  text        not null default '',    -- крошки, метки, оглавление одной строкой для поиска
    body_format   text        not null,
    title_tsv     tsvector    generated always as (to_tsvector('simple', unaccent(title))) stored,
    outline_tsv   tsvector    generated always as (to_tsvector('russian', unaccent(outline_text)) || to_tsvector('english', unaccent(outline_text))) stored
);
create index on pages (lower(title));                         -- PgExactLookup: lower(title) = lower(q)
create index on pages using gist (title gist_trgm_ops);        -- PgTrigramLookup: title <-> q, top-N из индекса
create index on pages using gin (title_tsv);                   -- PgFtsLookup
create index on pages using gin (outline_tsv);
```

#### 3.3.2 `page_sections`

```sql
create table page_sections (                   -- текст страницы по разделам и таблицам
    id            bigserial   primary key,
    node_id       bigint      not null references nodes on delete cascade,
    ordinal       int         not null,
    kind          text        not null,   -- section | table
    heading_path  text        not null default '',
    anchor        text        not null default '',
    raw_content   text        not null,   -- HTML раздела как в источнике
    format_content text       not null,   -- markdown: отдаётся модели и эмбеддится
    metadata      jsonb       not null default '{}',   -- у таблицы: подпись, колонки, раскладка
    content_sha256 text       not null,
    tsv           tsvector    generated always as (...format_content...) stored,
    unique (node_id, ordinal)
);
create index on page_sections using gin (tsv);
create table page_section_vectors__e5 (        -- на модель: имя = поверхность + "__" + slug модели, dim — из её конфига
    section_id    bigint      primary key references page_sections on delete cascade,
    embedding     vector(1024) not null
);
create index on page_section_vectors__e5 using hnsw (embedding vector_cosine_ops);
```

#### 3.3.3 `page_summaries`

Саммари страницы языковой моделью, с именем модели и хэшем промпта,
чтобы смена того или другого была видна.

```sql
create table page_summaries (
    node_id            bigint  primary key references nodes on delete cascade,
    summary            text    not null,
    topics             text[]  not null default '{}',
    model              text    not null,
    system_prompt_hash text    not null,
    tsv                tsvector generated always as (...summary...) stored,
    updated_at         timestamptz not null default now()
);
create index on page_summaries using gin (tsv);
create table page_summary_vectors__e5 (
    node_id       bigint      primary key references page_summaries on delete cascade,
    embedding     vector(1024) not null
);
create index on page_summary_vectors__e5 using hnsw (embedding vector_cosine_ops);
```

#### 3.3.4 Вложения: `attachments`, `attachment_texts`, `attachment_captions`, `attachment_images`

Вложение — отдельный узел. Его файл описан в `attachments`, а
содержимое — тремя таблицами разной природы: текст (текстовый слой или
OCR) по страницам документа, описание картинки моделью зрения и сама
картинка с вектором SigLIP.

```sql
create table attachments (
    node_id       bigint      primary key references nodes on delete cascade,
    attachment_id text        not null unique,
    title         text        not null,   -- имя файла
    media_type    text        not null,
    file_size     bigint      not null,
    version       int         not null,
    download_path text        not null
);
create table attachment_texts (                -- текст документа: текстовый слой или OCR
    id            bigserial   primary key,
    node_id       bigint      not null references nodes on delete cascade,
    method        text        not null,   -- text | ocr — чем получен; признак происхождения, не фильтр поиска
    ordinal       int         not null,   -- страница документа
    content       text        not null,
    tsv           tsvector    generated always as (...) stored,
    unique (node_id, ordinal)
);
create index on attachment_texts using gin (tsv);
create table attachment_text_vectors__e5 (
    text_id       bigint      primary key references attachment_texts on delete cascade,
    embedding     vector(1024) not null
);
create index on attachment_text_vectors__e5 using hnsw (embedding vector_cosine_ops);
create table attachment_captions (             -- описание картинки моделью зрения: другая природа, другой вес
    node_id       bigint      primary key references nodes on delete cascade,
    caption       text        not null,
    model         text        not null,
    system_prompt_hash text   not null,
    tsv           tsvector    generated always as (...) stored
);
create index on attachment_captions using gin (tsv);
create table attachment_caption_vectors__e5 (
    node_id       bigint      primary key references attachment_captions on delete cascade,
    embedding     vector(1024) not null
);
create index on attachment_caption_vectors__e5 using hnsw (embedding vector_cosine_ops);

create table attachment_images (           -- картинки вложений: поиск по смыслу изображения, не текста
    id            bigserial   primary key,
    node_id       bigint      not null references nodes on delete cascade,
    title         text        not null,   -- имя файла: единственный текст, который есть у картинки
    content       bytea       not null,   -- байты; их читает стадия индексации (ImageEncoder), не поиск
    width         int         not null,
    height        int         not null
);
create table attachment_image_vectors__siglip (   -- модель modality = image, dim 1152
    image_id      bigint      primary key references attachment_images on delete cascade,
    embedding     vector(1152) not null
);
create index on attachment_image_vectors__siglip using hnsw (embedding vector_cosine_ops);
```

Индексация зеркальна поиску: стадия `embed` берёт `content` из
`attachment_images`, зовёт `ImageEncoder` той же модели
(`encode(image: bytes) -> DenseVector`) и пишет в `attachment_image_vectors`;
поиск считает зонд из текста `ClipTextEncoder`. Одна модель, два энкодера,
одна секция конфига.

#### 3.3.5 `pending_links`

`pending_links` — ссылки на страницы, которых в корпусе ещё нет; когда
цель индексируется, они становятся рёбрами `link`.

```sql
create table pending_links (
    node_id        bigint     not null references nodes on delete cascade,
    target_title   text       not null,
    target_page_id text       not null default '',
    anchor_text    text       not null default '',
    primary key (node_id, target_title)
);
```

### 3.4 Способы поиска

Индексы, которые корпус объявляет ядру:

```python
class ConfluenceCorpus(Corpus[PgStatement]):
    """Реализация Corpus для Confluence: способы поиска над своими content tables, роли, веса, имена рёбер, тексты узла, документ."""

    def __init__(self, cfg: ConfluenceCorpusConfig, encoders: VectorEncoderRegistry) -> None:
        self._cfg = cfg
        schema = cfg.storage.pg_schema
        e5 = encoders.dense(cfg.embedding.model)          # нет модели в [encoders.models] — падение на старте
        siglip = encoders.dense(cfg.embedding.image_model)
        # имена таблиц векторов — содержимое плюс slug модели из её конфига

        title_exact = PgExactLookup(content=ConfluenceContentKind.TITLE, schema=schema, table="pages",
                                    node_column="node_id", row_column="node_id", text_column="title")
        summary_vector = PgVectorLookup(content=ConfluenceContentKind.SUMMARY, schema=schema, table="page_summaries",
                                        node_column="node_id", row_column="node_id", text_column="summary",
                                        vector_table="page_summary_vectors__e5", ref_column="node_id", encoder=e5)

        self._lookups: Sequence[IndexLookup[PgStatement]] = (
            PgFtsLookup(content=ConfluenceContentKind.TITLE, schema=schema, table="pages",
                        node_column="node_id", row_column="node_id", text_column="title", tsv_column="title_tsv"),
            PgTrigramLookup(content=ConfluenceContentKind.TITLE, schema=schema, table="pages",
                            node_column="node_id", row_column="node_id", text_column="title"),
            title_exact,
            PgFtsLookup(content=ConfluenceContentKind.OUTLINE, schema=schema, table="pages",
                        node_column="node_id", row_column="node_id", text_column="outline_text", tsv_column="outline_tsv"),
            PgFtsLookup(content=ConfluenceContentKind.SECTION, schema=schema, table="page_sections",
                        node_column="node_id", row_column="id", text_column="format_content", tsv_column="tsv"),
            PgVectorLookup(content=ConfluenceContentKind.SECTION, schema=schema, table="page_sections",
                           node_column="node_id", row_column="id", text_column="format_content",
                           vector_table="page_section_vectors__e5", ref_column="section_id", encoder=e5),
            PgFtsLookup(content=ConfluenceContentKind.SUMMARY, schema=schema, table="page_summaries",
                        node_column="node_id", row_column="node_id", text_column="summary", tsv_column="tsv"),
            summary_vector,
            PgFtsLookup(content=ConfluenceContentKind.ATTACHMENT_TEXT, schema=schema, table="attachment_texts",
                        node_column="node_id", row_column="id", text_column="content", tsv_column="tsv"),
            PgVectorLookup(content=ConfluenceContentKind.ATTACHMENT_TEXT, schema=schema, table="attachment_texts",
                           node_column="node_id", row_column="id", text_column="content",
                           vector_table="attachment_text_vectors__e5", ref_column="text_id", encoder=e5),
            PgFtsLookup(content=ConfluenceContentKind.CAPTION, schema=schema, table="attachment_captions",
                        node_column="node_id", row_column="node_id", text_column="caption", tsv_column="tsv"),
            PgVectorLookup(content=ConfluenceContentKind.CAPTION, schema=schema, table="attachment_captions",
                           node_column="node_id", row_column="node_id", text_column="caption",
                           vector_table="attachment_caption_vectors__e5", ref_column="node_id", encoder=e5),
            PgVectorLookup(content=ConfluenceContentKind.IMAGE, schema=schema, table="attachment_images",
                           node_column="node_id", row_column="id", text_column="title",
                           vector_table="attachment_image_vectors__siglip", ref_column="image_id", encoder=siglip),
            *PgEntityLookups.of(schema),                      # слой сущностей: entity/exact, entity/trigram
        )
        self._roles: Mapping[LookupRole, IndexLookup[PgStatement]] = {
            LookupRole.NAMING: title_exact,
            LookupRole.SIMILARITY: summary_vector,
        }

    def lookups(self) -> Sequence[IndexLookup[PgStatement]]:
        return self._lookups

    def role_lookups(self) -> Mapping[LookupRole, IndexLookup[PgStatement]]:
        return self._roles

    def computed_edge_kinds(self) -> Mapping[ComputedEdge, str]:
        return {
            ComputedEdge.ENTITY: ConfluenceEdgeKind.ENTITY,
            ComputedEdge.SIMILAR: ConfluenceEdgeKind.SIMILAR,
        }

    def search_weights(self) -> Mapping[str, float]:
        return self._cfg.search.weights               # [search.weights] "title/exact" = 3.0 …
```

Способы создаются корпусом при старте, а не константами модуля: схема и
энкодеры приходят из конфига, объявление их не знает; веса при способах не
лежат, их отдаёт `search_weights()` по подписи. Подпись каждого способа
собирается из пары «содержимое/способ», поэтому `title` встречается трижды
с разными методами, а `section` дважды, и ключи `[search.weights]` в
точности повторяют эти пары.

Роли — это те же объекты из списка, отмеченные по назначению: ядро ищет
имя узла точным совпадением по заголовку, а похожие узлы — вектором
саммари. Меняется назначение — меняется одна строка таблицы ролей, список
способов остаётся прежним.

Повторы `table`/`node_column`/`row_column` в объявлениях — намеренные:
каждый индекс читается сам по себе, без поиска общего определения. 

### 3.5 Рёбра

Виды связей, которые корпус Confluence видит в источнике или считает сам,
и обоснования к ним:

```python
class ConfluenceEdgeKind(StrEnum):
    IN_SPACE = "in_space"              # пространство → страница
    CHILD_PAGE = "child_page"          # страница → дочерняя страница в дереве пространства
    HAS_ATTACHMENT = "has_attachment"  # страница → вложение
    LINK = "link"                      # ссылка на страницу в теле
    ATTACHMENT_REF = "attachment_ref"  # ссылка на вложение другой страницы
    MENTION = "mention"                # заголовок другой страницы встретился в тексте
    SERIES = "series"                  # общий код серии в заголовках: FLIP-457 и FLIP-458
    ENTITY = "entity"                  # страница → сущность, которую упоминает   (считает ядро)
    SIMILAR = "similar"                # близость векторов                     (считает ядро)
    SAME_AUTHOR = "same_author"        # один автор последней правки

# boba-confluence, подмодуль graph: обоснования явных рёбер
class LinkEvidence(Evidence):
    """Обоснование ребра link: ссылка в теле страницы — её якорь, фраза вокруг и раздел, где она стоит."""

    anchor: str
    phrase: str
    section_id: int

class MentionEvidence(Evidence):
    """Обоснование ребра mention: заголовок другого узла встретился в тексте — сколько раз, в каком разделе, при каком пороге длины."""

    title: str
    occurrences: int
    section_id: int
    min_words: int                      # mention_min_words на момент расчёта

class SeriesEvidence(Evidence):
    """Обоснование ребра series: общий код серии в заголовках (FLIP-457 и FLIP-458) — префикс и номера."""

    prefix: str
    numbers: Sequence[int]

class SameAuthorEvidence(Evidence):
    """Обоснование ребра same_author: логин автора последней правки, общий у двух страниц."""

    author: str
```

Как считается каждое:

- `link`, `attachment_ref` — корпус, из разобранной страницы: каждая
  ссылка в теле даёт якорь и фразу вокруг него; адрес цели разрешается в
  узел, а если цели ещё нет, ссылка ждёт в `pending_links` и ребро
  строится при её появлении.
- `mention` — корпус: заголовки других узлов ищутся в тексте страницы
  способом роли `naming`, точным совпадением; заголовок короче
  `mention_min_words` не считается.
- `series` — корпус: код серии из заголовка регулярным выражением; общий
  префикс у двух страниц.
- `in_space`, `child_page`, `has_attachment`, `contains` — факт из
  метаданных источника, `FactEvidence`; `same_author` — логин автора.

### 3.6 Документ узла

Страница — разделы по порядку с вложениями на своих местах
(`NodePart`); вложение — разобранным текстом или подписью картинки.
Собирается из `pages`, `page_sections`, `attachments` и текстов вложений
(2.8).

## 4. Слой Warehouse

### 4.1 Обзор

Индексатор Warehouse получает подключение и обходит системный каталог
движка: `pg_catalog` в PostgreSQL, `sys.*` в MSSQL, `system.tables` /
`system.columns` в ClickHouse, `ALL_*` в Oracle, `information_schema` в
MySQL. У движков разные наборы объектов и разные слова для одного и того
же, поэтому content tables описывают объекты в терминах общей модели отношения, а
всё, чему нет места в общей модели, кладёт в `properties` движка. За
разбор каталога отвечает интроспектор движка — по классу на движок.

Таблицы слоя:

| таблица | что хранит |
|---|---|
| `relations` | отношение любого движка: таблица, представление, matview, словарь — определение, комментарий, оценка строк, свойства движка |
| `relation_ddl`, `relation_column_lists`, `relation_profiles`, `relation_samples`, `relation_summaries` и их `*_vectors__<slug>` | тексты отношения по виду: DDL, состав колонок, профиль, пример строк, саммари |
| `columns`, `column_comment_vectors__<slug>` | колонка: тип в движке и канонический, комментарий |
| `column_profiles` | профиль данных колонки по выборке: доли, границы, частые значения, minhash |
| `constraints` | ограничения: ключи, уникальность, check |
| `indexes` | индексы отношения |
| `routines` | функции и процедуры: сигнатура, тело, что читают и пишут |

Модели: узлы и адреса — в пакетах движков (4.2), виды содержимого и порт
каталога — 4.4, рёбра — 4.6, аспекты — 2.3.

### 4.2 Модели узлов и адресов

Виды узлов и адреса объектов каталога — в пакетах движков: `boba-db-postgres`
и `boba-db-clickhouse`; движки без пакета (MSSQL, Oracle, MySQL) придут со
своими `boba-db-*` и своими перечислениями, общего перечисления всех
движков не будет ни в одном пакете. Грамматика строки адреса — у базы
адресов пакета, на `urllib.parse`; строку без знания объекта разбирают
`PgAddresses.parse` и `ChAddresses.parse` по составу ролей.

```python
# boba-db-postgres: boba/db/postgres/nodes.py
class PgNodeKind(StrEnum):
    DATABASE = "pg_database"
    SCHEMA = "pg_schema"
    TABLE = "pg_table"
    VIEW = "pg_view"
    MATVIEW = "pg_matview"
    COLUMN = "pg_column"
    INDEX = "pg_index"
    CONSTRAINT = "pg_constraint"
    FUNCTION = "pg_function"
    PROCEDURE = "pg_procedure"
    TRIGGER = "pg_trigger"
    SEQUENCE = "pg_sequence"

class PgAddress(Address):
    """Адрес объекта каталога PostgreSQL: подключение плюс роли объекта внутри базы.

    Часть подключения — libpq URI, объект — query-параметры с ролью в имени
    в порядке объявления полей наследника:
    postgresql://host:port/database?schema=dm&table=fact_orders. Один
    наследник на строку списка 4.3; сборка и разбор строки по канону 2.2.1
    — здесь и только здесь, на urllib.parse.
    """

    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset({"scheme", "host", "port", "database"})

    scheme: Literal["postgresql"]
    host: str
    port: int
    database: str

    @classmethod
    def roles(cls) -> Sequence[str]:
        """Роли объекта — поля наследника после полей подключения, в порядке объявления, по alias."""
        names: list[str] = []
        for name, field in cls.model_fields.items():
            if name in cls.BASE_FIELDS:
                continue

            alias = field.alias
            if alias is None:
                alias = name

            names.append(alias)

        return names

    def render(self) -> str:
        query = urlencode(self.model_dump(by_alias=True, exclude=self.BASE_FIELDS), quote_via=quote)
        split = SplitResult(
            scheme=self.scheme,
            netloc=self._netloc(),
            path="/" + quote(self.database, safe=""),
            query=query,
            fragment="",
        )
        return urlunsplit(split)

    def _netloc(self) -> str:
        host = self.host
        if ":" in host:                      # IPv6 — в скобках, RFC 3986 §3.2.2
            host = f"[{host}]"

        return f"{host}:{self.port}"

    @classmethod
    def parse(cls, text: str) -> Self:
        """Строка → адрес этого класса; канон 2.2.1: без учётных данных, с портом, path = /database, роли по составу и порядку."""
        url = urlsplit(text)
        if url.scheme != "postgresql":
            raise AddressError(f"postgresql address {text!r}: expected scheme postgresql, got {url.scheme!r}")

        if url.username is not None:
            raise AddressError(f"postgresql address {text!r}: credentials are not part of an address")

        if url.fragment:
            raise AddressError(f"postgresql address {text!r}: fragment is not part of an address")

        host = url.hostname
        if host is None:
            raise AddressError(f"postgresql address {text!r}: host is required")

        try:
            port = url.port
        except ValueError as exc:
            raise AddressError(f"postgresql address {text!r}: port is not a number: {exc}") from exc

        if port is None:
            raise AddressError(f"postgresql address {text!r}: port is required")

        database = unquote(url.path.removeprefix("/"))
        if not database:
            raise AddressError(f"postgresql address {text!r}: path must be /<database>, got {url.path!r}")

        if "/" in database:
            raise AddressError(f"postgresql address {text!r}: path must be a single segment /<database>, got {url.path!r}")

        roles = parse_qsl(url.query, keep_blank_values=True)
        given: list[str] = []
        for name, _ in roles:
            given.append(name)

        expected = list(cls.roles())
        if given != expected:
            raise AddressError(f"{cls.__name__}: address {text!r} expects roles {expected}, got {given}")

        parts: dict[str, str | int] = {"scheme": url.scheme, "host": host, "port": port, "database": database}
        parts.update(roles)
        try:
            return cls.model_validate(parts)
        except ValidationError as exc:
            raise AddressError(f"{cls.__name__}: address {text!r} is not valid: {exc}") from exc

class PgDatabaseAddress(PgAddress): ...

class PgSchemaAddress(PgAddress):
    schema_name: str = Field(alias="schema")

class PgTableAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    table: str

class PgTableColumnAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    table: str
    column: str

class PgViewColumnAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    view: str
    column: str

class PgIndexAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    index: str

class PgFunctionAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    function: str
    args: str                                  # pg_get_function_identity_arguments; пустая строка обязательна

class PgConstraintAddress(PgAddress):
    schema_name: str = Field(alias="schema")
    table: str
    constraint: str

# … view, matview и его колонка, sequence, procedure, trigger — по строке списка 4.3 каждый

class PgAddresses:
    """Строка → адрес конкретного объекта PostgreSQL: класс выбирается по составу ролей в query."""

    MODELS: ClassVar[Sequence[type[PgAddress]]] = (
        PgDatabaseAddress, PgSchemaAddress, PgTableAddress, PgTableColumnAddress, PgViewColumnAddress,
        PgIndexAddress, PgFunctionAddress, PgConstraintAddress,
    )

    @classmethod
    def parse(cls, text: str) -> PgAddress:
        given: list[str] = []
        for name, _ in parse_qsl(urlsplit(text).query, keep_blank_values=True):
            given.append(name)

        for model in cls.MODELS:
            if list(model.roles()) == given:
                return model.parse(text)

        raise AddressError(f"postgresql address {text!r}: no object has roles {given}")

class PgTableNode(BaseModel):
    kind: Literal[PgNodeKind.TABLE]
    address: PgTableAddress

class PgColumnNode(BaseModel):
    kind: Literal[PgNodeKind.COLUMN]
    address: PgTableColumnAddress | PgViewColumnAddress | PgMatviewColumnAddress   # колонка чьей-то реляции; pydantic различит по ролям

PgNode = Annotated[PgDatabaseNode | PgSchemaNode | PgTableNode | PgColumnNode | ..., Field(discriminator="kind")]
```

```python
# boba-db-clickhouse: boba/db/clickhouse/nodes.py
class ChNodeKind(StrEnum):
    DATABASE = "ch_database"
    TABLE = "ch_table"
    VIEW = "ch_view"
    MATVIEW = "ch_matview"
    COLUMN = "ch_column"
    INDEX = "ch_index"          # skip-индекс, внутри таблицы
    PROJECTION = "ch_projection"
    DICTIONARY = "ch_dictionary"
    FUNCTION = "ch_function"

class ChAddress(Address):
    """Адрес объекта ClickHouse: подключение плюс роли объекта; схем нет, объекты сразу в базе.

    Грамматика та же, что у PgAddress, со своей схемой:
    clickhouse://host:port/database?table=events&column=ts. Один наследник
    на строку списка 4.3; сборка и разбор — здесь и только здесь.
    """

    BASE_FIELDS: ClassVar[frozenset[str]] = frozenset({"scheme", "host", "port", "database"})

    scheme: Literal["clickhouse"]
    host: str
    port: int
    database: str

    @classmethod
    def roles(cls) -> Sequence[str]:
        """Роли объекта — поля наследника после полей подключения, в порядке объявления, по alias."""
        names: list[str] = []
        for name, field in cls.model_fields.items():
            if name in cls.BASE_FIELDS:
                continue

            alias = field.alias
            if alias is None:
                alias = name

            names.append(alias)

        return names

    def render(self) -> str:
        query = urlencode(self.model_dump(by_alias=True, exclude=self.BASE_FIELDS), quote_via=quote)
        split = SplitResult(
            scheme=self.scheme,
            netloc=self._netloc(),
            path="/" + quote(self.database, safe=""),
            query=query,
            fragment="",
        )
        return urlunsplit(split)

    def _netloc(self) -> str:
        host = self.host
        if ":" in host:                      # IPv6 — в скобках, RFC 3986 §3.2.2
            host = f"[{host}]"

        return f"{host}:{self.port}"

    @classmethod
    def parse(cls, text: str) -> Self:
        """Строка → адрес этого класса; канон 2.2.1: без учётных данных, с портом, path = /database, роли по составу и порядку."""
        url = urlsplit(text)
        if url.scheme != "clickhouse":
            raise AddressError(f"clickhouse address {text!r}: expected scheme clickhouse, got {url.scheme!r}")

        if url.username is not None:
            raise AddressError(f"clickhouse address {text!r}: credentials are not part of an address")

        if url.fragment:
            raise AddressError(f"clickhouse address {text!r}: fragment is not part of an address")

        host = url.hostname
        if host is None:
            raise AddressError(f"clickhouse address {text!r}: host is required")

        try:
            port = url.port
        except ValueError as exc:
            raise AddressError(f"clickhouse address {text!r}: port is not a number: {exc}") from exc

        if port is None:
            raise AddressError(f"clickhouse address {text!r}: port is required")

        database = unquote(url.path.removeprefix("/"))
        if not database:
            raise AddressError(f"clickhouse address {text!r}: path must be /<database>, got {url.path!r}")

        if "/" in database:
            raise AddressError(f"clickhouse address {text!r}: path must be a single segment /<database>, got {url.path!r}")

        roles = parse_qsl(url.query, keep_blank_values=True)
        given: list[str] = []
        for name, _ in roles:
            given.append(name)

        expected = list(cls.roles())
        if given != expected:
            raise AddressError(f"{cls.__name__}: address {text!r} expects roles {expected}, got {given}")

        parts: dict[str, str | int] = {"scheme": url.scheme, "host": host, "port": port, "database": database}
        parts.update(roles)
        try:
            return cls.model_validate(parts)
        except ValidationError as exc:
            raise AddressError(f"{cls.__name__}: address {text!r} is not valid: {exc}") from exc

class ChDatabaseAddress(ChAddress): ...

class ChTableAddress(ChAddress):
    table: str

class ChTableColumnAddress(ChAddress):
    table: str
    column: str

class ChIndexAddress(ChAddress):               # skip-индекс уникален внутри таблицы — после table
    table: str
    index: str

class ChDictionaryAddress(ChAddress):
    dictionary: str

class ChFunctionAddress(ChAddress):            # перегрузок нет — args не нужен
    function: str

# … view, matview и их колонки, projection — по строке списка 4.3 каждый

class ChAddresses:
    """Строка → адрес конкретного объекта ClickHouse: класс по составу ролей, как PgAddresses."""

    MODELS: ClassVar[Sequence[type[ChAddress]]] = (
        ChDatabaseAddress, ChTableAddress, ChTableColumnAddress, ChIndexAddress, ChDictionaryAddress, ChFunctionAddress,
    )

    @classmethod
    def parse(cls, text: str) -> ChAddress:
        given: list[str] = []
        for name, _ in parse_qsl(urlsplit(text).query, keep_blank_values=True):
            given.append(name)

        for model in cls.MODELS:
            if list(model.roles()) == given:
                return model.parse(text)

        raise AddressError(f"clickhouse address {text!r}: no object has roles {given}")

class ChColumnNode(BaseModel):
    kind: Literal[ChNodeKind.COLUMN]
    address: ChTableColumnAddress | ChViewColumnAddress | ChMatviewColumnAddress

ChNode = Annotated[ChDatabaseNode | ChTableNode | ChColumnNode | ..., Field(discriminator="kind")]
```

### 4.3 Адреса объектов

Примеры адресов всех объектов — 2.2.3; здесь правила состава и порядка ролей.

Функции и процедуры уникальны сигнатурой, а не именем: `args` — типы
аргументов в форме `pg_get_function_identity_arguments` (имена типов
каноничны, `integer`, а не `int4`), через запятую, запятая кодируется.
Роль `args` у функций и процедур обязательна даже пустой, чтобы адрес
без аргументов не спутать с адресом, где аргументы забыли. Индексы и
последовательности в PostgreSQL уникальны в схеме и от таблицы не зависят,
поэтому `table` в их адрес не входит — принадлежность таблице выражает
ребро `contains`; ограничения и триггеры уникальны внутри таблицы, поэтому идут
после `table`.

Набор ClickHouse. Схем нет: база сразу содержит объекты; представления и
материализованные представления — отдельные роли, хотя в
`system.tables` они лежат рядом с таблицами и различаются полем `engine`.

У ClickHouse skip-индекс и проекция принадлежат таблице и уникальны только
внутри неё, поэтому идут после `table` — в отличие от PostgreSQL, где
индекс уникален в схеме. Это ровно та разница между движками, ради которой
роли объявляет корпус, а не ядро: порядок и состав ролей — часть
`Introspector` движка (раздел 4.4).

### 4.4 Content tables

Виды содержимого корпуса и порт чтения каталога движка; виды узлов — в
пакетах движков (4.2):

```python
# виды узлов — перечисления пакетов движков (4.2): PgNodeKind в boba-db-postgres,
# ChNodeKind в boba-db-clickhouse; MssqlNodeKind, OracleNodeKind, MysqlNodeKind придут
# со своими пакетами boba-db-*; у каждого свой набор объектов:
#   pg:     database, schema, table, view, matview, column, index, constraint, function, procedure, trigger, sequence
#   ch:     database, table, view, matview, column, index (skip), projection, dictionary, function
#   mssql:  database, schema, table, view, column, index, constraint, procedure, function, trigger, sequence
#   oracle: schema, table, view, matview, column, index, constraint, function, procedure, trigger, sequence, partition
#   mysql:  database, table, view, column, index, constraint, procedure, function, trigger

class WarehouseContentKind(StrEnum):
    TITLE = "title"
    COMMENT = "comment"
    DDL = "ddl"
    COLUMNS = "columns"
    PROFILE = "profile"
    SAMPLE = "sample"
    SUMMARY = "summary"

class Introspector(Protocol):
    """Порт чтения системного каталога одного движка: объекты и их описания для узлов и content tables хранилища.

    Реализация на движок (pg_catalog, system.tables, sys.*), потому что у
    движков разные наборы объектов и разные слова для одного и того же.
    """
    def objects(self, database: str) -> AsyncIterator[WarehouseObject]: ...
    def profile(self, table: WarehouseObject, sample: int) -> ColumnProfiles: ...
```

#### 4.4.1 `relations`

```sql
create table relations (               -- table | view | materialized_view | dictionary
    node_id       bigint      primary key references nodes on delete cascade,
    engine        text        not null,   -- postgresql | mssql | clickhouse | oracle | mysql
    relation_kind text        not null,   -- = nodes.kind: pg_table | pg_view | ch_table | …
    title         text        not null,   -- имя объекта без схемы
    definition    text        not null default '',   -- create table … / select … представления
    comment       text        not null default '',
    row_estimate  bigint,
    size_bytes    bigint,
    owner         text        not null default '',
    modified_at   timestamptz,
    properties    jsonb       not null default '{}',
        -- clickhouse: {"engine": "MergeTree", "order_by": ["ts","user_id"], "partition_by": "toYYYYMM(ts)"}
        -- postgresql: {"tablespace": "fast", "partitioned": true, "partition_key": "created_at"}
    title_tsv     tsvector    generated always as (to_tsvector('simple', title)) stored,
    comment_tsv   tsvector    generated always as (...comment...) stored
);
create index on relations (lower(title));                     -- PgExactLookup
create index on relations using gist (title gist_trgm_ops);    -- PgTrigramLookup
create index on relations using gin (title_tsv);
create index on relations using gin (comment_tsv);
```

#### 4.4.2 Тексты отношения: `relation_ddl`, `relation_column_lists`, `relation_profiles`, `relation_samples`, `relation_summaries`

Что модель читает вместо базы: по таблице на вид текста, у каждой своя
структура, свой GIN по `tsv` и своя таблица векторов на модель.

```sql
create table relation_ddl (
    node_id       bigint      primary key references nodes on delete cascade,
    ddl           text        not null,   -- create table … / create view … as …, нормализованный вывод движка
    ddl_sha256    text        not null,
    tsv           tsvector    generated always as (...) stored
);
create table relation_column_lists (   -- колонки одной строкой: имя, тип, комментарий — для поиска по составу
    node_id       bigint      primary key references nodes on delete cascade,
    content       text        not null,
    tsv           tsvector    generated always as (...) stored
);
create table relation_profiles (       -- профиль таблицы текстом, собранный из column_profiles
    node_id       bigint      primary key references nodes on delete cascade,
    content       text        not null,
    sampled_at    timestamptz not null,
    tsv           tsvector    generated always as (...) stored
);
create table relation_samples (        -- несколько строк таблицы в раскладке столбцов
    node_id       bigint      primary key references nodes on delete cascade,
    content       text        not null,
    rows_shown    int         not null,
    sampled_at    timestamptz not null,
    tsv           tsvector    generated always as (...) stored
);
-- у каждой GIN по tsv и таблица векторов на модель: relation_ddl_vectors__e5,
-- relation_column_list_vectors__e5, relation_profile_vectors__e5, relation_sample_vectors__e5 —
-- (node_id primary key, embedding vector(1024)) с HNSW
create table relation_summaries (     -- как page_summaries
    node_id bigint primary key references nodes on delete cascade,
    summary text not null, topics text[] not null default '{}',
    model text not null, system_prompt_hash text not null,
    tsv tsvector generated always as (...) stored, updated_at timestamptz not null default now()
);
create table relation_summary_vectors__e5 (
    node_id bigint primary key references relation_summaries on delete cascade,
    embedding vector(1024) not null
);
create index on relation_summary_vectors__e5 using hnsw (embedding vector_cosine_ops);
```

Четыре вида содержимого отношения — четыре таблицы, а не одна с колонкой
вида: у каждой своя структура (у профиля — время выборки, у примера —
число строк), свой вес в RRF и свои индексы без фильтров.

#### 4.4.3 `columns` и `column_profiles`

```sql
create table columns (
    node_id       bigint      primary key references nodes on delete cascade,
    relation_id   bigint      not null references nodes on delete cascade,
    position      int         not null,
    title         text        not null,   -- имя колонки
    native_type   text        not null,   -- как в движке: Nullable(DateTime64(3)), NUMBER(18,2), timestamptz
    canonical_type text       not null,   -- integer | decimal | text | timestamp | date | bool | binary | json | array | other
    nullable      bool        not null,
    default_expr  text        not null default '',
    comment       text        not null default '',
    properties    jsonb       not null default '{}',
    comment_tsv   tsvector    generated always as (...comment...) stored
);
create index on columns (lower(title));
create index on columns using gin (comment_tsv);
create index on columns using gist (title gist_trgm_ops);      -- same_column, name_pattern: похожие имена колонок
create table column_comment_vectors__e5 (
    node_id bigint primary key references columns on delete cascade,
    embedding vector(1024) not null
);
create index on column_comment_vectors__e5 using hnsw (embedding vector_cosine_ops);
create table column_profiles (         -- профиль данных: выборкой, не полным сканом
    node_id       bigint      primary key references nodes on delete cascade,   -- узел колонки
    sampled_at    timestamptz not null,
    sample_rows   bigint      not null,
    null_frac     real        not null,
    distinct_est  bigint      not null,
    min_value     text        not null default '',
    max_value     text        not null default '',
    top_values    jsonb       not null default '[]',   -- [{"v": "PAID", "share": 0.71}, …]
    value_pattern text        not null default '',    -- e-mail | uuid | phone | date-as-text | code:^[A-Z]{2}\d{6}$
    values_minhash bytea                               -- minhash-скетч значений: пересечение с другой колонкой без соединения таблиц
);
```

Профиль — то, что делает хаос описуемым: по нему модель понимает, что в
колонке `status` три значения, а `ext_ref` — на самом деле e-mail; по
`values_minhash` корпус оценивает пересечение значений двух колонок без
соединения таблиц и ставит ребро `inferred_key`.

#### 4.4.4 `constraints`, `indexes`, `routines`

Объекты, которые сами не ищутся, но дают явные рёбра: внешний ключ —
`foreign_key`, тело процедуры — `routine_uses`.

```sql
create table constraints (
    node_id         bigint    primary key references nodes on delete cascade,
    relation_id     bigint    not null references nodes on delete cascade,
    constraint_kind text      not null,   -- primary_key | foreign_key | unique | check
    columns         text[]    not null,
    ref_relation_id bigint    references nodes on delete set null,
    ref_columns     text[]    not null default '{}',
    definition      text      not null default ''
);
create table indexes (
    node_id       bigint      primary key references nodes on delete cascade,
    relation_id   bigint      not null references nodes on delete cascade,
    columns       text[]      not null,
    is_unique     bool        not null,
    index_kind    text        not null default '',   -- btree | gin | minmax | bloom_filter | …
    definition    text        not null default ''
);
create table routines (
    node_id       bigint      primary key references nodes on delete cascade,
    routine_kind  text        not null,   -- function | procedure
    title         text        not null,   -- имя с сигнатурой: calc_total(bigint, numeric)
    args          text        not null default '',
    returns       text        not null default '',
    language      text        not null default '',
    definition    text        not null default '',
    reads         text[]      not null default '{}',   -- отношения из тела, разбор по движку
    writes        text[]      not null default '{}'
);
```

Что откуда: явные рёбра — `contains` из каталога, `foreign_key` из
`constraints`, `view_source` и `routine_uses` из разбора определений;
косвенные — `same_column` и `name_pattern` из имён, `inferred_key` из
профилей, `co_queried` из журнала запросов движка (`pg_stat_statements`,
`system.query_log`) — отдельным читателем, если журнал доступен.

### 4.5 Способы поиска

Индексы Warehouse, объявляются корпусом как у Confluence (3.4):

| вид содержимого | таблица, ключ строки, колонка | индексы Postgres |
|---|---|---|
| `title` | `relations`, `node_id`, `title` | `fts(title_tsv)`, `trigram`, `exact` — `exact` назначен роли `naming` |
| `title` | `columns`, `node_id`, `title` | `trigram`, `exact` — для `same_column`, `name_pattern` |
| `comment` | `relations`, `node_id`, `comment` | `fts(comment_tsv)` |
| `comment` | `columns`, `relation_id`, `comment` | `fts(comment_tsv)`, `vector(column_comment_vectors.node_id)` — узел выдачи: таблица, колонка идёт фрагментом |
| `ddl` | `relation_ddl`, `node_id`, `ddl` | `fts(tsv)`, `vector(relation_ddl_vectors__e5.node_id)` |
| `columns` | `relation_column_lists`, `node_id`, `content` | `fts(tsv)`, `vector(relation_column_list_vectors__e5.node_id)` |
| `profile` | `relation_profiles`, `node_id`, `content` | `fts(tsv)`, `vector(relation_profile_vectors__e5.node_id)` |
| `sample` | `relation_samples`, `node_id`, `content` | `fts(tsv)`, `vector(relation_sample_vectors__e5.node_id)` |
| `summary` | `relation_summaries`, `node_id`, `summary` | `fts(tsv)`, `vector(relation_summary_vectors__e5.node_id)` — `vector` назначен роли `similarity` |

### 4.6 Рёбра

```python
class WarehouseEdgeKind(StrEnum):
    CONTAINS = "contains"              # база → схема → таблица; таблица → колонка, индекс, ограничение, триггер
    FOREIGN_KEY = "foreign_key"        # объявленный внешний ключ; редок, но надёжен
    VIEW_SOURCE = "view_source"        # представление читает таблицу — из его определения
    ROUTINE_USES = "routine_uses"      # процедура читает или пишет таблицу
    INFERRED_KEY = "inferred_key"      # значения колонки A содержатся в значениях колонки B: кандидат в ключ
    SAME_COLUMN = "same_column"        # колонка с тем же именем и типом в двух таблицах
    NAME_PATTERN = "name_pattern"      # общий префикс или суффикс имён: fact_*, *_hist, stg_orders/dm_orders
    CO_QUERIED = "co_queried"          # таблицы вместе в одних запросах: pg_stat_statements, system.query_log
    MENTION = "mention"                # имя таблицы в комментарии другой
    ENTITY = "entity"                  # отношение → сущность, которую упоминает (считает ядро)
    SIMILAR = "similar"                # близость векторов                     (считает ядро)
```

Для Warehouse именно косвенные виды — `inferred_key`, `same_column`,
`name_pattern`, `co_queried` — описывают хаос, где внешних ключей нет:
`fact_orders.customer_id ⊆ dim_customer.customer_id` при 99,8% покрытия
значений — почти наверняка ключ, хоть он и не объявлен.

Как считаются, следующим планом:

- `inferred_key` — включение значений колонки
  A в значения B на выборке, в обосновании покрытие и размер выборки;
  `same_column` — совпадение имени и типа; `name_pattern` — общий префикс
  или суффикс; `co_queried` — число совместных запросов за окно из
  `pg_stat_statements` или `system.query_log`; `view_source`,
  `routine_uses` — разбор определения, в обосновании имя объекта.

### 4.7 Документ узла

Отношение — DDL, комментарии, профиль и пример строк; колонка — профиль
и ссылка на таблицу (2.8).

## 5. Слой Entity

### 5.1 Обзор

`Entity` — именованная вещь, о которой говорят тексты: продукт, технология,
организация, версия, термин предметной области, метка. `KRaft`, `ClickHouse`,
`Gazprom-Neft`, «качество данных». Она не лежит ни в каком источнике как
объект: её нельзя скачать, у неё нет страницы и нет таблицы, она
производная от текста. Но с ней делают всё то же, что с `Node`: ищут по
имени, показывают в выдаче, ходят от неё к тому, что о ней написано, и
считают её место в графе. Поэтому `Entity` — это `Node` с видом `entity`,
и слой Entity устроен так же, как слой страниц или слой таблиц: свой
вид узла, свой адрес, своя content table, свои способы поиска, свой вид
рёбер. Разница в одном: его никто не скачивает — он появляется при
индексации других слоёв и живёт во времени. Объявляет его ядро, потому
что он одинаков для любого корпуса.

### 5.2 Адрес

**Адрес чистый, без источника.** ClickHouse один и тот же, где бы о нём
ни писали, поэтому адрес — `entity://clickhouse`: схема `entity` и
нормализованное имя, без корпуса и без вида (`EntityAddress`):
Вид сущности — продукт, технология, термин — атрибут, а не часть
идентичности: NER может назвать `kraft` продуктом на одной странице и
технологией на другой, а сущность одна, и вид у неё — преобладающий.
Каждая схема корпуса держит свои узлы-сущности, потому что нумерация
узлов на схему; одинаковый адрес `entity://kraft` в `confluence` и в
`warehouse` — это и есть мост между корпусами, когда он понадобится.

### 5.3 Таблица `entities` и извлечение

**Откуда берутся.** Стадия `entities` конвейера (раздел 7) берёт у
корпуса тексты узла (`Corpus.entity_texts`) и извлекает из них имена
четырьмя способами, все проверены на пробе (5.6):

- NER моделью GLiNER: ей даётся текст и список типов (`software product`,
  `technology`, `version`, `organization`), она размечает отрезки этих
  типов. Модель не знает списка продуктов заранее и узнаёт их по
  контексту — так находятся и `KRaft`, и `Arenadata QuickMarts`.
- Ключевые фразы YAKE: статистика по тексту без модели, даёт термины
  предметной области вроде «качество данных», вид `term`.
- Метки страницы Confluence как есть, вид `label`.
- В Warehouse — токены имён колонок и таблиц: `customer_id` и
  `customer_region` дают `customer`, вид `field`.

Найденное приводится к одной форме — нижний регистр, один пробел, без
диакритики, — и это имя становится адресом. Узел-сущность создаётся
upsert'ом по адресу, как любой `Node`; его содержимое — одна строка
content table слоя Entity (DDL, как и весь слой, у ядра):

```sql
create table entities (
    node_id       bigint      primary key references nodes on delete cascade,
    name          text        not null unique,   -- нормализованная форма, она же в адресе
    display       text        not null,          -- форма, в которой встретилась первой
    type          text        not null,          -- преобладающий тип: product | technology | organization | version | term | label | field
    tsv           tsvector    generated always as (to_tsvector('simple', name)) stored
);
create index on entities using gist (name gist_trgm_ops);   -- entity/trigram
```

### 5.4 Ребро `entity`

**Связь узла с сущностью — ребро `entity`** от `Node` к `Entity` в
`edges`, как любая другая связь: вес — tf-idf, обоснование — сколько раз
встретилась и каким типом её назвали. Отдельной таблицы привязок нет.

- `entity` — ядро, стадия `entities`: от узла к каждой сущности, которую
  извлёк из его текстов `EntityExtractor`; вес tf-idf (5.4), `idf` —
  глобальной стадией.

```
tf(n, e)  = count(n, e) / Σ count(n, ·)
idf(e)    = ln((N + 1) / (df(e) + 1)) + 1        N — узлов с сущностями, df — узлов, упоминающих e
weight    = tf · idf / max по узлу n            в [0, 1], 1 у самой характерной сущности узла
```

`count` считает стадия для одного узла; `idf` зависит от всего корпуса и
пересчитывается глобальной стадией `kb_graph_rebuild`, которая обновляет
веса всех рёбер `entity`. Сущность на половине корпуса (`cassandra` в
пространстве Cassandra) получает малый `idf` и не связывает всё со всем;
сущность на 2–5 узлах связывает их сильно.

Рёбра `entity` страницы FLIP-457 (узел 17) и таблицы `dm.fact_orders` (41):

| source_id | target_id | kind | weight | evidence | почему такой вес |
|---|---|---|---|---|---|
| 17 | 60 | `entity` | 0.61 | `{"count": 4, "type": "technology"}` | страница FLIP-457 упоминает Kubernetes 4 раза |
| 17 | 62 | `entity` | 0.20 | `{"count": 1, "type": "label"}` | метка `accepted` — на 40% страниц пространства, `idf` мал |
| 41 | 63 | `entity` | 0.83 | `{"count": 2, "type": "field"}` | колонки `customer_id`, `customer_region` |
| 41 | 64 | `entity` | 0.95 | `{"count": 3, "type": "term"}` | OMS в комментариях таблицы |

**Что это даёт.** Две страницы, обе упоминающие `kraft`, связаны путём в
два шага через узел `entity://kraft`, и расширение по графу (8.2) находит
этот путь само; вычислять и хранить отдельное ребро «общие сущности»
между страницами не нужно. Через частую сущность активация растекается
слабо, потому что вес каждого её ребра мал. На запрос «kraft» способы
`entity/exact` и `entity/trigram` находят сам узел-сущность, и в выдаче
он стоит первым, а страницы о нём приходят как его соседи; `kb_node` по
адресу `entity://kraft` показывает, где она встречается и с каким весом.
Глобальные метрики (2.5) считаются и для сущностей: PageRank сущности —
насколько термин центральный для корпуса.

**Жизнь во времени.** У сущности нет области обхода и версии в
источнике: она возникает, когда её впервые упомянул какой-то `Node`, и
дальше укрепляется или слабеет вместе с корпусом. Каждая новая страница о
`KRaft` добавляет ей входящее ребро — растут `degree_in` и PageRank, она
становится центральнее; но каждое ребро при этом чуть слабее, потому что
`idf` падает: сущность, о которой пишут все, перестаёт отличать одну
страницу от другой. Страницу удалили — её ребро ушло вместе с ней. Стадия
`entities` отмечает `last_seen_run` у каждой встреченной сущности, а
глобальная стадия пересчитывает `idf` и удаляет узлы-сущности, у которых
не осталось входящих рёбер `entity`, — вместе с ними уходит и строка
`entities`.

### 5.5 Способы поиска

Слой Entity ищется теми же классами (2.9.1), что и остальные, но
объявляет их не корпус, а ядро: таблица `entities` и её колонки
одинаковы в любой схеме. Реализация живёт в `boba-db-pggraph`; корпус
включает её в свой список одной строкой и назначает вес подписям
`entity/exact` и `entity/trigram` в `[search.weights]`:

```python
class PgEntityLookups:
    """Способы поиска по слою сущностей: точное имя и триграммы над entities; одинаковы для любого корпуса."""

    CONTENT: ClassVar[str] = "entity"          # вид содержимого слоя сущностей: подписи entity/exact, entity/trigram

    @classmethod
    def of(cls, schema: str) -> Sequence[IndexLookup[PgStatement]]:
        return (
            PgExactLookup(content=cls.CONTENT, schema=schema, table="entities",
                          node_column="node_id", row_column="node_id", text_column="name"),
            PgTrigramLookup(content=cls.CONTENT, schema=schema, table="entities",
                            node_column="node_id", row_column="node_id", text_column="display"),
        )
```

### 5.6 Проба извлечения

GLiNER `multi-v2.1` в песочнице плагина вместе с torch CPU: по пробе на
50 страницах cwiki — 4 с на страницу в 13,6 тыс. символов на 8 потоках,
продукты и технологии извлекаются надёжно, мусор предсказуем и режется
стоп-листом и tf-idf. Метки — из конфига, объединение по имени без учёта
типа, тип — преобладающий. Термины — YAKE, вид `term`.

Русская проба — 17 страниц внутреннего Confluence (PHDD2, TMETA, PIXBI,
DQ; 77 тыс. символов): 1,5 с на страницу, плотность сущностей та же, что
на английском (9,7 на 10 тыс. символов против 8,3). Продукты и организации
извлекаются: `PIX BI`, `ADQM`, `Arenadata QuickMarts`, `PostgresPro`,
`Oracle`, `Airflow`, `Gazprom-Neft`, `EDM`; предметные термины тоже —
«продуктивный ландшафт», «качество данных», «lineage». Мусор того же
рода, что в английском, плюс склонённые формы («версию», «следующих
версиях»), которые режутся тем же стоп-листом и порогом на вид `version`.
YAKE на русском без лемматизации слаб («Рисунок», «Вкладка», «данных»):
термины берутся только из двух и более слов и с меньшим весом, замена на
извлечение ключевых фраз через эмбеддер e5 — отдельная проба позже.

## 6. Код

Новых пакетов три — ядро, его реализация на Postgres и инструменты над
графом; всё, что относится к источнику, живёт в существующих пакетах
этого источника:

| пакет | что внутри |
|---|---|
| `packages/core/boba-graph` | новый. Домен: `Node`, `Edge`, `Address`, `EntityAddress`, `Evidence`, `Probe`, `IndexLookup[S]`, `LookupMethod`, `VectorEncoder[V]`, `Corpus`; порты хранения и сервисов стадий; конвейер 2.0 |
| `packages/infra/db/boba-db-pggraph` | новый. Postgres: DDL graph tables, реализации портов для relational и age, слияние поиска по способам, обход, глобальный экспорт; пул и курсоры — из `boba-db-postgres` |
| `packages/tools/boba-tool-graph` | новый. Инструменты над любым корпусом: `kb_search`, `kb_related`, `kb_node`, `kb_graph_rebuild`, `kb_graph_check`, установка схемы |
| `packages/infra/format/boba-confluence` | модели узлов и адресов (3.2) — рядом с `models.py`; подмодуль `boba.confluence.graph` за extra `graph`: `ConfluenceContentKind`, content tables и их DDL, `ConfluenceCorpus`, ридер 2.0, явные рёбра, документ узла (раздел 3) |
| `packages/tools/boba-tool-confluence` | инструменты индексации 2.0 `confluence_graph_index_*` рядом с индексатором 1.0; манифест плагина регистрирует корпус в реестре |
| `packages/infra/db/boba-db-postgres` | `PgNodeKind`, адреса и узлы объектов каталога, `PgNode` (4.2); интроспекция PostgreSQL для Warehouse — там, где уже лежит `catalog.py` (следующий план) |
| `packages/infra/db/boba-db-clickhouse` | `ChNodeKind`, адреса и узлы объектов, `ChNode` (4.2); интроспекция ClickHouse (следующий план) |
| `packages/core/boba-catalog` и `boba-tool-postgres`, `boba-tool-clickhouse` | слой Warehouse (следующий план): content tables 4.4 сверяются с моделью отношений каталога и сводятся к ней, где совпадают; инструменты индексации — в инструментах движков |

Extra `graph` у `boba-confluence` — тот же механизм `[tool.boba.extras]`,
что у `boba-krb`: подмодуль объявляет свои зависимости (`boba-graph`,
`boba-db-pggraph`), остальной пакет их не получает, и `DepsAudit` это
проверяет. Где именно окажется `WarehouseCorpus`, решает следующий план:
модель отношений уже в `boba-catalog` (core), интроспекторы в `boba-db-*`,
а самому корпусу нужен `boba-db-pggraph`, значит его место в сервисе
каталога или в инструменте, но не в новом пакете.

Корпус попадает в реестр корпусов по имени схемы через манифест плагина
`boba.tools` своего источника (`boba-tool-confluence`); `boba-tool-graph`
получает реализацию `Corpus` из реестра и никогда не импортирует пакеты
источников напрямую. Добавление корпуса Warehouse не меняет ни ядро, ни
`boba-db-pggraph`, ни `boba-tool-graph`.

Порты ядра и кто их реализует — в обзоре ядра (2.1).

`boba-graph` зависит от `boba-indexing` только ради `Reader`, `Section`,
`Chunker`, `Embedder`, `RawDocument`. Схема создаётся установкой:
`boba-db-pggraph` держит DDL graph tables одним файлом на
`create … if not exists`, корпус — свой файл content tables; установка получает
имя схемы и бэкенд графа, накатывает graph tables, затем content tables, для AGE
создаёт граф `<схема>_graph`. Ядро `boba-graph` ни одного из этих файлов
не содержит и не импортирует.
Миграций нет: до первого релиза схема правится пересозданием, стендовые
`*_test` создаются тем же кодом.

Конфиг корпуса Confluence:

```toml
[storage]
    pg_schema = "confluence"

[graph]
    backend             = "relational"
    similar_top_k       = 10
    similar_min_cos     = 0.80
    mention_min_words   = 2
    metrics             = ["pagerank", "betweenness", "degree_in", "community"]

[search]
    candidates        = 50
    seed_k            = 20
    snippets_per_node = 3
    rrf_k             = 60
    [search.metrics]
        pagerank  = 0.5
        degree_in = 0.1
    [search.expand]
        depth  = 2
        weight = 0.5
        [search.expand.factors]
            link           = 1.0
            attachment_ref = 0.9
            mention        = 0.8
            similar        = 0.7
            entity         = 0.7
            series         = 0.4
            same_author    = 0.3
    [search.weights]
        "title/exact"      = 3.0
        "title/fts"        = 2.0
        "title/trigram"    = 1.5
        "summary/vector"   = 1.5
        "summary/fts"      = 1.2
        "section/vector"   = 1.0
        "section/fts"      = 1.0
        "outline/fts"      = 1.0
        "attachment_text/fts"    = 0.8
        "attachment_text/vector" = 0.8
        "caption/fts"      = 0.6
        "caption/vector"   = 0.6
        "image/vector"     = 0.5
        "entity/exact"     = 2.5
        "entity/trigram"   = 1.0

[entities]
    kind      = "gliner"
    model_dir = "${env.models}/gliner-multi"
    labels    = ["software product", "technology", "version", "organization"]
    threshold = 0.5
    stopwords = ["apache", "application", "version", "it"]
    terms_top = 20

[summary]
    input_chars = 12000
    [summary.generation]
        kind          = "local"
        model_dir     = "${env.models}/onnx-genai/qwen3-4b-int4"
        max_tokens    = 400
        reply_prefix  = "<think>\n\n</think>\n\n"
        system_prompt = "..."
```

## 7. Индексация

Стадии на узел. Ядро задаёт каркас — учёт, сущности, рёбра ядра,
глобальную стадию — и даёт сервисы; что писать в content tables, решает корпус:

| стадия | кто делает | что пишет |
|---|---|---|
| `fetch` | `boba-tool-confluence`: `ConfluenceHttpTransport` индексатора 1.0, без правок | Confluence: как сейчас |
| `parse` | `boba-confluence`, подмодуль `graph`: ридер 2.0 (раздел 6) | страницы, разделы, таблицы, ссылки / объекты, колонки, определения |
| `embed` | `ConfluenceCorpus` зовёт `VectorEncoder` из `boba-llm` — те же модели, что у его векторных способов поиска | `page_section_vectors`, `page_summary_vectors` / `relation_ddl_vectors`, … |
| `summary` | `ConfluenceCorpus` зовёт `Generator` из `boba-llm`, если запрошено | `page_summaries` / `relation_summaries` |
| `entities` | конвейер `boba-graph`: `GlinerExtractor` по `Corpus.entity_texts` | узлы `entity` в `nodes` и `entities`, рёбра `entity` от узла к ним — через `PgEntityStore` |
| `edges` | конвейер `boba-graph`: `Corpus.explicit_edges` + `similar` по `Corpus.similar_text` | `PgGraphStore` или `AgeGraphStore` |

Стадии `entities` и `edges` инкрементальны: рёбра индексируемого узла
удаляются в обе стороны и строятся заново. `entity` — от узла к
сущностям, которые экстрактор нашёл в его текстах, с upsert'ом самих
узлов-сущностей по адресу; `similar` — способом роли `similarity`: текст
узла кодируется и ищется kNN по векторной таблице через HNSW с порогом.

Глобальная стадия — инструмент `kb_graph_rebuild(corpus)`: пересчёт `idf`
и весов рёбер `entity`, удаление сущностей без упоминаний, экспорт рёбер
в NetworkX, метрики из конфига → `ranks`.
На корпусе в 1,4 тыс. узлов — секунды; NetworkX держит десятки тысяч узлов
и миллионы рёбер в памяти.

## 8. Поиск и ранжирование

Инструмент `kb_search(corpus, query, top_k, expand)`. Вход — текст
пользователя; выход — до `top_k` строк `Match` (2.6): узел с `kind` и
адресом, заголовок, счёт, фрагменты, которыми он найден, и путь по графу,
если пришёл через граф. Алгоритм — три запроса к базе и одно сложение в
ядре; ни строки способов, ни фрагменты, которые не попадут в выдачу, до
Python не доходят.

| шаг | кто | что делает | запросов |
|---|---|---|---|
| 1 кандидаты | `SearchStore.candidates` | все способы корпуса одним SQL, RRF, фрагменты, `kind`, адрес, метрики | 1 |
| 2 расширение | `GraphStore.expand` | от опорных узлов по ссылочным и семантическим рёбрам; только если `expand` | 0 или 1 |
| 3 счёт | `NodeSearch` в ядре | сложить `s_base`, `s_graph` и метрики, отрезать `top_k` | 0 |
| 4 заголовки | `Corpus.titles` | заголовки итоговых узлов из content tables | 1 |

### 8.1 Кандидаты: один запрос на все способы

Каждый способ (`IndexLookup`) даёт подзапрос с колонками `node_id`,
`row_id`, `snippet`, `score`, не длиннее `candidates` строк. Хранилище
вкладывает их ветками в один запрос, и дальше всё считает база. Пример с
двумя ветками из двенадцати:

```sql
with
    hits as (                                                  -- по ветке на способ корпуса
        select
            'title/exact' as lookup, 'title' as content_kind, 3.0::real as weight,
            q.node_id, q.row_id, q.snippet,
            row_number() over (order by q.score desc) as rank
        from (
            select
                t.node_id as node_id,
                t.node_id as row_id,
                t.title as snippet,
                1.0 as score
            from
                confluence.pages t
            where
                lower(t.title) = lower(%(text)s)
            limit %(limit)s
        ) q
        union all
        select
            'section/vector', 'section', 1.0::real,
            q.node_id, q.row_id, q.snippet,
            row_number() over (order by q.score desc)
        from (
            select
                t.node_id,
                t.id,
                t.format_content,
                1 - (v.embedding <=> '[…]'::vector)                -- вектор запроса литералом от энкодера способа
            from
                confluence.page_section_vectors__e5 v
                join confluence.page_sections t on
                    t.id = v.section_id
            order by
                v.embedding <=> '[…]'::vector
            limit %(limit)s
        ) q
    ),
    seeds as (                                                 -- слияние RRF: сумма weight / (k + rank) по веткам
        select
            node_id,
            sum(weight / (%(rrf_k)s + rank)) as s_base
        from
            hits
        group by
            node_id
        order by
            s_base desc
        limit %(seed_k)s
    ),
    picked as (                                                -- фрагменты опорных узлов по вкладу в счёт
        select
            h.*,
            row_number() over (
                partition by h.node_id
                order by h.weight / (%(rrf_k)s + h.rank) desc
            ) as n
        from
            hits h
            join seeds s on
                s.node_id = h.node_id
    ),
    snippets as (
        select
            node_id,
            json_agg(
                json_build_object('lookup', lookup, 'content_kind', content_kind, 'row_id', row_id, 'text', snippet)
                order by n
            ) as items
        from
            picked
        where
            n <= %(per_node)s
        group by
            node_id
    ),
    metrics as (                                               -- глобальные метрики из ranks, только названные в [search.metrics]
        select
            node_id,
            json_object_agg(metric, value) as values
        from
            confluence.ranks
        where
            node_id in (select node_id from seeds)
            and metric = any(%(metrics)s)
        group by
            node_id
    )
select
    s.node_id,
    n.kind,
    n.address,
    s.s_base,
    sn.items as snippets,
    coalesce(m.values, '{}'::json) as metrics
from
    seeds s
    join confluence.nodes n on
        n.id = s.node_id
    join snippets sn on
        sn.node_id = s.node_id
    left join metrics m on
        m.node_id = s.node_id
order by
    s.s_base desc
```

**Слияние по обратному рангу.** Узел, стоящий в списке способа на месте
`rank`, получает от этого списка `weight / (k + rank)`, `k = 60`;
слагаемые по всем спискам складываются. Узел на первом месте в двух
списках с весами 1.0 набирает `2/61`; узел на первом месте только по
заголовку с весом 3.0 — `3/61`; на десятом месте по разделу с весом 1.0 —
`1/70`. Складываются места, а не счета, потому что `ts_rank`, косинус и
`similarity()` несопоставимы, а место в списке сопоставимо; поэтому
нормировать их не нужно. Вес — из `[search.weights]` по подписи способа
«содержимое/способ»: точное совпадение заголовка значит больше, чем
совпадение по абзацу раздела. Константа 60 сглаживает разницу между
первым и вторым местом, чтобы одно попадание на первом месте не
перебивало три попадания на пятых.

**Фрагменты.** У узла остаются `snippets_per_node` фрагментов с
наибольшим вкладом в его счёт, из разных способов: раздел, найденный
полнотекстом, и саммари, найденное вектором, — два разных ракурса.
Фрагмент несёт подпись способа и вид содержимого: по ним рендер отличает
текст от картинки, а `row_id` позволяет взять оригинал куска.

**Единица выдачи.** На каком уровне узел попадает в выдачу, решает
способ полем `node_column`, а не обход графа. У `page_sections` там
страница, и раздел приходит фрагментом страницы. У `columns` для
комментария там `relation_id`: запрос «отгрузка нефтепродуктов» находит
комментарии колонок `shipment_volume` и `product_type`, а в выдаче
оказывается таблица `dm.fact_shipments` с двумя фрагментами-колонками.
Подниматься по `contains` не нужно, и в выдаче нет ни колонок, ни схемы,
ни базы. Колонка остаётся узлом для графа и lineage; кому нужны колонки
таблицы, тот берёт `kb_node`.

### 8.2 Расширение: узлы, которых текст не нашёл

Текстовый поиск находит узлы, где встретились слова запроса. Нужный
ответ часто лежит в соседнем узле, где этих слов нет: страница, на
которую ссылаются три найденные; таблица, чей ключ покрывает найденную.
Расширение переходит от опорных узлов по рёбрам и поднимает то, куда
сходятся связи от нескольких найденных.

**По каким рёбрам.** Только по перечисленным в `[search.expand.factors]`
— ссылочным (`link`, `mention`, `attachment_ref`, `foreign_key`,
`view_source`, `routine_uses`) и семантическим (`similar`, `entity`,
`same_column`, `co_queried`, `series`). Ребро `entity` ведёт к
узлу-сущности, поэтому две страницы об одном продукте соединяются за два
шага через него — это и есть «общие сущности», без отдельного ребра. Структурных (`contains`,
`child_page`, `in_space`, `has_attachment`) в списке нет, и это не
пропуск: через контейнер схема `dm` связана с каждой своей таблицей, и по
`contains` активация от любой найденной таблицы дотекла бы до всех
таблиц схемы; от найденной таблицы `contains` вниз размазал бы её счёт
по сорока колонкам. Работа структурных рёбер сделана раньше, единицей
выдачи в 8.1. Вид ребра без множителя в конфиге в обходе не участвует.

**Как считается.** Активация распространяется от каждого опорного узла:
сосед получает `s_base` опорного, умноженный на вес ребра и на множитель
его вида, сосед соседа — ещё раз умноженный, и так `depth` шагов. Вклады
с разных сторон складываются: узел, к которому ведут два опорных,
поднимается выше того, к которому ведёт один. Это распространение
активации; его обобщение на любое число шагов — персонализированный
PageRank с рестартом от опорных узлов, который считают HippoRAG и
локальный поиск GraphRAG. Два шага — практический предел для запроса в
базе; PageRank до сходимости требует графа в памяти и остаётся следующим
этапом, если стенд покажет, что двух шагов мало.

```
s_graph(n) = Σ по путям от опорных к n длиной ≤ depth:
             s_base(seed) · Π по рёбрам пути (weight · factor[kind])
```

Порт один, `GraphStore.expand(seeds, depth)`, реализации две. Реляционная —
рекурсивный CTE по `adjacency` (2.4):

```sql
with recursive
    seed(node_id, s_base) as (
        values (17, 0.0482), (21, 0.0311)                     -- опорные узлы из 8.1, параметр запроса
    ),
    factor(kind, value) as (
        values ('link', 1.0), ('mention', 0.8), ('similar', 0.7), ('entity', 0.7)   -- [search.expand.factors]
    ),
    walk as (
        select
            s.node_id,
            0 as depth,
            s.s_base as activation,
            array[s.node_id] as path,
            array[]::text[] as kinds
        from
            seed s
        union all
        select
            a.target_id,
            w.depth + 1,
            w.activation * a.weight * f.value,
            w.path || a.target_id,
            w.kinds || a.kind
        from
            walk w
            join confluence.adjacency a on
                a.source_id = w.node_id
            join factor f on                                   -- вид без множителя — не идём
                f.kind = a.kind
        where
            w.depth < %(depth)s
            and not a.target_id = any(w.path)                  -- без циклов
    ),
    reached as (
        select
            node_id,
            sum(activation) as s_graph,
            min(depth) as distance,
            (array_agg(path order by activation desc))[1] as path,    -- путь с наибольшим вкладом: для «почему пришёл»
            (array_agg(kinds order by activation desc))[1] as kinds
        from
            walk
        where
            depth > 0
        group by
            node_id
    )
select
    r.node_id,
    n.kind,
    n.address,
    r.s_graph,
    r.distance,
    r.path,
    r.kinds
from
    reached r
    join confluence.nodes n on
        n.id = r.node_id
order by
    r.s_graph desc
limit %(limit)s
```

Опорный узел, до которого дотекла активация от другого опорного, тоже
попадает в `reached`: два найденных узла, ссылающиеся друг на друга,
подтверждают друг друга, и ядро сложит `s_graph` к их `s_base`.

Бэкенд AGE делает то же одним Cypher: рёбра там помечены видом, и
переменная длина пути с фильтром по метке пишется одной строкой:

```sql
select
    w.node_id::bigint,
    w.s_graph::float,
    w.distance::int
from
    cypher('confluence_graph', $$
        unwind $seeds as s
        match p = (a:node {node_id: s.node_id})-[e*1..2]-(b:node)
        where all(r in e where label(r) in keys($factors))
        with b, s.s_base * reduce(w = 1.0, r in e | w * r.weight * $factors[label(r)]) as activation, length(p) as depth
        return b.node_id, sum(activation), min(depth)
    $$, $params) as w(node_id agtype, s_graph agtype, distance agtype)
```

### 8.3 Счёт и выдача

Сложение делает ядро: строк здесь не больше `seed_k` плюс достигнутые,
это десятки, а не сотни, и они уже с `kind`, адресом и метриками (`NodeSearch`, 2.2.2).

Счёт: `score = s_base + weight · s_graph + Σ μ_m · metric_m`, где `weight`
из `[search.expand]`, `μ_m` из `[search.metrics]`, метрики — глобальные
величины из `ranks` (2.5). Реранк кросс-энкодером первых N — стадия
поверх этого счёта, не способ поиска; добавляется отдельно, когда
понадобится.

**Выдача.** Каждый `Match` рендерится двумя способами из одних полей.
Большой модели — заголовок, `kind`, адрес строкой (`render()` модели
адреса корпуса, чтобы следующим вызовом попросить именно этот узел),
фрагменты с подписями способов и путь: «пришёл по `link` от FLIP-457».
Человеку в чат — заголовок ссылкой, фрагменты, где фрагмент с
`content_kind = image` показывается картинкой, а путь — словами: «на неё
ссылаются две найденные страницы».

**Пример.** Запрос «таймауты подключения к postgres». `section/fts`
находит страницу про libpq третьей, `summary/vector` — её же первой,
`title/trigram` — страницу «Настройки драйвера Postgres» первой. Страница
про libpq набирает `1/63 + 1.5/61`, «Настройки драйвера» — `1.5/61`;
обе опорные. С `expand` от них по `link` и `mention` приходит страница
«Пул соединений в сервисах», на которую ссылаются обе; слов запроса в ней
нет, но в выдаче она третья, с путём «`link` от libpq, `mention` от
Настроек драйвера».

### 8.4 Ход по графу руками: kb_node и kb_related

Расширение внутри `kb_search` одинаково для всех запросов: множители
заданы конфигом и смысла запроса не знают. Когда модели нужно идти по
графу осмысленно, у неё есть отдельные инструменты, и каждый шаг там
стоит вызова модели, зато решение принимает она.

- `kb_node(corpus, address)` — узел целиком: адрес строкой разбирает
  `parse()` модели адреса корпуса, документ собирает
  `Corpus.document` (2.8); рёбра узла с обоснованием, включая
  `entity` к его сущностям, — из `edges`. Для узла-сущности документ —
  кто её упоминает и с каким весом.
- `kb_related(corpus, address, kinds)` — соседи узла по видам рёбер
  через `GraphStore.neighbors`, с весом и обоснованием, заголовки от
  корпуса. Здесь структурные рёбра как раз нужны: «покажи колонки этой
  таблицы», «покажи вложения этой страницы».

Связи между корпусами (страница описывает таблицу) — вне плана: у каждой
схемы своя нумерация узлов, мост — отдельная таблица без внешних ключей.
Проектируется с появлением второго корпуса.

## 9. Этапы

1. **Проба NER** — сделана на английском; русская проба на внутреннем
   Confluence — результат в 5.6.
2. **Ядро и хранение.** `boba-graph`: модели, протоколы `Address` и
   `Evidence`, порты, конвейер;
   `boba-db-pggraph`: DDL graph tables, реализации портов, реляционный
   `GraphStore`, `SearchStore` по индексам; установка `confluence` и
   `confluence_test`.
2a. **AGE.** Второй `GraphStore`, граф `confluence_graph`, те же тесты на
   обоих бэкендах; `kb_graph_check`.
3. **Корпус Confluence.** В `boba-confluence`: модели узлов рядом с
   `models.py`, подмодуль `graph` за extra — виды содержимого и рёбер,
   content tables и их DDL, способы поиска, ридер 2.0; инструменты
   индексации в `boba-tool-confluence`; конвейер пишет `nodes`, `sync`,
   `pages`, `page_sections`, `attachments`, `attachment_texts`.
4. **Явные рёбра.** `in_space`, `child_page`, `has_attachment`, `link`,
   `attachment_ref`, `mention`, `series`, `pending_links`.
5. **Сущности** как узлы `entity` и рёбра `entity` к ним, tf-idf.
6. **Семантика.** Векторные индексы, рёбра `similar`.
7. **Саммари.** `page_summaries`, генератор по схеме, способ `summary` в `applied_methods`.
8. **Глобальная стадия.** `kb_graph_rebuild`, `ranks`.
9. **Поиск.** `boba-tool-graph`: `kb_search` по алгоритму раздела 8
   (сначала без расширения, затем расширение по флагу), `kb_related`,
   `kb_node`; документ узла — в корпусе.

Текущий индексатор всё это время не трогается; его судьба решается
отдельно, когда 2.0 принят. Корпус Warehouse — следующий план поверх
этого: интроспекторы движков, профили, косвенные рёбра.
