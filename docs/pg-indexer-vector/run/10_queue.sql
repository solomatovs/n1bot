/*
pg-indexer-vector, шаг 1: очередь на расчёт. Строки аспектов, у которых нет эмбеддинга
или content в таблице отличается от вычисленного, в порядке ключа, пачкой %(batch)s. Каждая
выданная строка захвачена сессионным advisory-замком (ключ: хэш 'pg_emb' и node_id),
чтобы второй воркер не считал её одновременно; замки снимает 90_unlock.sql после записи
или обрыв сессии. Строки, занятые другим воркером, пропускаются.
Определение аспектов (CTE obj и aspect) продублировано в каждом файле пакета и в других
пакетах индексаторов намеренно: общих объектов в базе нет, общая функция появится при
переносе на Python.
*/
-- @name queue
-- @params batch
with col as (
    select t.parent_id as rel_id,
           string_agg(c.name, ' ' order by c.ordinal) as names,
           string_agg(c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal) as typed
    from ix.pg_column c
    join ix.tree t on t.node_id = c.node_id
    group by t.parent_id
),
obj as (
    select x.node_id, 'pg_database'::ix.surface_e as surface, x.name, null::varchar as schema_name, x.name as path,
           'Database ' || x.name as head, x.comment, null::varchar as columns, null::varchar as typed
    from ix.pg_database x
    union all
    select x.node_id, 'pg_schema', x.name, x.name, x.name, 'Schema ' || x.name, x.comment, null, null
    from ix.pg_schema x
    union all
    select x.node_id, 'pg_table', x.name, x.schema_name, x.schema_name || '.' || x.name,
           case x.kind when 'partition' then 'Partition ' when 'partitioned' then 'Partitioned table ' when 'foreign' then 'Foreign table ' else 'Table ' end || x.schema_name || '.' || x.name,
           x.comment, col.names, col.typed
    from ix.pg_table x left join col on col.rel_id = x.node_id
    union all
    select x.node_id, 'pg_column', x.name, x.schema_name, x.schema_name || '.' || x.relation_name || '.' || x.name,
           'Column ' || x.schema_name || '.' || x.relation_name || '.' || x.name || ' ' || x.data_type, x.comment, null, null
    from ix.pg_column x
    union all
    select x.node_id, 'pg_view', x.name, x.schema_name, x.schema_name || '.' || x.name,
           case x.kind when 'matview' then 'Materialized view ' else 'View ' end || x.schema_name || '.' || x.name,
           x.comment, col.names, col.typed
    from ix.pg_view x left join col on col.rel_id = x.node_id
    union all
    select x.node_id, 'pg_index', x.name, x.schema_name, x.schema_name || '.' || x.name,
           'Index ' || x.name || ' on ' || x.table_name || ' (' || array_to_string(x.columns, ', ') || ')' || case when x.is_unique then ' unique' else '' end,
           x.comment, null, null
    from ix.pg_index x
    union all
    select x.node_id, 'pg_sequence', x.name, x.schema_name, x.schema_name || '.' || x.name, 'Sequence ' || x.schema_name || '.' || x.name, x.comment, null, null
    from ix.pg_sequence x
    union all
    select x.node_id, 'pg_routine', x.name, x.schema_name, x.schema_name || '.' || x.name || '(' || coalesce(x.identity_args, '') || ')',
           initcap(x.kind) || ' ' || x.name || '(' || coalesce(x.identity_args, '') || ')' || coalesce(' returns ' || x.result_type, ''), x.comment, null, null
    from ix.pg_routine x
    union all
    select x.node_id, 'pg_constraint', x.name, x.schema_name, x.schema_name || '.' || x.table_name || '.' || x.name,
           initcap(x.kind) || ' ' || x.name || ' on ' || coalesce(x.table_name, '') || ': ' || x.definition, x.comment, null, null
    from ix.pg_constraint x
    union all
    select x.node_id, 'pg_trigger', x.name, x.schema_name, x.schema_name || '.' || x.table_name || '.' || x.name,
           'Trigger ' || x.name || ' on ' || x.table_name || ' ' || x.timing || ' ' || array_to_string(x.events, ', '), x.comment, null, null
    from ix.pg_trigger x
    union all
    select x.node_id, 'pg_type', x.name, x.schema_name, x.schema_name || '.' || x.name,
           initcap(x.kind) || ' ' || x.schema_name || '.' || x.name || coalesce(' ' || x.base_type, '') || coalesce(' (' || array_to_string(x.enum_labels, ', ') || ')', ''), x.comment, null, null
    from ix.pg_type x
    union all
    select x.node_id, 'pg_statistics', x.name, x.schema_name, x.schema_name || '.' || x.name, 'Statistics ' || x.name || ' on ' || x.table_name, x.comment, null, null
    from ix.pg_statistics x
),
aspect as (
    select o.node_id, o.surface, 'description'::ix.pg_aspect_e as aspect,
           o.head || coalesce(': ' || o.comment, '') || coalesce('. Columns: ' || o.typed, '') as content
    from obj o
    union all
    select o.node_id, o.surface, 'comment', o.comment
    from obj o where o.comment is not null and o.comment <> ''
    union all
    select o.node_id, o.surface, 'columns', o.columns
    from obj o where o.columns is not null and o.columns <> ''
),
todo as (
    select a.node_id, a.surface, a.aspect, a.content
    from aspect a
    left join ix.pg_emb_e5_1024 e on e.node_id = a.node_id and e.surface = a.surface and e.aspect = a.aspect
    where e.node_id is null or e.content is distinct from a.content
    order by a.node_id, a.surface, a.aspect
    limit %(batch)s * 4
)
select node_id, surface, aspect, content
from todo
where pg_try_advisory_lock(hashtextextended('pg_emb', node_id))
limit %(batch)s;
