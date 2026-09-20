-- канонический отпечаток одного источника (host = %(host)s), без host, database, id
with n as (select id, surface, (address - 'host' - 'database' - 'port' - 'scheme')::text as a from {schema}.node where address->>'host' = %(host)s),
lines as (
    select 'N|' || surface || '|' || a as l from n
    union all select 'T|' || n.a || '|' || coalesce(p.a, '') from {schema}.tree t join n on n.id = t.node_id left join n p on p.id = t.parent_id
    union all select 'E|' || s.a || '|' || d.a || '|' || e.surface from {schema}.edge e join n s on s.id = e.node_src_id join n d on d.id = e.node_tgt_id
    union all select 'M|' || s.a || '|' || d.a || '|' || m.role || '|' || m.side || '|' || m.ordinal || '|' || m.is_key from {schema}.pg_meta_edge m join {schema}.edge e on e.id = m.edge_id join n s on s.id = e.node_src_id join n d on d.id = e.node_tgt_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_database x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_schema x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_table x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_column x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_view x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_index x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_sequence x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_routine x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_constraint x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_trigger x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_type x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.pg_meta_statistics x join n on n.id = x.node_id
)
select count(*) || ' ' || md5(string_agg(l, E'\n' order by l)) from lines;
