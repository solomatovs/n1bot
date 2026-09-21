-- Согласование ix со стадией: всё или ничего. Правила те же, что у pg-meta-scraper
-- (layout/50_apply.sql там): транзакция repeatable read открыта клиентом, сначала замок
-- for update на весь срез ix по scope источника в порядке node -> tree -> edge ->
-- ch_meta_edge -> surface-таблицы по id, затем на каждую таблицу два statement'а: удалить
-- лишние и изменившиеся строки, вставить недостающие. Промежуточные множества это CTE,
-- temp-таблиц три: apply_log, scope_node и node_map. Сводка planned/applied внутри
-- удавшейся транзакции равна всегда; неравенство это ошибка пакета.
-- Scope у ClickHouse это сервер: scheme, host, port.

create temp table apply_log (op text, planned bigint, applied bigint);

create temp table scope_node as
select
    n.id
from
    {schema}.node n, raw_source s
where
    n.address @> jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port);

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

select count(*) as locked_ch_meta_edge
from (
    select
        m.edge_id,
        m.role
    from
        {schema}.ch_meta_edge m
        join {schema}.edge e on e.id = m.edge_id
    where
        exists (
            select 1
            from   scope_node sc
            where  sc.id = e.node_src_id or sc.id = e.node_tgt_id
        )
    order by
        m.edge_id, m.role
    for update of m
) l;

select count(*) as locked_ch_meta_server
from (
    select
        x.node_id
    from
        {schema}.ch_meta_server x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_database
from (
    select
        x.node_id
    from
        {schema}.ch_meta_database x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_table
from (
    select
        x.node_id
    from
        {schema}.ch_meta_table x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_view
from (
    select
        x.node_id
    from
        {schema}.ch_meta_view x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_column
from (
    select
        x.node_id
    from
        {schema}.ch_meta_column x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_index
from (
    select
        x.node_id
    from
        {schema}.ch_meta_index x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_projection
from (
    select
        x.node_id
    from
        {schema}.ch_meta_projection x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_dictionary
from (
    select
        x.node_id
    from
        {schema}.ch_meta_dictionary x
        join scope_node sc on sc.id = x.node_id
    order by
        x.node_id
    for update of x
) l;

select count(*) as locked_ch_meta_function
from (
    select
        x.node_id
    from
        {schema}.ch_meta_function x
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
    n.address @> jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port);

create temp table node_map as
select s.kind, s.database, s.relation, s.name, s.address, s.surface, n.id
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

-- 5. ch_meta_edge: роли рёбер
with cur as (
    select
        m.edge_id,
        m.role::text as role
    from
        {schema}.ch_meta_edge m
        join {schema}.edge e on e.id = m.edge_id
        join scope_node sc on sc.id = e.node_src_id
),
want as (
    select
        distinct e.id as edge_id,
        s.role
    from
        stage_edge s
        join node_map sn on sn.address = s.src_address
        join node_map tn on tn.address = s.tgt_address
        join {schema}.edge e on e.node_src_id = sn.id and e.node_tgt_id = tn.id
),
del as (
    select
        c.edge_id,
        c.role
    from
        cur c
        left join want w on w.edge_id = c.edge_id and w.role = c.role
    where
        w.edge_id is null
),
done as (
    delete from {schema}.ch_meta_edge x
    using
        del d
    where
        x.edge_id = d.edge_id
        and x.role::text = d.role
    returning x.edge_id
)
insert into apply_log
select
    'ch_meta_edge_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        m.edge_id,
        m.role::text as role
    from
        {schema}.ch_meta_edge m
        join {schema}.edge e on e.id = m.edge_id
        join scope_node sc on sc.id = e.node_src_id
),
want as (
    select
        distinct e.id as edge_id,
        s.role
    from
        stage_edge s
        join node_map sn on sn.address = s.src_address
        join node_map tn on tn.address = s.tgt_address
        join {schema}.edge e on e.node_src_id = sn.id and e.node_tgt_id = tn.id
),
ins as (
    select
        w.edge_id,
        w.role
    from
        want w
        left join cur c on c.edge_id = w.edge_id and c.role = w.role
    where
        c.edge_id is null
),
done as (
    insert into {schema}.ch_meta_edge
        (edge_id, role)
    select
        edge_id, role::{schema}.ch_meta_edge_role_e
    from
        ins
    on conflict (edge_id, role) do nothing
    returning edge_id
)
insert into apply_log
select
    'ch_meta_edge_insert',
    (select count(*) from ins),
    (select count(*) from done);

-- 6. surface node: строка целиком против строки целиком, ключ node_id

with cur as (
    select
        x.node_id,
        host,
        port,
        version
    from
        {schema}.ch_meta_server x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.host,
        st.port,
        st.version
    from
        stage_ch_meta_server st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.host, w.port, w.version)
                is not distinct from
                row(c.host, c.port, c.version)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_server x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_server_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        host,
        port,
        version
    from
        {schema}.ch_meta_server x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.host,
        st.port,
        st.version
    from
        stage_ch_meta_server st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.host, c.port, c.version)
                is not distinct from
                row(w.host, w.port, w.version)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_server
        (node_id, host, port, version)
    select
        node_id, host, port, version
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_server_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        name,
        engine,
        engine_full,
        uuid,
        comment
    from
        {schema}.ch_meta_database x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.name,
        st.engine,
        st.engine_full,
        st.uuid,
        st.comment
    from
        stage_ch_meta_database st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.name, w.engine, w.engine_full, w.uuid, w.comment)
                is not distinct from
                row(c.name, c.engine, c.engine_full, c.uuid, c.comment)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_database x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_database_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        name,
        engine,
        engine_full,
        uuid,
        comment
    from
        {schema}.ch_meta_database x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.name,
        st.engine,
        st.engine_full,
        st.uuid,
        st.comment
    from
        stage_ch_meta_database st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.name, c.engine, c.engine_full, c.uuid, c.comment)
                is not distinct from
                row(w.name, w.engine, w.engine_full, w.uuid, w.comment)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_database
        (node_id, name, engine, engine_full, uuid, comment)
    select
        node_id, name, engine, engine_full, uuid, comment
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_database_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        name,
        uuid,
        engine,
        engine_full,
        partition_key,
        sorting_key,
        primary_key,
        sampling_key,
        storage_policy,
        total_rows,
        total_bytes,
        comment,
        create_query,
        modified_at
    from
        {schema}.ch_meta_table x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.name,
        st.uuid,
        st.engine,
        st.engine_full,
        st.partition_key,
        st.sorting_key,
        st.primary_key,
        st.sampling_key,
        st.storage_policy,
        st.total_rows,
        st.total_bytes,
        st.comment,
        st.create_query,
        st.modified_at
    from
        stage_ch_meta_table st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.database_name, w.name, w.uuid, w.engine, w.engine_full, w.partition_key, w.sorting_key, w.primary_key, w.sampling_key, w.storage_policy, w.total_rows, w.total_bytes, w.comment, w.create_query, w.modified_at)
                is not distinct from
                row(c.database_name, c.name, c.uuid, c.engine, c.engine_full, c.partition_key, c.sorting_key, c.primary_key, c.sampling_key, c.storage_policy, c.total_rows, c.total_bytes, c.comment, c.create_query, c.modified_at)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_table x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_table_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        name,
        uuid,
        engine,
        engine_full,
        partition_key,
        sorting_key,
        primary_key,
        sampling_key,
        storage_policy,
        total_rows,
        total_bytes,
        comment,
        create_query,
        modified_at
    from
        {schema}.ch_meta_table x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.name,
        st.uuid,
        st.engine,
        st.engine_full,
        st.partition_key,
        st.sorting_key,
        st.primary_key,
        st.sampling_key,
        st.storage_policy,
        st.total_rows,
        st.total_bytes,
        st.comment,
        st.create_query,
        st.modified_at
    from
        stage_ch_meta_table st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.database_name, c.name, c.uuid, c.engine, c.engine_full, c.partition_key, c.sorting_key, c.primary_key, c.sampling_key, c.storage_policy, c.total_rows, c.total_bytes, c.comment, c.create_query, c.modified_at)
                is not distinct from
                row(w.database_name, w.name, w.uuid, w.engine, w.engine_full, w.partition_key, w.sorting_key, w.primary_key, w.sampling_key, w.storage_policy, w.total_rows, w.total_bytes, w.comment, w.create_query, w.modified_at)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_table
        (node_id, database_name, name, uuid, engine, engine_full, partition_key, sorting_key, primary_key, sampling_key, storage_policy, total_rows, total_bytes, comment, create_query, modified_at)
    select
        node_id, database_name, name, uuid, engine, engine_full, partition_key, sorting_key, primary_key, sampling_key, storage_policy, total_rows, total_bytes, comment, create_query, modified_at
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_table_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        name,
        uuid,
        engine,
        kind,
        engine_full,
        as_select,
        partition_key,
        sorting_key,
        primary_key,
        target_database,
        target_table,
        comment,
        create_query,
        modified_at
    from
        {schema}.ch_meta_view x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.name,
        st.uuid,
        st.engine,
        st.kind,
        st.engine_full,
        st.as_select,
        st.partition_key,
        st.sorting_key,
        st.primary_key,
        st.target_database,
        st.target_table,
        st.comment,
        st.create_query,
        st.modified_at
    from
        stage_ch_meta_view st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.database_name, w.name, w.uuid, w.engine, w.kind, w.engine_full, w.as_select, w.partition_key, w.sorting_key, w.primary_key, w.target_database, w.target_table, w.comment, w.create_query, w.modified_at)
                is not distinct from
                row(c.database_name, c.name, c.uuid, c.engine, c.kind, c.engine_full, c.as_select, c.partition_key, c.sorting_key, c.primary_key, c.target_database, c.target_table, c.comment, c.create_query, c.modified_at)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_view x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_view_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        name,
        uuid,
        engine,
        kind,
        engine_full,
        as_select,
        partition_key,
        sorting_key,
        primary_key,
        target_database,
        target_table,
        comment,
        create_query,
        modified_at
    from
        {schema}.ch_meta_view x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.name,
        st.uuid,
        st.engine,
        st.kind,
        st.engine_full,
        st.as_select,
        st.partition_key,
        st.sorting_key,
        st.primary_key,
        st.target_database,
        st.target_table,
        st.comment,
        st.create_query,
        st.modified_at
    from
        stage_ch_meta_view st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.database_name, c.name, c.uuid, c.engine, c.kind, c.engine_full, c.as_select, c.partition_key, c.sorting_key, c.primary_key, c.target_database, c.target_table, c.comment, c.create_query, c.modified_at)
                is not distinct from
                row(w.database_name, w.name, w.uuid, w.engine, w.kind, w.engine_full, w.as_select, w.partition_key, w.sorting_key, w.primary_key, w.target_database, w.target_table, w.comment, w.create_query, w.modified_at)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_view
        (node_id, database_name, name, uuid, engine, kind, engine_full, as_select, partition_key, sorting_key, primary_key, target_database, target_table, comment, create_query, modified_at)
    select
        node_id, database_name, name, uuid, engine, kind, engine_full, as_select, partition_key, sorting_key, primary_key, target_database, target_table, comment, create_query, modified_at
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_view_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        relation_name,
        relation_kind,
        name,
        ordinal,
        data_type,
        default_kind,
        default_expression,
        codec,
        in_partition_key,
        in_sorting_key,
        in_primary_key,
        in_sampling_key,
        comment
    from
        {schema}.ch_meta_column x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.relation_name,
        st.relation_kind,
        st.name,
        st.ordinal,
        st.data_type,
        st.default_kind,
        st.default_expression,
        st.codec,
        st.in_partition_key,
        st.in_sorting_key,
        st.in_primary_key,
        st.in_sampling_key,
        st.comment
    from
        stage_ch_meta_column st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.database_name, w.relation_name, w.relation_kind, w.name, w.ordinal, w.data_type, w.default_kind, w.default_expression, w.codec, w.in_partition_key, w.in_sorting_key, w.in_primary_key, w.in_sampling_key, w.comment)
                is not distinct from
                row(c.database_name, c.relation_name, c.relation_kind, c.name, c.ordinal, c.data_type, c.default_kind, c.default_expression, c.codec, c.in_partition_key, c.in_sorting_key, c.in_primary_key, c.in_sampling_key, c.comment)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_column x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_column_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        relation_name,
        relation_kind,
        name,
        ordinal,
        data_type,
        default_kind,
        default_expression,
        codec,
        in_partition_key,
        in_sorting_key,
        in_primary_key,
        in_sampling_key,
        comment
    from
        {schema}.ch_meta_column x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.relation_name,
        st.relation_kind,
        st.name,
        st.ordinal,
        st.data_type,
        st.default_kind,
        st.default_expression,
        st.codec,
        st.in_partition_key,
        st.in_sorting_key,
        st.in_primary_key,
        st.in_sampling_key,
        st.comment
    from
        stage_ch_meta_column st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.database_name, c.relation_name, c.relation_kind, c.name, c.ordinal, c.data_type, c.default_kind, c.default_expression, c.codec, c.in_partition_key, c.in_sorting_key, c.in_primary_key, c.in_sampling_key, c.comment)
                is not distinct from
                row(w.database_name, w.relation_name, w.relation_kind, w.name, w.ordinal, w.data_type, w.default_kind, w.default_expression, w.codec, w.in_partition_key, w.in_sorting_key, w.in_primary_key, w.in_sampling_key, w.comment)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_column
        (node_id, database_name, relation_name, relation_kind, name, ordinal, data_type, default_kind, default_expression, codec, in_partition_key, in_sorting_key, in_primary_key, in_sampling_key, comment)
    select
        node_id, database_name, relation_name, relation_kind, name, ordinal, data_type, default_kind, default_expression, codec, in_partition_key, in_sorting_key, in_primary_key, in_sampling_key, comment
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_column_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        table_name,
        name,
        kind,
        kind_full,
        expr,
        granularity
    from
        {schema}.ch_meta_index x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.table_name,
        st.name,
        st.kind,
        st.kind_full,
        st.expr,
        st.granularity
    from
        stage_ch_meta_index st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.database_name, w.table_name, w.name, w.kind, w.kind_full, w.expr, w.granularity)
                is not distinct from
                row(c.database_name, c.table_name, c.name, c.kind, c.kind_full, c.expr, c.granularity)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_index x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_index_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        table_name,
        name,
        kind,
        kind_full,
        expr,
        granularity
    from
        {schema}.ch_meta_index x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.table_name,
        st.name,
        st.kind,
        st.kind_full,
        st.expr,
        st.granularity
    from
        stage_ch_meta_index st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.database_name, c.table_name, c.name, c.kind, c.kind_full, c.expr, c.granularity)
                is not distinct from
                row(w.database_name, w.table_name, w.name, w.kind, w.kind_full, w.expr, w.granularity)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_index
        (node_id, database_name, table_name, name, kind, kind_full, expr, granularity)
    select
        node_id, database_name, table_name, name, kind, kind_full, expr, granularity
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_index_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        table_name,
        name,
        kind,
        sorting_key,
        query
    from
        {schema}.ch_meta_projection x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.table_name,
        st.name,
        st.kind,
        st.sorting_key,
        st.query
    from
        stage_ch_meta_projection st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.database_name, w.table_name, w.name, w.kind, w.sorting_key, w.query)
                is not distinct from
                row(c.database_name, c.table_name, c.name, c.kind, c.sorting_key, c.query)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_projection x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_projection_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        table_name,
        name,
        kind,
        sorting_key,
        query
    from
        {schema}.ch_meta_projection x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.table_name,
        st.name,
        st.kind,
        st.sorting_key,
        st.query
    from
        stage_ch_meta_projection st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.database_name, c.table_name, c.name, c.kind, c.sorting_key, c.query)
                is not distinct from
                row(w.database_name, w.table_name, w.name, w.kind, w.sorting_key, w.query)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_projection
        (node_id, database_name, table_name, name, kind, sorting_key, query)
    select
        node_id, database_name, table_name, name, kind, sorting_key, query
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_projection_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        name,
        uuid,
        origin,
        layout,
        key_names,
        key_types,
        attribute_names,
        attribute_types,
        source,
        lifetime_min,
        lifetime_max,
        comment,
        create_query
    from
        {schema}.ch_meta_dictionary x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.name,
        st.uuid,
        st.origin,
        st.layout,
        st.key_names,
        st.key_types,
        st.attribute_names,
        st.attribute_types,
        st.source,
        st.lifetime_min,
        st.lifetime_max,
        st.comment,
        st.create_query
    from
        stage_ch_meta_dictionary st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.database_name, w.name, w.uuid, w.origin, w.layout, w.key_names, w.key_types, w.attribute_names, w.attribute_types, w.source, w.lifetime_min, w.lifetime_max, w.comment, w.create_query)
                is not distinct from
                row(c.database_name, c.name, c.uuid, c.origin, c.layout, c.key_names, c.key_types, c.attribute_names, c.attribute_types, c.source, c.lifetime_min, c.lifetime_max, c.comment, c.create_query)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_dictionary x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_dictionary_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        database_name,
        name,
        uuid,
        origin,
        layout,
        key_names,
        key_types,
        attribute_names,
        attribute_types,
        source,
        lifetime_min,
        lifetime_max,
        comment,
        create_query
    from
        {schema}.ch_meta_dictionary x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.database_name,
        st.name,
        st.uuid,
        st.origin,
        st.layout,
        st.key_names,
        st.key_types,
        st.attribute_names,
        st.attribute_types,
        st.source,
        st.lifetime_min,
        st.lifetime_max,
        st.comment,
        st.create_query
    from
        stage_ch_meta_dictionary st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.database_name, c.name, c.uuid, c.origin, c.layout, c.key_names, c.key_types, c.attribute_names, c.attribute_types, c.source, c.lifetime_min, c.lifetime_max, c.comment, c.create_query)
                is not distinct from
                row(w.database_name, w.name, w.uuid, w.origin, w.layout, w.key_names, w.key_types, w.attribute_names, w.attribute_types, w.source, w.lifetime_min, w.lifetime_max, w.comment, w.create_query)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_dictionary
        (node_id, database_name, name, uuid, origin, layout, key_names, key_types, attribute_names, attribute_types, source, lifetime_min, lifetime_max, comment, create_query)
    select
        node_id, database_name, name, uuid, origin, layout, key_names, key_types, attribute_names, attribute_types, source, lifetime_min, lifetime_max, comment, create_query
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_dictionary_insert',
    (select count(*) from ins),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        name,
        create_query
    from
        {schema}.ch_meta_function x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.name,
        st.create_query
    from
        stage_ch_meta_function st
        join node_map nm on nm.address = st.address
),
del as (
    select
        c.node_id
    from
        cur c
        left join want w on w.node_id = c.node_id
            and row(w.name, w.create_query)
                is not distinct from
                row(c.name, c.create_query)
    where
        w.node_id is null
),
done as (
    delete from {schema}.ch_meta_function x
    using
        del d
    where
        x.node_id = d.node_id
    returning x.node_id
)
insert into apply_log
select
    'ch_meta_function_delete',
    (select count(*) from del),
    (select count(*) from done);

with cur as (
    select
        x.node_id,
        name,
        create_query
    from
        {schema}.ch_meta_function x
        join scope_node sc on sc.id = x.node_id
),
want as (
    select
        nm.id as node_id,
        st.name,
        st.create_query
    from
        stage_ch_meta_function st
        join node_map nm on nm.address = st.address
),
ins as (
    select
        w.*
    from
        want w
        left join cur c on c.node_id = w.node_id
            and row(c.name, c.create_query)
                is not distinct from
                row(w.name, w.create_query)
    where
        c.node_id is null
),
done as (
    insert into {schema}.ch_meta_function
        (node_id, name, create_query)
    select
        node_id, name, create_query
    from
        ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log
select
    'ch_meta_function_insert',
    (select count(*) from ins),
    (select count(*) from done);

-- 7. Сводка
select op, planned, applied from apply_log;
