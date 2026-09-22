-- канонический отпечаток одного источника (host = %(host)s), без host, port, scheme, database
-- (имя сервиса у каждой цели своё), id и того, что зависит от сервера, а не от набора:
-- версия, кодировка, имена контейнера и базы, даты создания и DDL, статистика строк.
-- Системные имена constraint'ов (SYS_C<n>) и последовательностей identity (ISEQ$$_<n>)
-- новые на каждое пересоздание набора, поэтому в отпечатке номер заменён меткой.
with n as (select id, surface, (address - 'host' - 'port' - 'scheme' - 'database')::text as a from {schema}.node where address->>'host' = %(host)s),
lines as (
    select 'N|' || surface || '|' || a as l from n
    union all select 'T|' || n.a || '|' || coalesce(p.a, '') from {schema}.tree t join n on n.id = t.node_id left join n p on p.id = t.parent_id
    union all select 'E|' || s.a || '|' || d.a || '|' || e.surface from {schema}.edge e join n s on s.id = e.node_src_id join n d on d.id = e.node_tgt_id
    union all select 'M|' || s.a || '|' || d.a || '|' || m.role || '|' || m.side || '|' || m.ordinal || '|' || m.is_key from {schema}.ora_meta_edge m join {schema}.edge e on e.id = m.edge_id join n s on s.id = e.node_src_id join n d on d.id = e.node_tgt_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'host' - 'port' - 'service' - 'con_name' - 'db_name' - 'version' - 'charset')::text from {schema}.ora_meta_database x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'created')::text from {schema}.ora_meta_schema x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'created' - 'last_ddl_time' - 'num_rows')::text from {schema}.ora_meta_table x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'created' - 'last_ddl_time')::text from {schema}.ora_meta_view x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'created' - 'last_ddl_time')::text from {schema}.ora_meta_mview x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ora_meta_column x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ora_meta_constraint x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'created' - 'last_ddl_time')::text from {schema}.ora_meta_index x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ora_meta_sequence x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ora_meta_synonym x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id')::text from {schema}.ora_meta_trigger x join n on n.id = x.node_id
    union all select 'S|' || n.a || '|' || (to_jsonb(x) - 'node_id' - 'created' - 'last_ddl_time')::text from {schema}.ora_meta_routine x join n on n.id = x.node_id
),
canon as (
    select regexp_replace(regexp_replace(l, 'SYS_C\d+', 'SYS_C<n>', 'g'), 'ISEQ\$\$_\d+', 'ISEQ$$_<n>', 'g') as l from lines
)
select count(*) || ' ' || md5(string_agg(l, E'\n' order by l)) from canon;
