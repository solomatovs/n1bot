-- инварианты структуры ix для источников ClickHouse; каждая строка должна дать 0
with ch_node as (select * from {schema}.node where address->>'scheme' = 'clickhouse'),
surface_rows as (
    select node_id, 'ch_meta_server' s from {schema}.ch_meta_server union all select node_id, 'ch_meta_database' from {schema}.ch_meta_database
    union all select node_id, 'ch_meta_table' from {schema}.ch_meta_table union all select node_id, 'ch_meta_view' from {schema}.ch_meta_view
    union all select node_id, 'ch_meta_column' from {schema}.ch_meta_column union all select node_id, 'ch_meta_index' from {schema}.ch_meta_index
    union all select node_id, 'ch_meta_projection' from {schema}.ch_meta_projection union all select node_id, 'ch_meta_dictionary' from {schema}.ch_meta_dictionary
    union all select node_id, 'ch_meta_function' from {schema}.ch_meta_function
)
select 'node_without_tree' as chk, count(*) from ch_node n left join {schema}.tree t on t.node_id = n.id where t.id is null
union all select 'node_with_many_tree', count(*) from (select t.node_id from {schema}.tree t join ch_node n on n.id = t.node_id group by t.node_id having count(*) > 1) x
union all select 'root_not_server', count(*) from {schema}.tree t join ch_node n on n.id = t.node_id where t.parent_id is null and n.surface <> 'ch_meta_server'
union all select 'server_with_parent', count(*) from {schema}.tree t join ch_node n on n.id = t.node_id where t.parent_id is not null and n.surface = 'ch_meta_server'
union all select 'tree_cycle_or_broken', count(*) from (
    with recursive up as (select n.id, t.parent_id, 1 as depth from ch_node n join {schema}.tree t on t.node_id = n.id
                          union all select up.id, t.parent_id, up.depth + 1 from up join {schema}.tree t on t.node_id = up.parent_id where up.depth < 20)
    select id from up group by id having max(depth) >= 20 or bool_and(parent_id is not null)) x
union all select 'column_parent_kind', count(*) from {schema}.tree t join ch_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface = 'ch_meta_column' and p.surface not in ('ch_meta_table', 'ch_meta_view', 'ch_meta_dictionary')
union all select 'index_parent_kind', count(*) from {schema}.tree t join ch_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface in ('ch_meta_index', 'ch_meta_projection') and p.surface <> 'ch_meta_table'
union all select 'database_parent_kind', count(*) from {schema}.tree t join ch_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface in ('ch_meta_database', 'ch_meta_function') and p.surface <> 'ch_meta_server'
union all select 'edge_cross_source', count(*) from {schema}.edge e join ch_node s on s.id = e.node_src_id join {schema}.node d on d.id = e.node_tgt_id where s.address->>'host' <> d.address->>'host'
union all select 'edge_self', count(*) from {schema}.edge e join ch_node s on s.id = e.node_src_id where e.node_src_id = e.node_tgt_id
union all select 'edge_without_role', count(*) from {schema}.edge e join ch_node s on s.id = e.node_src_id where not exists (select 1 from {schema}.ch_meta_edge m where m.edge_id = e.id)
union all select 'key_edge_not_table_column', count(*) from {schema}.ch_meta_edge m join {schema}.edge e on e.id = m.edge_id join ch_node s on s.id = e.node_src_id join {schema}.node d on d.id = e.node_tgt_id
    where m.role in ('partition_key', 'sorting_key', 'primary_key', 'sampling_key') and (s.surface not in ('ch_meta_table', 'ch_meta_view') or d.surface <> 'ch_meta_column')
union all select 'surface_rows_ne_nodes', abs((select count(*) from ch_node) - (select count(*) from surface_rows r join ch_node n on n.id = r.node_id))
union all select 'surface_wrong_table', count(*) from surface_rows x join ch_node n on n.id = x.node_id where n.surface::text <> x.s
union all select 'node_without_surface', count(*) from ch_node n where not exists (select 1 from surface_rows s where s.node_id = n.id)
union all select 'column_name_ne_address', count(*) from {schema}.ch_meta_column c join ch_node n on n.id = c.node_id where c.name <> n.address->>'column'
union all select 'table_surface_ne_address', count(*) from {schema}.ch_meta_table c join ch_node n on n.id = c.node_id where c.name <> n.address->>'table' or c.database_name <> n.address->>'database'
union all select 'view_surface_ne_address', count(*) from {schema}.ch_meta_view c join ch_node n on n.id = c.node_id where c.name <> n.address->>'view' or c.database_name <> n.address->>'database'
union all select 'dictionary_surface_ne_address', count(*) from {schema}.ch_meta_dictionary c join ch_node n on n.id = c.node_id where c.name <> n.address->>'dictionary' or c.database_name <> n.address->>'database';
