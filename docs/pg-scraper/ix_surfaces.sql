-- Surface-таблицы node, которые заполняет layout/45_surfaces.sql и 50_apply.sql.
-- Ключ node_id; изменившаяся строка удаляется и вставляется заново, полей updated_at и
-- content_hash нет: сравнение идёт по всем колонкам.

create table ix.pg_database (
    node_id          bigint primary key references ix.node on delete cascade,
    name             varchar,
    owner            varchar,
    encoding         varchar,
    collate_name     varchar,
    ctype            varchar,
    comment          varchar
);

create table ix.pg_schema (
    node_id          bigint primary key references ix.node on delete cascade,
    name             varchar,
    owner            varchar,
    comment          varchar
);

create table ix.pg_table (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    name             varchar,
    kind             varchar,
    owner            varchar,
    tablespace       varchar,
    persistence      varchar,
    partition_bound  varchar,
    row_estimate     float8,
    pages            int,
    has_index        bool,
    has_triggers     bool,
    distribution     varchar,
    storage          varchar,
    comment          varchar
);

create table ix.pg_column (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    relation_name    varchar,
    relation_kind    varchar,
    name             varchar,
    ordinal          int,
    data_type        varchar,
    not_null         bool,
    default_expr     varchar,
    identity         varchar,
    generated        varchar,
    comment          varchar
);

create table ix.pg_view (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    name             varchar,
    kind             varchar,
    owner            varchar,
    comment          varchar
);

create table ix.pg_index (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    table_name       varchar,
    name             varchar,
    access_method    varchar,
    is_unique        bool,
    is_primary       bool,
    is_exclusion     bool,
    is_valid         bool,
    columns          varchar[],
    expression       varchar,
    predicate        varchar,
    comment          varchar
);

create table ix.pg_sequence (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    name             varchar,
    owner            varchar,
    data_type        varchar,
    start_value      bigint,
    increment        bigint,
    min_value        bigint,
    max_value        bigint,
    cycle            bool,
    comment          varchar
);

create table ix.pg_routine (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    name             varchar,
    kind             varchar,
    language         varchar,
    identity_args    varchar,
    result_type      varchar,
    volatility       varchar,
    security_definer bool,
    owner            varchar,
    comment          varchar
);

create table ix.pg_constraint (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    table_name       varchar,
    name             varchar,
    kind             varchar,
    definition       varchar,
    is_deferrable    bool,
    is_deferred      bool,
    is_validated     bool,
    on_update        varchar,
    on_delete        varchar,
    match_type       varchar,
    comment          varchar
);

create table ix.pg_trigger (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    table_name       varchar,
    name             varchar,
    timing           varchar,
    events           varchar[],
    row_level        bool,
    enabled          bool,
    comment          varchar
);

create table ix.pg_type (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    name             varchar,
    kind             varchar,
    base_type        varchar,
    enum_labels      varchar[],
    comment          varchar
);

create table ix.pg_statistics (
    node_id          bigint primary key references ix.node on delete cascade,
    schema_name      varchar,
    name             varchar,
    table_name       varchar,
    kinds            varchar[],
    comment          varchar
);
