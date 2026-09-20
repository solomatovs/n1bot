/*
pg-llm-describer, шаг 1: очередь на описание. Для каждой таблицы и view из ix собирается текст
структуры, который уйдёт в модель: заголовок и комментарий, колонки с типом, not null,
default и комментарием, внешние ключи наружу и внутрь, индексы, для view список таблиц,
которые она читает, оценка числа строк. input_hash это md5 этого текста. В очередь попадают
объекты без описания, с другим input_hash или с другим indexer_hash (сменили модель или
промпт). Каждый выданный объект захвачен сессионным advisory-замком, чужие пропускаются.
*/
-- @name queue
-- @params batch indexer_hash
with rel as (
    select n.id as node_id, n.surface, t.schema_name, t.name, t.kind, t.comment, t.row_estimate, t.partition_bound
    from {schema}.pg_meta_table t join {schema}.node n on n.id = t.node_id
    union all
    select n.id, n.surface, v.schema_name, v.name, v.kind, v.comment, null, null
    from {schema}.pg_meta_view v join {schema}.node n on n.id = v.node_id
),
cols as (
    select tr.parent_id as rel_id,
           string_agg('  ' || c.name || ' ' || c.data_type
                      || case when c.not_null then ' not null' else '' end
                      || coalesce(' default ' || c.default_expr, '')
                      || coalesce(' -- ' || c.comment, ''), E'\n' order by c.ordinal) as text
    from {schema}.pg_meta_column c join {schema}.tree tr on tr.node_id = c.node_id
    group by tr.parent_id
),
fk_out as (
    select tr.parent_id as rel_id, string_agg('  ' || k.definition, E'\n' order by k.name) as text
    from {schema}.pg_meta_constraint k join {schema}.tree tr on tr.node_id = k.node_id
    where k.kind = 'foreign key'
    group by tr.parent_id
),
fk_in as (
    select ct.parent_id as rel_id,
           string_agg(distinct '  ' || k.schema_name || '.' || k.table_name || ': ' || k.definition, E'\n') as text
    from {schema}.pg_meta_edge m
    join {schema}.edge e on e.id = m.edge_id
    join {schema}.pg_meta_constraint k on k.node_id = e.node_src_id and k.kind = 'foreign key'
    join {schema}.tree ct on ct.node_id = e.node_tgt_id
    where m.role = 'constraint' and m.side = 1
    group by ct.parent_id
),
idx as (
    select tr.parent_id as rel_id,
           string_agg('  ' || i.name || ' (' || array_to_string(i.columns, ', ') || ')'
                      || case when i.is_unique then ' unique' else '' end
                      || coalesce(' where ' || i.predicate, ''), E'\n' order by i.name) as text
    from {schema}.pg_meta_index i join {schema}.tree tr on tr.node_id = i.node_id
    group by tr.parent_id
),
reads as (
    select e.node_src_id as rel_id, string_agg(distinct '  ' || (p.address->>'schema') || '.' || coalesce(p.address->>'table', p.address->>'view'), E'\n') as text
    from {schema}.edge e
    join {schema}.node v on v.id = e.node_src_id and v.surface = 'pg_meta_view'
    join {schema}.node c on c.id = e.node_tgt_id
    join {schema}.tree ct on ct.node_id = c.id
    join {schema}.node p on p.id = ct.parent_id
    group by e.node_src_id
),
input as (
    select r.node_id, r.surface,
           case when r.surface = 'pg_meta_view' then initcap(r.kind) else initcap(r.kind) end || ' ' || r.schema_name || '.' || r.name
           || coalesce(E'\nComment: ' || r.comment, '')
           || case when r.row_estimate >= 0 then E'\nRows (estimate): ' || r.row_estimate::bigint else '' end
           || coalesce(E'\nPartition: ' || r.partition_bound, '')
           || coalesce(E'\nColumns:\n' || cols.text, '')
           || coalesce(E'\nForeign keys:\n' || fk_out.text, '')
           || coalesce(E'\nReferenced by:\n' || fk_in.text, '')
           || coalesce(E'\nIndexes:\n' || idx.text, '')
           || coalesce(E'\nReads:\n' || reads.text, '') as text
    from rel r
    left join cols on cols.rel_id = r.node_id
    left join fk_out on fk_out.rel_id = r.node_id
    left join fk_in on fk_in.rel_id = r.node_id
    left join idx on idx.rel_id = r.node_id
    left join reads on reads.rel_id = r.node_id
),
todo as (
    select i.node_id, i.surface, i.text, md5(i.text) as input_hash
    from input i
    left join {schema}.pg_llm_description s on s.node_id = i.node_id
    where s.node_id is null or s.input_hash <> md5(i.text) or s.indexer_hash <> %(indexer_hash)s
    order by i.node_id
    limit %(batch)s * 4
)
select node_id, surface, text, input_hash
from todo
where pg_try_advisory_lock(hashtextextended('pg_llm_description', node_id))
limit %(batch)s;
