-- Сырые таблицы каталога: по одной на @name scrape-запроса, колонки в порядке выборки.
-- Все таблицы временные: живут в сессии загрузчика и исчезают с ней. Python делает
-- COPY raw_<name> FROM STDIN, ничего не преобразуя.
-- raw_source заполняется одной строкой: откуда снято, для адресов node. У ClickHouse
-- подключение идёт к серверу, а не к базе, поэтому scope это scheme, host, port.
create temp table raw_source (
    scheme  text not null,
    host    text not null,
    port    int  not null
);

create temp table raw_server (
    version      text,
    row_version  text
);

create temp table raw_databases (
    name         text,
    engine       text,
    engine_full  text,
    uuid         uuid,
    comment      text,
    row_version  text
);

create temp table raw_tables (
    database                       text,
    name                           text,
    uuid                           uuid,
    engine                         text,
    engine_full                    text,
    create_table_query             text,
    as_select                      text,
    partition_key                  text,
    sorting_key                    text,
    primary_key                    text,
    sampling_key                   text,
    storage_policy                 text,
    total_rows                     bigint,
    total_bytes                    bigint,
    comment                        text,
    modified_at                    timestamp,
    dependencies_database          jsonb,
    dependencies_table             jsonb,
    loading_dependencies_database  jsonb,
    loading_dependencies_table     jsonb,
    target_database                text,
    target_table                   text,
    row_version                    text
);

create temp table raw_columns (
    database             text,
    table_name           text,
    name                 text,
    type                 text,
    position             bigint,
    default_kind         text,
    default_expression   text,
    comment              text,
    is_in_partition_key  smallint,
    is_in_sorting_key    smallint,
    is_in_primary_key    smallint,
    is_in_sampling_key   smallint,
    compression_codec    text,
    row_version          text
);

create temp table raw_indices (
    database     text,
    table_name   text,
    name         text,
    type         text,
    type_full    text,
    expr         text,
    granularity  bigint,
    row_version  text
);

create temp table raw_projections (
    database     text,
    table_name   text,
    name         text,
    type         text,
    sorting_key  text,
    query        text,
    row_version  text
);

create temp table raw_dictionaries (
    database         text,
    name             text,
    uuid             uuid,
    origin           text,
    layout           text,
    key_names        jsonb,
    key_types        jsonb,
    attribute_names  jsonb,
    attribute_types  jsonb,
    source           text,
    lifetime_min     bigint,
    lifetime_max     bigint,
    comment          text,
    row_version      text
);

create temp table raw_functions (
    name          text,
    create_query  text,
    row_version   text
);
