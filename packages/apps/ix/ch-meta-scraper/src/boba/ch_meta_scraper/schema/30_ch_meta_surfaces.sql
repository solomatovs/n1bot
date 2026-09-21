/*
ch-meta-scraper, схема, шаг 3: surface-таблицы node ClickHouse, ключ node_id.
Изменившаяся строка удаляется и вставляется заново, полей updated_at и content_hash нет.
Тексты каталога (create_query, as_select, ключи, выражения индексов, источник словаря)
лежат как есть: по ним объект восстанавливается, а связи, которых нет в system-таблицах
структурно, достаёт из текста описатель.
*/
create table if not exists {schema}.ch_meta_server (
    node_id  bigint primary key references {schema}.node on delete cascade,
    host     varchar,
    port     int,
    version  varchar
);

create table if not exists {schema}.ch_meta_database (
    node_id      bigint primary key references {schema}.node on delete cascade,
    name         varchar,
    engine       varchar,
    engine_full  varchar,
    uuid         uuid,
    comment      varchar
);

create table if not exists {schema}.ch_meta_table (
    node_id         bigint primary key references {schema}.node on delete cascade,
    database_name   varchar,
    name            varchar,
    uuid            uuid,
    engine          varchar,
    engine_full     varchar,
    partition_key   varchar,
    sorting_key     varchar,
    primary_key     varchar,
    sampling_key    varchar,
    storage_policy  varchar,
    total_rows      bigint,
    total_bytes     bigint,
    comment         varchar,
    create_query    varchar,
    modified_at     timestamp
);

create table if not exists {schema}.ch_meta_view (
    node_id          bigint primary key references {schema}.node on delete cascade,
    database_name    varchar,
    name             varchar,
    uuid             uuid,
    engine           varchar,
    kind             varchar,
    engine_full      varchar,
    as_select        varchar,
    partition_key    varchar,
    sorting_key      varchar,
    primary_key      varchar,
    target_database  varchar,
    target_table     varchar,
    comment          varchar,
    create_query     varchar,
    modified_at      timestamp
);

create table if not exists {schema}.ch_meta_column (
    node_id             bigint primary key references {schema}.node on delete cascade,
    database_name       varchar,
    relation_name       varchar,
    relation_kind       varchar,
    name                varchar,
    ordinal             int,
    data_type           varchar,
    default_kind        varchar,
    default_expression  varchar,
    codec               varchar,
    in_partition_key    bool,
    in_sorting_key      bool,
    in_primary_key      bool,
    in_sampling_key     bool,
    comment             varchar
);

create table if not exists {schema}.ch_meta_index (
    node_id        bigint primary key references {schema}.node on delete cascade,
    database_name  varchar,
    table_name     varchar,
    name           varchar,
    kind           varchar,
    kind_full      varchar,
    expr           varchar,
    granularity    int
);

create table if not exists {schema}.ch_meta_projection (
    node_id        bigint primary key references {schema}.node on delete cascade,
    database_name  varchar,
    table_name     varchar,
    name           varchar,
    kind           varchar,
    sorting_key    varchar,
    query          varchar
);

create table if not exists {schema}.ch_meta_dictionary (
    node_id          bigint primary key references {schema}.node on delete cascade,
    database_name    varchar,
    name             varchar,
    uuid             uuid,
    origin           varchar,
    layout           varchar,
    key_names        varchar[],
    key_types        varchar[],
    attribute_names  varchar[],
    attribute_types  varchar[],
    source           varchar,
    lifetime_min     bigint,
    lifetime_max     bigint,
    comment          varchar,
    create_query     varchar
);

create table if not exists {schema}.ch_meta_function (
    node_id       bigint primary key references {schema}.node on delete cascade,
    name          varchar,
    create_query  varchar
);
