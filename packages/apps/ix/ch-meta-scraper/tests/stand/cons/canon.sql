-- канонический отпечаток одного источника (host = %(host)s), без host, port, scheme, id и
-- того, что зависит от сервера, а не от набора: uuid, origin словаря (это его uuid), modified_at, total_rows, total_bytes.
-- Внутренняя таблица материализованного представления называется .inner_id.<uuid>, uuid
-- новый на каждое пересоздание набора, поэтому в отпечатке он заменён меткой.
with n as (select id, surface, (address - 'host' - 'port' - 'scheme')::text as a from {schema}.node where address->>'host' = %(host)s),
lines as (
    select 'N|' || surface || '|' || a as l from n
    union all select 'T|' || n.a || '|' || coalesce(p.a, '') from {schema}.tree t join n on n.id = t.node_id left join n p on p.id = t.parent_id
    union all select 'E|' || s.a || '|' || d.a || '|' || e.surface from {schema}.edge e join n s on s.id = e.node_src_id join n d on d.id = e.node_tgt_id
    union all select 'M|' || s.a || '|' || d.a || '|' || m.role from {schema}.ch_meta_edge m join {schema}.edge e on e.id = m.edge_id join n s on s.id = e.node_src_id join n d on d.id = e.node_tgt_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'host' - 'port' - 'version')::text from {schema}.ch_meta_server x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'uuid')::text from {schema}.ch_meta_database x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'uuid' - 'modified_at' - 'total_rows' - 'total_bytes' - 'create_query')::text from {schema}.ch_meta_table x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'uuid' - 'modified_at' - 'create_query')::text from {schema}.ch_meta_view x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ch_meta_column x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ch_meta_index x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ch_meta_projection x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'uuid' - 'origin' - 'create_query')::text from {schema}.ch_meta_dictionary x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ch_meta_function x join n on n.id = x.node_id
)
,
canon as (select regexp_replace(l, '\.inner_id\.[0-9a-f-]{{36}}', '.inner_id.<uuid>', 'g') as l from lines)
select count(*) || ' ' || md5(string_agg(l, E'\n' order by l)) from canon;
