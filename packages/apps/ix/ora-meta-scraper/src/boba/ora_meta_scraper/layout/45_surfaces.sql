-- Стадии surface: свойства node по видам, из raw_* через stage_node. Одна temp-таблица на
-- surface, первая колонка address, дальше колонки surface-таблицы {schema}.<surface> по
-- порядку. Битовые поля словаря раскладываются формулами из определений dba_*: бит k
-- это mod(floor(x / 2^k), 2) = 1.

create temp table stage_ora_meta_database (
    address   jsonb not null primary key,
    host      varchar,
    port      int,
    service   varchar,
    con_name  varchar,
    db_name   varchar,
    version   varchar,
    charset   varchar
);

insert into stage_ora_meta_database
select
    n.address,
    s.host,
    s.port,
    s.database,
    d.con_name,
    d.db_name,
    d.version,
    d.charset
from
    raw_database d,
    raw_source s,
    stage_node n
where
    n.kind = 'db';

create temp table stage_ora_meta_schema (
    address  jsonb not null primary key,
    name     varchar,
    created  timestamp
);

insert into stage_ora_meta_schema
select
    n.address,
    u.name,
    u.created
from
    raw_users u
    join stage_node n on n.kind = 'sch' and n.obj_id = u.user_id;

-- Комментарий объекта: строка com$ без номера колонки; у mview комментарий может
-- висеть и на объекте type# 42, и на контейнере
create temp table stage_comment as
select
    o.obj_id, c.comment_text
from
    raw_comments c
    join raw_objects o on o.obj_id = c.obj_id
where
    c.col_id is null;

create temp table stage_ora_meta_table (
    address         jsonb not null primary key,
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

insert into stage_ora_meta_table
select
    n.address,
    n.address->>'schema',
    o.name,
    ts.name,
    mod(floor(t.property / 32), 2) = 1,
    case po.parttype
        when 1 then 'RANGE' when 2 then 'HASH' when 3 then 'SYSTEM'
        when 4 then 'LIST' when 5 then 'REFERENCE'
    end,
    mod(floor(o.flags / 2), 2) = 1,
    mod(floor(t.property / 64), 2) = 1,
    t.row_count,
    cm.comment_text,
    case o.status when 1 then 'VALID' when 0 then 'N/A' else 'INVALID' end,
    o.created,
    o.last_ddl_time
from
    stage_node n
    join raw_objects o on o.obj_id = n.obj_id
    join raw_tables t on t.obj_id = n.obj_id
    left join raw_tablespaces ts on ts.ts_id = t.ts_id
    left join raw_partobj po on po.obj_id = n.obj_id
    left join stage_comment cm on cm.obj_id = n.obj_id
where
    n.surface = 'ora_meta_table';

create temp table stage_ora_meta_view (
    address        jsonb not null primary key,
    schema_name    varchar,
    name           varchar,
    text           varchar,
    comment        varchar,
    status         varchar,
    created        timestamp,
    last_ddl_time  timestamp
);

insert into stage_ora_meta_view
select
    n.address,
    n.address->>'schema',
    o.name,
    v.text,
    cm.comment_text,
    case o.status when 1 then 'VALID' when 0 then 'N/A' else 'INVALID' end,
    o.created,
    o.last_ddl_time
from
    stage_node n
    join raw_objects o on o.obj_id = n.obj_id
    left join raw_views v on v.obj_id = n.obj_id
    left join stage_comment cm on cm.obj_id = n.obj_id
where
    n.surface = 'ora_meta_view';

-- mview: node по контейнеру, свойства объекта type# 42 через stage_alias
create temp table stage_ora_meta_mview (
    address        jsonb not null primary key,
    schema_name    varchar,
    name           varchar,
    query          varchar,
    refresh_mode   varchar,
    comment        varchar,
    status         varchar,
    created        timestamp,
    last_ddl_time  timestamp
);

insert into stage_ora_meta_mview
select
    n.address,
    n.address->>'schema',
    m.name,
    m.query_text,
    case
        when m.auto_fast = 'N' then 'NEVER'
        when mod(floor(m.flag / 32768), 2) = 1 then 'COMMIT'
        else 'DEMAND'
    end,
    coalesce(cm42.comment_text, cm.comment_text),
    case o.status when 1 then 'VALID' when 0 then 'N/A' else 'INVALID' end,
    o.created,
    o.last_ddl_time
from
    stage_node n
    join raw_objects c on c.obj_id = n.obj_id
    join raw_users u on u.user_id = c.owner_id
    join raw_mviews m on m.owner_name = u.name and m.container_name = c.name
    join raw_objects o on o.owner_id = u.user_id and o.name = m.name and o.type_id = 42
    left join stage_comment cm on cm.obj_id = c.obj_id
    left join stage_comment cm42 on cm42.obj_id = o.obj_id
where
    n.surface = 'ora_meta_mview';

create temp table stage_ora_meta_column (
    address         jsonb not null primary key,
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

insert into stage_ora_meta_column
select
    n.address,
    n.address->>'schema',
    coalesce(n.address->>'table', n.address->>'view', n.address->>'mview'),
    case
        when n.address ? 'table' then 'table'
        when n.address ? 'view' then 'view'
        else 'mview'
    end,
    c.name,
    c.col_id,
    case c.type_id
        when 1 then case when c.charsetform = 2 then 'NVARCHAR2' else 'VARCHAR2' end
        when 2 then case
            when c.scale is null and c.precision_num is not null then 'FLOAT'
            else 'NUMBER'
        end
        when 8 then 'LONG'
        when 9 then case when c.charsetform = 2 then 'NCHAR VARYING' else 'VARCHAR' end
        when 12 then 'DATE'
        when 23 then 'RAW'
        when 24 then 'LONG RAW'
        when 69 then 'ROWID'
        when 96 then case when c.charsetform = 2 then 'NCHAR' else 'CHAR' end
        when 100 then 'BINARY_FLOAT'
        when 101 then 'BINARY_DOUBLE'
        when 112 then case when c.charsetform = 2 then 'NCLOB' else 'CLOB' end
        when 113 then 'BLOB'
        when 114 then 'BFILE'
        when 180 then 'TIMESTAMP(' || c.scale || ')'
        when 181 then 'TIMESTAMP(' || c.scale || ') WITH TIME ZONE'
        when 231 then 'TIMESTAMP(' || c.scale || ') WITH LOCAL TIME ZONE'
        when 182 then 'INTERVAL YEAR(' || c.precision_num || ') TO MONTH'
        when 183 then 'INTERVAL DAY(' || c.precision_num || ') TO SECOND(' || c.scale || ')'
        when 208 then 'UROWID'
        when 58 then 'OBJECT'
        when 111 then 'REF'
        when 121 then 'OBJECT'
        when 122 then 'NESTED TABLE'
        when 123 then 'VARRAY'
        else 'UNDEFINED'
    end,
    case when c.type_id in (1, 9, 96) then coalesce(c.char_length, c.length) else c.length end,
    c.precision_num,
    c.scale,
    c.null_flag = 0,
    c.default_text,
    mod(floor(c.property / 8), 2) = 1,
    mod(floor(c.property / 137438953472), 2) = 1
        or mod(floor(c.property / 274877906944), 2) = 1,
    cm.comment_text
from
    stage_node n
    join raw_columns c on c.obj_id = n.obj_id and c.intcol_id = n.sub_id
    left join raw_comments cm on cm.obj_id = c.obj_id and cm.col_id = c.intcol_id
where
    n.kind = 'col';

create temp table stage_ora_meta_constraint (
    address           jsonb not null primary key,
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

insert into stage_ora_meta_constraint
select
    n.address,
    n.address->>'schema',
    coalesce(n.address->>'table', n.address->>'view', n.address->>'mview'),
    cn.name,
    case d.type_id
        when 1 then 'C' when 2 then 'P' when 3 then 'U'
        when 4 then 'R' when 5 then 'V' when 6 then 'O'
    end,
    d.condition,
    ru.name,
    rc.name,
    case
        when d.type_id <> 4 then null
        when d.refact = 1 then 'CASCADE'
        when d.refact = 2 then 'SET NULL'
        else 'NO ACTION'
    end,
    d.type_id = 5 or d.enabled is not null,
    mod(floor(d.defer_flags / 4), 2) = 1,
    mod(floor(d.defer_flags / 1), 2) = 1
from
    stage_node n
    join raw_cdef d on d.con_id = n.obj_id
    join raw_con cn on cn.con_id = d.con_id
    left join raw_con rc on rc.con_id = d.rcon_id
    left join raw_users ru on ru.user_id = rc.owner_id
where
    n.kind = 'con';

-- Колонки индекса текстом по позициям; выражение функционального индекса берётся из
-- default$ скрытой колонки, DESC из бита 131072 свойств колонки
create temp table stage_index_columns as
select
    ic.obj_id,
    string_agg(
        case
            when mod(floor(ic.spare1 / 1), 2) = 1 then c.default_text
            else c.name
        end
            || case when mod(floor(c.property / 131072), 2) = 1 then ' DESC' else '' end,
        ', ' order by ic.pos
    ) as columns
from
    raw_icol ic
    join raw_columns c on c.obj_id = ic.bo_id and c.intcol_id = ic.intcol_id
group by
    ic.obj_id;

create temp table stage_ora_meta_index (
    address        jsonb not null primary key,
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

insert into stage_ora_meta_index
select
    n.address,
    n.address->>'schema',
    coalesce(n.address->>'table', n.address->>'mview'),
    o.name,
    case when mod(floor(i.property / 16), 2) = 1 then 'FUNCTION-BASED ' else '' end
        || case i.type_id
            when 1 then 'NORMAL' || case when mod(floor(i.property / 4), 2) = 1 then '/REV' else '' end
            when 2 then 'BITMAP' when 3 then 'CLUSTER' when 4 then 'IOT - TOP'
            when 6 then 'SECONDARY' when 7 then 'ANSI' when 9 then 'DOMAIN'
        end,
    mod(floor(i.property / 1), 2) = 1,
    ts.name,
    ix.columns,
    case when mod(floor(i.flags / 1), 2) = 1 then 'UNUSABLE' else 'VALID' end,
    o.created,
    o.last_ddl_time
from
    stage_node n
    join raw_indexes i on i.obj_id = n.obj_id
    join raw_objects o on o.obj_id = n.obj_id
    left join raw_tablespaces ts on ts.ts_id = i.ts_id
    left join stage_index_columns ix on ix.obj_id = n.obj_id
where
    n.kind = 'idx';

create temp table stage_ora_meta_sequence (
    address       jsonb not null primary key,
    schema_name   varchar,
    name          varchar,
    min_value     numeric,
    max_value     numeric,
    increment_by  numeric,
    cycle         bool,
    ordered       bool,
    cache_size    numeric
);

insert into stage_ora_meta_sequence
select
    n.address,
    n.address->>'schema',
    o.name,
    s.min_value,
    s.max_value,
    s.increment_by,
    s.cycle_flag = 1,
    s.order_flag = 1,
    s.cache_size
from
    stage_node n
    join raw_objects o on o.obj_id = n.obj_id
    join raw_sequences s on s.obj_id = n.obj_id
where
    n.kind = 'seq';

create temp table stage_ora_meta_synonym (
    address        jsonb not null primary key,
    schema_name    varchar,
    name           varchar,
    target_schema  varchar,
    target_name    varchar,
    db_link        varchar
);

insert into stage_ora_meta_synonym
select
    n.address,
    n.address->>'schema',
    o.name,
    s.owner_name,
    s.name,
    s.node
from
    stage_node n
    join raw_objects o on o.obj_id = n.obj_id
    join raw_synonyms s on s.obj_id = n.obj_id
where
    n.kind = 'syn';

create temp table stage_ora_meta_trigger (
    address       jsonb not null primary key,
    schema_name   varchar,
    table_name    varchar,
    name          varchar,
    trigger_type  varchar,
    event         varchar,
    enabled       bool,
    status        varchar
);

insert into stage_ora_meta_trigger
select
    n.address,
    n.address->>'schema',
    coalesce(n.address->>'table', n.address->>'view'),
    o.name,
    case t.type_id
        when 0 then 'BEFORE STATEMENT' when 1 then 'BEFORE EACH ROW'
        when 2 then 'AFTER STATEMENT' when 3 then 'AFTER EACH ROW'
        when 4 then 'INSTEAD OF' when 5 then 'COMPOUND'
        else 'UNDEFINED'
    end,
    concat_ws(
        ' OR ',
        case when t.insert_flag = 1 then 'INSERT' end,
        case when t.update_flag = 1 then 'UPDATE' end,
        case when t.delete_flag = 1 then 'DELETE' end
    ),
    t.enabled = 1,
    case o.status when 1 then 'VALID' when 0 then 'N/A' else 'INVALID' end
from
    stage_node n
    join raw_objects o on o.obj_id = n.obj_id
    join raw_triggers t on t.obj_id = n.obj_id
where
    n.kind = 'trg';

create temp table stage_ora_meta_routine (
    address        jsonb not null primary key,
    schema_name    varchar,
    name           varchar,
    kind           varchar,
    status         varchar,
    created        timestamp,
    last_ddl_time  timestamp
);

insert into stage_ora_meta_routine
select
    n.address,
    n.address->>'schema',
    o.name,
    case o.type_id
        when 7 then 'PROCEDURE' when 8 then 'FUNCTION'
        when 9 then 'PACKAGE' when 13 then 'TYPE'
    end,
    case o.status when 1 then 'VALID' when 0 then 'N/A' else 'INVALID' end,
    o.created,
    o.last_ddl_time
from
    stage_node n
    join raw_objects o on o.obj_id = n.obj_id
where
    n.kind = 'rtn';
