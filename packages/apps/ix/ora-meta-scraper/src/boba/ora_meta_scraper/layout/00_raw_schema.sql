-- Сырые таблицы словаря: по одной на @name scrape-запроса, колонки в порядке выборки.
-- Все таблицы временные: живут в сессии загрузчика и исчезают с ней. Python делает
-- COPY raw_<name> FROM STDIN, ничего не преобразуя.
-- raw_source заполняется одной строкой: откуда снято, для адресов node. Соединение с
-- Oracle идёт к сервису (PDB), поэтому scope это scheme, host, port, database.
-- Битовые поля словаря (property, flags) шире bigint, они numeric.
create temp table raw_source (
    scheme    text not null,
    host      text not null,
    port      int  not null,
    database  text not null
);

create temp table raw_database (
    con_name     text,
    db_name      text,
    version      text,
    charset      text,
    row_version  text
);

create temp table raw_users (
    user_id      bigint,
    name         text,
    created      timestamp,
    row_version  text
);

create temp table raw_tablespaces (
    ts_id        bigint,
    name         text,
    row_version  text
);

create temp table raw_objects (
    obj_id         bigint,
    owner_id       bigint,
    name           text,
    namespace      int,
    type_id        int,
    created        timestamp,
    last_ddl_time  timestamp,
    spec_time      timestamp,
    status         int,
    flags          numeric,
    row_version    text
);

create temp table raw_tables (
    obj_id       bigint,
    ts_id        bigint,
    property     numeric,
    flags        numeric,
    trigflag     numeric,
    row_count    bigint,
    row_version  text
);

create temp table raw_columns (
    obj_id         bigint,
    col_id         int,
    intcol_id      int,
    segcol_id      int,
    name           text,
    type_id        int,
    length         int,
    precision_num  int,
    scale          int,
    null_flag      int,
    deflength      int,
    default_text   text,
    property       numeric,
    charsetform    int,
    char_length    int,
    row_version    text
);

create temp table raw_comments (
    obj_id        bigint,
    col_id        int,
    comment_text  text,
    row_version   text
);

create temp table raw_con (
    con_id       bigint,
    owner_id     bigint,
    name         text,
    row_version  text
);

create temp table raw_cdef (
    con_id       bigint,
    obj_id       bigint,
    cols         int,
    type_id      int,
    robj_id      bigint,
    rcon_id      bigint,
    enabled      bigint,
    defer_flags  numeric,
    refact       int,
    mtime        timestamp,
    condition    text,
    row_version  text
);

create temp table raw_ccol (
    con_id       bigint,
    obj_id       bigint,
    col_id       int,
    intcol_id    int,
    pos          int,
    row_version  text
);

create temp table raw_indexes (
    obj_id       bigint,
    bo_id        bigint,
    ts_id        bigint,
    type_id      int,
    property     numeric,
    flags        numeric,
    intcols      int,
    prefix_len   int,
    row_version  text
);

create temp table raw_icol (
    obj_id       bigint,
    bo_id        bigint,
    col_id       int,
    pos          int,
    intcol_id    int,
    spare1       numeric,
    spare2       numeric,
    row_version  text
);

create temp table raw_views (
    obj_id       bigint,
    textlength   int,
    text         text,
    property     numeric,
    row_version  text
);

create temp table raw_mviews (
    owner_name      text,
    name            text,
    container_name  text,
    query_len       int,
    query_text      text,
    flag            numeric,
    auto_fast       text,
    row_version     text
);

create temp table raw_sequences (
    obj_id        bigint,
    increment_by  numeric,
    min_value     numeric,
    max_value     numeric,
    cycle_flag    int,
    order_flag    int,
    cache_size    numeric,
    flags         numeric,
    row_version   text
);

create temp table raw_synonyms (
    obj_id       bigint,
    node         text,
    owner_name   text,
    name         text,
    row_version  text
);

create temp table raw_triggers (
    obj_id       bigint,
    base_obj_id  bigint,
    type_id      int,
    insert_flag  int,
    update_flag  int,
    delete_flag  int,
    enabled      int,
    property     numeric,
    row_version  text
);

create temp table raw_dependencies (
    d_obj_id     bigint,
    p_obj_id     bigint,
    property     numeric,
    row_version  text
);

create temp table raw_partobj (
    obj_id       bigint,
    parttype     int,
    partcnt      int,
    partkeycols  int,
    spare2       numeric,
    row_version  text
);

create temp table raw_partcol (
    obj_id       bigint,
    intcol_id    int,
    pos          int,
    row_version  text
);
