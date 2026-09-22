-- Стадии surface: свойства node по видам, из raw_* через stage_node. Одна temp-таблица на
-- surface, первая колонка address, дальше колонки surface-таблицы {schema}.<surface> по порядку.

create temp table stage_ch_meta_server (
    address  jsonb not null primary key,
    host     varchar,
    port     int,
    version  varchar
);

insert into stage_ch_meta_server
select
    n.address,
    s.host,
    s.port,
    srv.version
from
    raw_server srv,
    raw_source s,
    stage_node n
where
    n.kind = 'srv';

create temp table stage_ch_meta_database (
    address      jsonb not null primary key,
    name         varchar,
    engine       varchar,
    engine_full  varchar,
    uuid         uuid,
    comment      varchar
);

insert into stage_ch_meta_database
select
    n.address,
    d.name,
    d.engine,
    d.engine_full,
    d.uuid,
    nullif(d.comment, '')
from
    raw_databases d
    join stage_node n on n.kind = 'db' and n.database = d.name;

create temp table stage_ch_meta_table (
    address         jsonb not null primary key,
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

insert into stage_ch_meta_table
select
    n.address,
    t.database,
    t.name,
    t.uuid,
    t.engine,
    nullif(t.engine_full, ''),
    nullif(t.partition_key, ''),
    nullif(t.sorting_key, ''),
    nullif(t.primary_key, ''),
    nullif(t.sampling_key, ''),
    nullif(t.storage_policy, ''),
    t.total_rows,
    t.total_bytes,
    nullif(t.comment, ''),
    t.create_table_query,
    t.modified_at
from
    raw_tables t
    join stage_node n
        on  n.kind = 'rel'
        and n.surface = 'ch_meta_table'
        and n.database = t.database
        and n.relation = t.name;

create temp table stage_ch_meta_view (
    address          jsonb not null primary key,
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

insert into stage_ch_meta_view
select
    n.address,
    t.database,
    t.name,
    t.uuid,
    t.engine,
    case t.engine
        when 'MaterializedView' then 'materialized'
        when 'LiveView'         then 'live'
        when 'WindowView'       then 'window'
        else                         'view'
    end,
    nullif(t.engine_full, ''),
    nullif(t.as_select, ''),
    nullif(t.partition_key, ''),
    nullif(t.sorting_key, ''),
    nullif(t.primary_key, ''),
    nullif(t.target_database, ''),
    nullif(t.target_table, ''),
    nullif(t.comment, ''),
    t.create_table_query,
    t.modified_at
from
    raw_tables t
    join stage_node n
        on  n.kind = 'rel'
        and n.surface = 'ch_meta_view'
        and n.database = t.database
        and n.relation = t.name;

create temp table stage_ch_meta_column (
    address             jsonb not null primary key,
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

insert into stage_ch_meta_column
select
    n.address,
    c.database,
    c.table_name,
    case p.surface
        when 'ch_meta_view'       then 'view'
        when 'ch_meta_dictionary' then 'dictionary'
        else                           'table'
    end,
    c.name,
    c.position,
    c.type,
    nullif(c.default_kind, ''),
    nullif(c.default_expression, ''),
    nullif(c.compression_codec, ''),
    c.is_in_partition_key = 1,
    c.is_in_sorting_key = 1,
    c.is_in_primary_key = 1,
    c.is_in_sampling_key = 1,
    nullif(c.comment, '')
from
    raw_columns c
    join stage_node n
        on  n.kind = 'col'
        and n.database = c.database
        and n.relation = c.table_name
        and n.name = c.name
    join stage_node p
        on  p.kind in ('rel', 'dict')
        and p.database = c.database
        and p.relation = c.table_name;

create temp table stage_ch_meta_index (
    address        jsonb not null primary key,
    database_name  varchar,
    table_name     varchar,
    name           varchar,
    kind           varchar,
    kind_full      varchar,
    expr           varchar,
    granularity    int
);

insert into stage_ch_meta_index
select
    n.address,
    i.database,
    i.table_name,
    i.name,
    i.type,
    i.type_full,
    i.expr,
    i.granularity
from
    raw_indices i
    join stage_node n
        on  n.kind = 'idx'
        and n.database = i.database
        and n.relation = i.table_name
        and n.name = i.name;

create temp table stage_ch_meta_projection (
    address        jsonb not null primary key,
    database_name  varchar,
    table_name     varchar,
    name           varchar,
    kind           varchar,
    sorting_key    varchar,
    query          varchar
);

insert into stage_ch_meta_projection
select
    n.address,
    pr.database,
    pr.table_name,
    pr.name,
    pr.type,
    nullif(pr.sorting_key, ''),
    pr.query
from
    raw_projections pr
    join stage_node n
        on  n.kind = 'proj'
        and n.database = pr.database
        and n.relation = pr.table_name
        and n.name = pr.name;

create temp table stage_ch_meta_dictionary (
    address          jsonb not null primary key,
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

insert into stage_ch_meta_dictionary
select
    n.address,
    x.database,
    x.name,
    x.uuid,
    x.origin,
    x.layout,
    array(select jsonb_array_elements_text(x.key_names))::varchar[],
    array(select jsonb_array_elements_text(x.key_types))::varchar[],
    array(select jsonb_array_elements_text(x.attribute_names))::varchar[],
    array(select jsonb_array_elements_text(x.attribute_types))::varchar[],
    nullif(x.source, ''),
    x.lifetime_min,
    x.lifetime_max,
    nullif(x.comment, ''),
    t.create_table_query
from
    raw_dictionaries x
    join stage_node n
        on  n.kind = 'dict'
        and n.database = x.database
        and n.relation = x.name
    left
    join raw_tables t on t.database = x.database and t.name = x.name;

create temp table stage_ch_meta_function (
    address       jsonb not null primary key,
    name          varchar,
    create_query  varchar
);

insert into stage_ch_meta_function
select
    n.address,
    f.name,
    f.create_query
from
    raw_functions f
    join stage_node n on n.kind = 'fn' and n.name = f.name;
