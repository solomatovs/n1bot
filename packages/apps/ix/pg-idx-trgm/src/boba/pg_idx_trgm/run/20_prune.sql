/*
pg-idx-trgm, шаг 2: удалить строки pg_trgm, для которых аспекта больше нет: node удалён
или сменил поверхность.
Определение аспектов (CTE obj и aspect) продублировано в каждом файле пакета и в других
пакетах индексаторов намеренно: общих объектов в базе нет, общая функция появится при
переносе на Python.
*/
-- @name prune
with col as (
    select
        t.parent_id                                        as rel_id,
        string_agg(c.name, ' ' order by c.ordinal)         as names,
        string_agg(
            c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
        )                                                  as typed
    from
        {schema}.pg_meta_column c
        join {schema}.tree t on t.node_id = c.node_id
    group by
        t.parent_id
),
obj as (
    select
        x.node_id,
        'pg_meta_database'::{schema}.surface_e              as surface,
        x.name,
        null::varchar                                      as schema_name,
        x.name                                             as path,
        'Database ' || x.name                              as head,
        x.comment,
        null::varchar                                      as columns,
        null::varchar                                      as typed
    from
        {schema}.pg_meta_database x
    union all
    select
        x.node_id,
        'pg_meta_schema',
        x.name,
        x.name,
        x.name,
        'Schema ' || x.name,
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_schema x
    union all
    select
        x.node_id,
        'pg_meta_table',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.name,
        case x.kind
            when 'partition'   then 'Partition '
            when 'partitioned' then 'Partitioned table '
            when 'foreign'     then 'Foreign table '
            else                    'Table '
        end || x.schema_name || '.' || x.name,
        x.comment,
        col.names,
        col.typed
    from
        {schema}.pg_meta_table x
        left join col on col.rel_id = x.node_id
    union all
    select
        x.node_id,
        'pg_meta_column',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.relation_name || '.' || x.name,
        'Column ' || x.schema_name || '.' || x.relation_name || '.' || x.name
            || ' ' || x.data_type,
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_column x
    union all
    select
        x.node_id,
        'pg_meta_view',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.name,
        case x.kind
            when 'matview' then 'Materialized view '
            else                'View '
        end || x.schema_name || '.' || x.name,
        x.comment,
        col.names,
        col.typed
    from
        {schema}.pg_meta_view x
        left join col on col.rel_id = x.node_id
    union all
    select
        x.node_id,
        'pg_meta_index',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.name,
        'Index ' || x.name || ' on ' || x.table_name
            || ' (' || array_to_string(x.columns, ', ') || ')'
            || case when x.is_unique then ' unique' else '' end,
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_index x
    union all
    select
        x.node_id,
        'pg_meta_sequence',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.name,
        'Sequence ' || x.schema_name || '.' || x.name,
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_sequence x
    union all
    select
        x.node_id,
        'pg_meta_routine',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.name
            || '(' || coalesce(x.identity_args, '') || ')',
        initcap(x.kind) || ' ' || x.name
            || '(' || coalesce(x.identity_args, '') || ')'
            || coalesce(' returns ' || x.result_type, ''),
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_routine x
    union all
    select
        x.node_id,
        'pg_meta_constraint',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.table_name || '.' || x.name,
        initcap(x.kind) || ' ' || x.name
            || ' on ' || coalesce(x.table_name, '') || ': ' || x.definition,
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_constraint x
    union all
    select
        x.node_id,
        'pg_meta_trigger',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.table_name || '.' || x.name,
        'Trigger ' || x.name || ' on ' || x.table_name
            || ' ' || x.timing || ' ' || array_to_string(x.events, ', '),
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_trigger x
    union all
    select
        x.node_id,
        'pg_meta_type',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.name,
        initcap(x.kind) || ' ' || x.schema_name || '.' || x.name
            || coalesce(' ' || x.base_type, '')
            || coalesce(' (' || array_to_string(x.enum_labels, ', ') || ')', ''),
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_type x
    union all
    select
        x.node_id,
        'pg_meta_statistics',
        x.name,
        x.schema_name,
        x.schema_name || '.' || x.name,
        'Statistics ' || x.name || ' on ' || x.table_name,
        x.comment,
        null,
        null
    from
        {schema}.pg_meta_statistics x
),
aspect as (
    select o.node_id, o.surface, 'meta_name'::{schema}.pg_idx_aspect_e as aspect, o.name::varchar as content from obj o
    union all
    select o.node_id, o.surface, 'meta_words', lower(replace(regexp_replace(regexp_replace(o.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'), '[_\-]+', ' ', 'g'), 'ё', 'е')) from obj o
    union all
    select o.node_id, o.surface, 'meta_path', o.path from obj o
    where o.surface in ('pg_meta_table', 'pg_meta_column', 'pg_meta_view', 'pg_meta_index', 'pg_meta_sequence', 'pg_meta_routine')
),
stale as (
    select f.node_id, f.surface, f.aspect
    from {schema}.pg_idx_trgm f
    where not exists (select 1 from aspect a where a.node_id = f.node_id and a.surface = f.surface and a.aspect = f.aspect)
    order by f.node_id, f.surface, f.aspect
    for update skip locked
),
done as (
    delete from {schema}.pg_idx_trgm f using stale s
    where f.node_id = s.node_id and f.surface = s.surface and f.aspect = s.aspect
    returning 1
)
select 'prune' as op, (select count(*) from done) as deleted;
