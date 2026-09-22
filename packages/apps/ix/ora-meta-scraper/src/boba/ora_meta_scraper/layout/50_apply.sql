-- Согласование ix со стадией: всё или ничего. Правила те же, что у pg-meta-scraper
-- (layout/50_apply.sql там): транзакция repeatable read открыта клиентом, сначала замок
-- for update на весь срез ix по scope источника в порядке node -> tree -> edge ->
-- ora_meta_edge -> surface-таблицы по id, затем на каждую таблицу два statement'а: удалить
-- лишние и изменившиеся строки, вставить недостающие. Промежуточные множества это CTE,
-- temp-таблиц три: apply_log, scope_node и node_map. Сводка planned/applied внутри
-- удавшейся транзакции равна всегда; неравенство это ошибка пакета.
-- Scope у Oracle это сервис: scheme, host, port, database.
-- Файл собран из schema/30_ora_meta_surfaces.sql: список колонок surface там.

create temp table apply_log (op text, planned bigint, applied bigint);

create temp table scope_node as
select
    n.id
from
    {schema}.node n, raw_source s
where
    n.address @> jsonb_build_object(
        'scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database
    );

-- 1. Замок на срез
select count(*) as locked_node
from (
    select
        n.id
    from
        {schema}.node n
        join scope_node sc on sc.id = n.id
    order by
        n.id
    for update of n
) l;

select count(*) as locked_tree
from (
    select
        t.id
    from
        {schema}.tree t
        join scope_node sc on sc.id = t.node_id
    order by
        t.id
    for update of t
) l;

select count(*) as locked_edge
from (
    select
        e.id
    from
        {schema}.edge e
    where
        exists (
            select 1
            from   scope_node sc
            where  sc.id = e.node_src_id or sc.id = e.node_tgt_id
        )
    order by
        e.id
    for update of e
) l;

select count(*) as locked_ora_meta_edge
from (
    select
        m.edge_id,
        m.role,
        m.side,
        m.ordinal
    from
        {schema}.ora_meta_edge m
        join {schema}.edge e on e.id = m.edge_id
    where
        exists (
            select 1
            from   scope_node sc
            where  sc.id = e.node_src_id or sc.id = e.node_tgt_id
        )
    order by
        m.edge_id, m.role, m.side, m.ordinal
    for update of m
) l;

select count(*) as locked_ora_meta_database
from (
    select
        x.node_id
    from
        {schema}.ora_meta_database x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_schema
from (
    select
        x.node_id
    from
        {schema}.ora_meta_schema x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_table
from (
    select
        x.node_id
    from
        {schema}.ora_meta_table x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_view
from (
    select
        x.node_id
    from
        {schema}.ora_meta_view x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_mview
from (
    select
        x.node_id
    from
        {schema}.ora_meta_mview x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_column
from (
    select
        x.node_id
    from
        {schema}.ora_meta_column x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_constraint
from (
    select
        x.node_id
    from
        {schema}.ora_meta_constraint x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_index
from (
    select
        x.node_id
    from
        {schema}.ora_meta_index x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_sequence
from (
    select
        x.node_id
    from
        {schema}.ora_meta_sequence x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_synonym
from (
    select
        x.node_id
    from
        {schema}.ora_meta_synonym x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_trigger
from (
    select
        x.node_id
    from
        {schema}.ora_meta_trigger x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ora_meta_routine
from (
    select
        x.node_id
    from
        {schema}.ora_meta_routine x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

-- 2. node
with cur as (
    select
        n.id,
        n.surface,
        n.address
    from
        {schema}.node n
        join scope_node sc on sc.id = n.id
),
del as (
    select
        c.id
    from
        cur c
        left join stage_node s on s.address = c.address
    where
        s.address is null or s.surface <> c.surface
),
done as (
    delete from {schema}.node x
    using
        del d
    where
        x.id = d.id
    returning x.id
)
insert into apply_log
select
    'node_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        n.surface,
        n.address
    from
        {schema}.node n
        join scope_node sc on sc.id = n.id
),
ins as (
    select
        s.surface,
        s.address
    from
        stage_node s
        left join cur c on c.address = s.address
    where
        c.address is null
),
done as (
    insert into {schema}.node
        (surface, address)
    select
        surface, address
    from
        ins
    on conflict (address) do nothing
    returning id
)
insert into apply_log
select
    'node_insert',
    (select count(*) from ins),
    (select count(*) from done);

-- scope после вставки: все node источника, старые и новые
truncate scope_node;
insert into scope_node
select
    n.id
from
    {schema}.node n, raw_source s
where
    n.address @> jsonb_build_object(
        'scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database
    );

create temp table node_map as
select s.kind, s.obj_id, s.sub_id, s.address, s.surface, n.id
from stage_node s
join {schema}.node n on n.address = s.address;

-- 3. tree
with cur as (
    select
        t.id,
        t.node_id,
        t.parent_id
    from
        {schema}.tree t
        join scope_node sc on sc.id = t.node_id
),
want as (
    select
        n.id as node_id,
        p.id as parent_id
    from
        stage_tree s
        join node_map n on n.address = s.node_address
        left join node_map p on p.address = s.parent_address
),
del as (
    select
        c.id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and w.parent_id is not distinct from c.parent_id
    where
        w.node_id is null
),
done as (
    delete from {schema}.tree x
    using
        del d
    where
        x.id = d.id
    returning x.id
)
insert into apply_log
select
    'tree_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        t.id,
        t.node_id,
        t.parent_id
    from
        {schema}.tree t
        join scope_node sc on sc.id = t.node_id
),
want as (
    select
        n.id as node_id,
        p.id as parent_id
    from
        stage_tree s
        join node_map n on n.address = s.node_address
        left join node_map p on p.address = s.parent_address
),
ins as (
    select
        w.node_id,
        w.parent_id
    from
        want w
        left join cur c on c.node_id = w.node_id
            and c.parent_id is not distinct from w.parent_id
    where
        c.id is null
),
done as (
    insert into {schema}.tree
        (node_id, parent_id)
    select
        node_id, parent_id
    from
        ins
    on conflict (node_id, parent_id) do nothing
    returning id
)
insert into apply_log
select
    'tree_insert',
    (select count(*) from ins),
    (select count(*) from done);

-- 4. edge: пары src, tgt
with cur as (
    select
        e.id,
        e.node_src_id,
        e.node_tgt_id
    from
        {schema}.edge e
        join scope_node sc on sc.id = e.node_src_id
),
want as (
    select
        distinct sn.id as node_src_id,
        tn.id as node_tgt_id
    from
        stage_edge s
        join node_map sn on sn.address = s.src_address
        join node_map tn on tn.address = s.tgt_address
),
del as (
    select
        c.id
    from
        cur c
        left join want w on w.node_src_id = c.node_src_id and w.node_tgt_id = c.node_tgt_id
    where
        w.node_src_id is null
),
done as (
    delete from {schema}.edge x
    using
        del d
    where
        x.id = d.id
    returning x.id
)
insert into apply_log
select
    'edge_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        e.id,
        e.node_src_id,
        e.node_tgt_id
    from
        {schema}.edge e
        join scope_node sc on sc.id = e.node_src_id
),
want as (
    select
        distinct sn.id as node_src_id,
        tn.id as node_tgt_id,
        sn.surface
    from
        stage_edge s
        join node_map sn on sn.address = s.src_address
        join node_map tn on tn.address = s.tgt_address
),
ins as (
    select
        w.node_src_id,
        w.node_tgt_id,
        w.surface
    from
        want w
        left join cur c on c.node_src_id = w.node_src_id and c.node_tgt_id = w.node_tgt_id
    where
        c.id is null
),
done as (
    insert into {schema}.edge
        (node_src_id, node_tgt_id, surface, weight)
    select
        node_src_id, node_tgt_id, surface, 1.0
    from
        ins
    on conflict (node_src_id, node_tgt_id) do nothing
    returning id
)
insert into apply_log
select
    'edge_insert',
    (select count(*) from ins),
    (select count(*) from done);

-- 5. ora_meta_edge: роли и позиции рёбер
with cur as (
    select
        m.edge_id,
        m.role::text as role,
        m.side,
        m.ordinal,
        m.is_key
    from
        {schema}.ora_meta_edge m
        join {schema}.edge e on e.id = m.edge_id
        join scope_node sc on sc.id = e.node_src_id
),
want as (
    select
        distinct e.id as edge_id,
        s.role,
        s.side,
        s.ordinal,
        s.is_key
    from
        stage_edge s
        join node_map sn on sn.address = s.src_address
        join node_map tn on tn.address = s.tgt_address
        join {schema}.edge e on e.node_src_id = sn.id and e.node_tgt_id = tn.id
),
del as (
    select
        c.edge_id,
        c.role,
        c.side,
        c.ordinal
    from
        cur c
        left join want w on w.edge_id = c.edge_id
            and w.role = c.role
                and w.side = c.side
                and w.ordinal = c.ordinal
                and w.is_key = c.is_key
    where
        w.edge_id is null
),
done as (
    delete from {schema}.ora_meta_edge x
    using
        del d
    where
        x.edge_id = d.edge_id
            and x.role = d.role::{schema}.ora_meta_edge_role_e
            and x.side = d.side
            and x.ordinal = d.ordinal
    returning x.edge_id
)
insert into apply_log
select
    'ora_meta_edge_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        m.edge_id,
        m.role::text as role,
        m.side,
        m.ordinal,
        m.is_key
    from
        {schema}.ora_meta_edge m
        join {schema}.edge e on e.id = m.edge_id
        join scope_node sc on sc.id = e.node_src_id
),
want as (
    select
        distinct e.id as edge_id,
        s.role,
        s.side,
        s.ordinal,
        s.is_key
    from
        stage_edge s
        join node_map sn on sn.address = s.src_address
        join node_map tn on tn.address = s.tgt_address
        join {schema}.edge e on e.node_src_id = sn.id and e.node_tgt_id = tn.id
),
ins as (
    select
        w.edge_id,
        w.role,
        w.side,
        w.ordinal,
        w.is_key
    from
        want w
        left join cur c on c.edge_id = w.edge_id
            and c.role = w.role
                and c.side = w.side
                and c.ordinal = w.ordinal
                and c.is_key = w.is_key
    where
        c.edge_id is null
),
done as (
    insert into {schema}.ora_meta_edge
        (edge_id, role, side, ordinal, is_key)
    select
        edge_id, role::{schema}.ora_meta_edge_role_e, side, ordinal, is_key
    from
        ins
    on conflict (edge_id, role, side, ordinal) do nothing
    returning edge_id
)
insert into apply_log
select
    'ora_meta_edge_insert',
    (select count(*) from ins),
    (select count(*) from done);

-- 6. surface node: строка целиком против строки целиком, ключ node_id

with cur as (
    select
        x.node_id,
        host,
        port,
        service,
        con_name,
        db_name,
        version,
        charset
    from
        {schema}.ora_meta_database x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.host,
        st.port,
        st.service,
        st.con_name,
        st.db_name,
        st.version,
        st.charset
    from
        stage_ora_meta_database st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.host, w.port, w.service, w.con_name, w.db_name, w.version, w.charset)
                is not distinct from
                row(c.host, c.port, c.service, c.con_name, c.db_name, c.version, c.charset)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_database x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_database_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        host,
        port,
        service,
        con_name,
        db_name,
        version,
        charset
    from
        {schema}.ora_meta_database x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.host,
        st.port,
        st.service,
        st.con_name,
        st.db_name,
        st.version,
        st.charset
    from
        stage_ora_meta_database st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.host, c.port, c.service, c.con_name, c.db_name, c.version, c.charset)
                is not distinct from
                row(w.host, w.port, w.service, w.con_name, w.db_name, w.version, w.charset)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_database
        (node_id, host, port, service, con_name, db_name, version, charset)
    select
        node_id, host, port, service, con_name, db_name, version, charset
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_database_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        name,
        created
    from
        {schema}.ora_meta_schema x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.name,
        st.created
    from
        stage_ora_meta_schema st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.name, w.created)
                is not distinct from
                row(c.name, c.created)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_schema x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_schema_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        name,
        created
    from
        {schema}.ora_meta_schema x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.name,
        st.created
    from
        stage_ora_meta_schema st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.name, c.created)
                is not distinct from
                row(w.name, w.created)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_schema
        (node_id, name, created)
    select
        node_id, name, created
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_schema_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        tablespace,
        partitioned,
        partition_type,
        temporary,
        iot,
        num_rows,
        comment,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_table x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.tablespace,
        st.partitioned,
        st.partition_type,
        st.temporary,
        st.iot,
        st.num_rows,
        st.comment,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_table st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.name, w.tablespace, w.partitioned, w.partition_type, w.temporary, w.iot, w.num_rows, w.comment, w.status, w.created, w.last_ddl_time)
                is not distinct from
                row(c.schema_name, c.name, c.tablespace, c.partitioned, c.partition_type, c.temporary, c.iot, c.num_rows, c.comment, c.status, c.created, c.last_ddl_time)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_table x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_table_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        tablespace,
        partitioned,
        partition_type,
        temporary,
        iot,
        num_rows,
        comment,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_table x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.tablespace,
        st.partitioned,
        st.partition_type,
        st.temporary,
        st.iot,
        st.num_rows,
        st.comment,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_table st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.name, c.tablespace, c.partitioned, c.partition_type, c.temporary, c.iot, c.num_rows, c.comment, c.status, c.created, c.last_ddl_time)
                is not distinct from
                row(w.schema_name, w.name, w.tablespace, w.partitioned, w.partition_type, w.temporary, w.iot, w.num_rows, w.comment, w.status, w.created, w.last_ddl_time)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_table
        (node_id, schema_name, name, tablespace, partitioned, partition_type, temporary, iot, num_rows, comment, status, created, last_ddl_time)
    select
        node_id, schema_name, name, tablespace, partitioned, partition_type, temporary, iot, num_rows, comment, status, created, last_ddl_time
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_table_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        text,
        comment,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_view x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.text,
        st.comment,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_view st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.name, w.text, w.comment, w.status, w.created, w.last_ddl_time)
                is not distinct from
                row(c.schema_name, c.name, c.text, c.comment, c.status, c.created, c.last_ddl_time)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_view x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_view_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        text,
        comment,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_view x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.text,
        st.comment,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_view st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.name, c.text, c.comment, c.status, c.created, c.last_ddl_time)
                is not distinct from
                row(w.schema_name, w.name, w.text, w.comment, w.status, w.created, w.last_ddl_time)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_view
        (node_id, schema_name, name, text, comment, status, created, last_ddl_time)
    select
        node_id, schema_name, name, text, comment, status, created, last_ddl_time
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_view_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        query,
        refresh_mode,
        comment,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_mview x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.query,
        st.refresh_mode,
        st.comment,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_mview st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.name, w.query, w.refresh_mode, w.comment, w.status, w.created, w.last_ddl_time)
                is not distinct from
                row(c.schema_name, c.name, c.query, c.refresh_mode, c.comment, c.status, c.created, c.last_ddl_time)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_mview x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_mview_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        query,
        refresh_mode,
        comment,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_mview x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.query,
        st.refresh_mode,
        st.comment,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_mview st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.name, c.query, c.refresh_mode, c.comment, c.status, c.created, c.last_ddl_time)
                is not distinct from
                row(w.schema_name, w.name, w.query, w.refresh_mode, w.comment, w.status, w.created, w.last_ddl_time)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_mview
        (node_id, schema_name, name, query, refresh_mode, comment, status, created, last_ddl_time)
    select
        node_id, schema_name, name, query, refresh_mode, comment, status, created, last_ddl_time
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_mview_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        relation_name,
        relation_kind,
        name,
        ordinal,
        data_type,
        data_length,
        data_precision,
        data_scale,
        nullable,
        default_text,
        virtual,
        identity,
        comment
    from
        {schema}.ora_meta_column x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.relation_name,
        st.relation_kind,
        st.name,
        st.ordinal,
        st.data_type,
        st.data_length,
        st.data_precision,
        st.data_scale,
        st.nullable,
        st.default_text,
        st.virtual,
        st.identity,
        st.comment
    from
        stage_ora_meta_column st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.relation_name, w.relation_kind, w.name, w.ordinal, w.data_type, w.data_length, w.data_precision, w.data_scale, w.nullable, w.default_text, w.virtual, w.identity, w.comment)
                is not distinct from
                row(c.schema_name, c.relation_name, c.relation_kind, c.name, c.ordinal, c.data_type, c.data_length, c.data_precision, c.data_scale, c.nullable, c.default_text, c.virtual, c.identity, c.comment)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_column x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_column_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        relation_name,
        relation_kind,
        name,
        ordinal,
        data_type,
        data_length,
        data_precision,
        data_scale,
        nullable,
        default_text,
        virtual,
        identity,
        comment
    from
        {schema}.ora_meta_column x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.relation_name,
        st.relation_kind,
        st.name,
        st.ordinal,
        st.data_type,
        st.data_length,
        st.data_precision,
        st.data_scale,
        st.nullable,
        st.default_text,
        st.virtual,
        st.identity,
        st.comment
    from
        stage_ora_meta_column st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.relation_name, c.relation_kind, c.name, c.ordinal, c.data_type, c.data_length, c.data_precision, c.data_scale, c.nullable, c.default_text, c.virtual, c.identity, c.comment)
                is not distinct from
                row(w.schema_name, w.relation_name, w.relation_kind, w.name, w.ordinal, w.data_type, w.data_length, w.data_precision, w.data_scale, w.nullable, w.default_text, w.virtual, w.identity, w.comment)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_column
        (node_id, schema_name, relation_name, relation_kind, name, ordinal, data_type, data_length, data_precision, data_scale, nullable, default_text, virtual, identity, comment)
    select
        node_id, schema_name, relation_name, relation_kind, name, ordinal, data_type, data_length, data_precision, data_scale, nullable, default_text, virtual, identity, comment
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_column_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        table_name,
        name,
        kind,
        search_condition,
        ref_schema,
        ref_constraint,
        delete_rule,
        enabled,
        validated,
        is_deferrable
    from
        {schema}.ora_meta_constraint x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.table_name,
        st.name,
        st.kind,
        st.search_condition,
        st.ref_schema,
        st.ref_constraint,
        st.delete_rule,
        st.enabled,
        st.validated,
        st.is_deferrable
    from
        stage_ora_meta_constraint st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.table_name, w.name, w.kind, w.search_condition, w.ref_schema, w.ref_constraint, w.delete_rule, w.enabled, w.validated, w.is_deferrable)
                is not distinct from
                row(c.schema_name, c.table_name, c.name, c.kind, c.search_condition, c.ref_schema, c.ref_constraint, c.delete_rule, c.enabled, c.validated, c.is_deferrable)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_constraint x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_constraint_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        table_name,
        name,
        kind,
        search_condition,
        ref_schema,
        ref_constraint,
        delete_rule,
        enabled,
        validated,
        is_deferrable
    from
        {schema}.ora_meta_constraint x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.table_name,
        st.name,
        st.kind,
        st.search_condition,
        st.ref_schema,
        st.ref_constraint,
        st.delete_rule,
        st.enabled,
        st.validated,
        st.is_deferrable
    from
        stage_ora_meta_constraint st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.table_name, c.name, c.kind, c.search_condition, c.ref_schema, c.ref_constraint, c.delete_rule, c.enabled, c.validated, c.is_deferrable)
                is not distinct from
                row(w.schema_name, w.table_name, w.name, w.kind, w.search_condition, w.ref_schema, w.ref_constraint, w.delete_rule, w.enabled, w.validated, w.is_deferrable)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_constraint
        (node_id, schema_name, table_name, name, kind, search_condition, ref_schema, ref_constraint, delete_rule, enabled, validated, is_deferrable)
    select
        node_id, schema_name, table_name, name, kind, search_condition, ref_schema, ref_constraint, delete_rule, enabled, validated, is_deferrable
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_constraint_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        table_name,
        name,
        index_type,
        is_unique,
        tablespace,
        columns,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_index x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.table_name,
        st.name,
        st.index_type,
        st.is_unique,
        st.tablespace,
        st.columns,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_index st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.table_name, w.name, w.index_type, w.is_unique, w.tablespace, w.columns, w.status, w.created, w.last_ddl_time)
                is not distinct from
                row(c.schema_name, c.table_name, c.name, c.index_type, c.is_unique, c.tablespace, c.columns, c.status, c.created, c.last_ddl_time)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_index x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_index_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        table_name,
        name,
        index_type,
        is_unique,
        tablespace,
        columns,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_index x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.table_name,
        st.name,
        st.index_type,
        st.is_unique,
        st.tablespace,
        st.columns,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_index st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.table_name, c.name, c.index_type, c.is_unique, c.tablespace, c.columns, c.status, c.created, c.last_ddl_time)
                is not distinct from
                row(w.schema_name, w.table_name, w.name, w.index_type, w.is_unique, w.tablespace, w.columns, w.status, w.created, w.last_ddl_time)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_index
        (node_id, schema_name, table_name, name, index_type, is_unique, tablespace, columns, status, created, last_ddl_time)
    select
        node_id, schema_name, table_name, name, index_type, is_unique, tablespace, columns, status, created, last_ddl_time
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_index_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        min_value,
        max_value,
        increment_by,
        cycle,
        ordered,
        cache_size
    from
        {schema}.ora_meta_sequence x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.min_value,
        st.max_value,
        st.increment_by,
        st.cycle,
        st.ordered,
        st.cache_size
    from
        stage_ora_meta_sequence st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.name, w.min_value, w.max_value, w.increment_by, w.cycle, w.ordered, w.cache_size)
                is not distinct from
                row(c.schema_name, c.name, c.min_value, c.max_value, c.increment_by, c.cycle, c.ordered, c.cache_size)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_sequence x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_sequence_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        min_value,
        max_value,
        increment_by,
        cycle,
        ordered,
        cache_size
    from
        {schema}.ora_meta_sequence x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.min_value,
        st.max_value,
        st.increment_by,
        st.cycle,
        st.ordered,
        st.cache_size
    from
        stage_ora_meta_sequence st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.name, c.min_value, c.max_value, c.increment_by, c.cycle, c.ordered, c.cache_size)
                is not distinct from
                row(w.schema_name, w.name, w.min_value, w.max_value, w.increment_by, w.cycle, w.ordered, w.cache_size)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_sequence
        (node_id, schema_name, name, min_value, max_value, increment_by, cycle, ordered, cache_size)
    select
        node_id, schema_name, name, min_value, max_value, increment_by, cycle, ordered, cache_size
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_sequence_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        target_schema,
        target_name,
        db_link
    from
        {schema}.ora_meta_synonym x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.target_schema,
        st.target_name,
        st.db_link
    from
        stage_ora_meta_synonym st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.name, w.target_schema, w.target_name, w.db_link)
                is not distinct from
                row(c.schema_name, c.name, c.target_schema, c.target_name, c.db_link)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_synonym x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_synonym_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        target_schema,
        target_name,
        db_link
    from
        {schema}.ora_meta_synonym x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.target_schema,
        st.target_name,
        st.db_link
    from
        stage_ora_meta_synonym st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.name, c.target_schema, c.target_name, c.db_link)
                is not distinct from
                row(w.schema_name, w.name, w.target_schema, w.target_name, w.db_link)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_synonym
        (node_id, schema_name, name, target_schema, target_name, db_link)
    select
        node_id, schema_name, name, target_schema, target_name, db_link
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_synonym_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        table_name,
        name,
        trigger_type,
        event,
        enabled,
        status
    from
        {schema}.ora_meta_trigger x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.table_name,
        st.name,
        st.trigger_type,
        st.event,
        st.enabled,
        st.status
    from
        stage_ora_meta_trigger st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.table_name, w.name, w.trigger_type, w.event, w.enabled, w.status)
                is not distinct from
                row(c.schema_name, c.table_name, c.name, c.trigger_type, c.event, c.enabled, c.status)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_trigger x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_trigger_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        table_name,
        name,
        trigger_type,
        event,
        enabled,
        status
    from
        {schema}.ora_meta_trigger x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.table_name,
        st.name,
        st.trigger_type,
        st.event,
        st.enabled,
        st.status
    from
        stage_ora_meta_trigger st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.table_name, c.name, c.trigger_type, c.event, c.enabled, c.status)
                is not distinct from
                row(w.schema_name, w.table_name, w.name, w.trigger_type, w.event, w.enabled, w.status)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_trigger
        (node_id, schema_name, table_name, name, trigger_type, event, enabled, status)
    select
        node_id, schema_name, table_name, name, trigger_type, event, enabled, status
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_trigger_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        kind,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_routine x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.kind,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_routine st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.schema_name, w.name, w.kind, w.status, w.created, w.last_ddl_time)
                is not distinct from
                row(c.schema_name, c.name, c.kind, c.status, c.created, c.last_ddl_time)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ora_meta_routine x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ora_meta_routine_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        schema_name,
        name,
        kind,
        status,
        created,
        last_ddl_time
    from
        {schema}.ora_meta_routine x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.schema_name,
        st.name,
        st.kind,
        st.status,
        st.created,
        st.last_ddl_time
    from
        stage_ora_meta_routine st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.schema_name, c.name, c.kind, c.status, c.created, c.last_ddl_time)
                is not distinct from
                row(w.schema_name, w.name, w.kind, w.status, w.created, w.last_ddl_time)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ora_meta_routine
        (node_id, schema_name, name, kind, status, created, last_ddl_time)
    select
        node_id, schema_name, name, kind, status, created, last_ddl_time
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ora_meta_routine_insert',
    (select count(*) from ins),
    (select count(*) from done);

-- 7. Сводка
select
    op, planned, applied
from
    apply_log
order by
    op;
