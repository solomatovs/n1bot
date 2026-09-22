/*
ora-meta-scraper, схема, шаг 3: surface-таблицы node Oracle, ключ node_id.
Изменившаяся строка удаляется и вставляется заново, полей updated_at и content_hash нет.
Битовые поля словаря здесь уже разложены в понятные признаки; тексты (запрос
представления, условие check, умолчание колонки) лежат как есть.
*/
create table if not exists {schema}.ora_meta_database (
    node_id   bigint primary key references {schema}.node on delete cascade,
    host      varchar,
    port      int,
    service   varchar,
    con_name  varchar,
    db_name   varchar,
    version   varchar,
    charset   varchar
);

create table if not exists {schema}.ora_meta_schema (
    node_id  bigint primary key references {schema}.node on delete cascade,
    name     varchar,
    created  timestamp
);

create table if not exists {schema}.ora_meta_table (
    node_id         bigint primary key references {schema}.node on delete cascade,
    schema_name     varchar,
    name            varchar,
    tablespace      varchar,
    partitioned     bool,
    partition_type  varchar,
    temporary       bool,
    iot             bool,
    num_rows        bigint,
    comment         varchar,
    status          varchar,
    created         timestamp,
    last_ddl_time   timestamp
);

create table if not exists {schema}.ora_meta_view (
    node_id        bigint primary key references {schema}.node on delete cascade,
    schema_name    varchar,
    name           varchar,
    text           varchar,
    comment        varchar,
    status         varchar,
    created        timestamp,
    last_ddl_time  timestamp
);

create table if not exists {schema}.ora_meta_mview (
    node_id        bigint primary key references {schema}.node on delete cascade,
    schema_name    varchar,
    name           varchar,
    query          varchar,
    refresh_mode   varchar,
    comment        varchar,
    status         varchar,
    created        timestamp,
    last_ddl_time  timestamp
);

create table if not exists {schema}.ora_meta_column (
    node_id         bigint primary key references {schema}.node on delete cascade,
    schema_name     varchar,
    relation_name   varchar,
    relation_kind   varchar,
    name            varchar,
    ordinal         int,
    data_type       varchar,
    data_length     int,
    data_precision  int,
    data_scale      int,
    nullable        bool,
    default_text    varchar,
    virtual         bool,
    identity        bool,
    comment         varchar
);

create table if not exists {schema}.ora_meta_constraint (
    node_id           bigint primary key references {schema}.node on delete cascade,
    schema_name       varchar,
    table_name        varchar,
    name              varchar,
    kind              varchar,
    search_condition  varchar,
    ref_schema        varchar,
    ref_constraint    varchar,
    delete_rule       varchar,
    enabled           bool,
    validated         bool,
    is_deferrable     bool
);

create table if not exists {schema}.ora_meta_index (
    node_id        bigint primary key references {schema}.node on delete cascade,
    schema_name    varchar,
    table_name     varchar,
    name           varchar,
    index_type     varchar,
    is_unique      bool,
    tablespace     varchar,
    columns        varchar,
    status         varchar,
    created        timestamp,
    last_ddl_time  timestamp
);

create table if not exists {schema}.ora_meta_sequence (
    node_id       bigint primary key references {schema}.node on delete cascade,
    schema_name   varchar,
    name          varchar,
    min_value     numeric,
    max_value     numeric,
    increment_by  numeric,
    cycle         bool,
    ordered       bool,
    cache_size    numeric
);

create table if not exists {schema}.ora_meta_synonym (
    node_id        bigint primary key references {schema}.node on delete cascade,
    schema_name    varchar,
    name           varchar,
    target_schema  varchar,
    target_name    varchar,
    db_link        varchar
);

create table if not exists {schema}.ora_meta_trigger (
    node_id       bigint primary key references {schema}.node on delete cascade,
    schema_name   varchar,
    table_name    varchar,
    name          varchar,
    trigger_type  varchar,
    event         varchar,
    enabled       bool,
    status        varchar
);

create table if not exists {schema}.ora_meta_routine (
    node_id        bigint primary key references {schema}.node on delete cascade,
    schema_name    varchar,
    name           varchar,
    kind           varchar,
    status         varchar,
    created        timestamp,
    last_ddl_time  timestamp
);
