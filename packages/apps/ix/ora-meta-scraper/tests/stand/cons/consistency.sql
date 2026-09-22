-- инварианты структуры ix для источников Oracle; каждая строка должна дать 0
with ora_node as (select * from {schema}.node where address->>'scheme' = 'oracle'),
surface_rows as (
    select node_id, 'ora_meta_database' s from {schema}.ora_meta_database union all select node_id, 'ora_meta_schema' from {schema}.ora_meta_schema
    union all select node_id, 'ora_meta_table' from {schema}.ora_meta_table union all select node_id, 'ora_meta_view' from {schema}.ora_meta_view
    union all select node_id, 'ora_meta_mview' from {schema}.ora_meta_mview union all select node_id, 'ora_meta_column' from {schema}.ora_meta_column
    union all select node_id, 'ora_meta_constraint' from {schema}.ora_meta_constraint union all select node_id, 'ora_meta_index' from {schema}.ora_meta_index
    union all select node_id, 'ora_meta_sequence' from {schema}.ora_meta_sequence union all select node_id, 'ora_meta_synonym' from {schema}.ora_meta_synonym
    union all select node_id, 'ora_meta_trigger' from {schema}.ora_meta_trigger union all select node_id, 'ora_meta_routine' from {schema}.ora_meta_routine
)
select 'node_without_tree' as chk, count(*) from ora_node n left join {schema}.tree t on t.node_id = n.id where t.id is null
union all select 'node_with_many_tree', count(*) from (select t.node_id from {schema}.tree t join ora_node n on n.id = t.node_id group by t.node_id having count(*) > 1) x
union all select 'root_not_database', count(*) from {schema}.tree t join ora_node n on n.id = t.node_id where t.parent_id is null and n.surface <> 'ora_meta_database'
union all select 'database_with_parent', count(*) from {schema}.tree t join ora_node n on n.id = t.node_id where t.parent_id is not null and n.surface = 'ora_meta_database'
union all select 'tree_cycle_or_broken', count(*) from (
    with recursive up as (select n.id, t.parent_id, 1 as depth from ora_node n join {schema}.tree t on t.node_id = n.id
                          union all select up.id, t.parent_id, up.depth + 1 from up join {schema}.tree t on t.node_id = up.parent_id where up.depth < 20)
    select id from up group by id having max(depth) >= 20 or bool_and(parent_id is not null)) x
union all select 'schema_parent_kind', count(*) from {schema}.tree t join ora_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface = 'ora_meta_schema' and p.surface <> 'ora_meta_database'
union all select 'object_parent_kind', count(*) from {schema}.tree t join ora_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface in ('ora_meta_table', 'ora_meta_view', 'ora_meta_mview', 'ora_meta_sequence', 'ora_meta_synonym', 'ora_meta_routine') and p.surface <> 'ora_meta_schema'
union all select 'column_parent_kind', count(*) from {schema}.tree t join ora_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface = 'ora_meta_column' and p.surface not in ('ora_meta_table', 'ora_meta_view', 'ora_meta_mview')
union all select 'index_parent_kind', count(*) from {schema}.tree t join ora_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface = 'ora_meta_index' and p.surface not in ('ora_meta_table', 'ora_meta_mview')
union all select 'constraint_parent_kind', count(*) from {schema}.tree t join ora_node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface in ('ora_meta_constraint', 'ora_meta_trigger') and p.surface not in ('ora_meta_table', 'ora_meta_view')
union all select 'edge_cross_source', count(*) from {schema}.edge e join ora_node s on s.id = e.node_src_id join {schema}.node d on d.id = e.node_tgt_id where s.address->>'host' <> d.address->>'host' or s.address->>'database' <> d.address->>'database'
union all select 'edge_self', count(*) from {schema}.edge e join ora_node s on s.id = e.node_src_id where e.node_src_id = e.node_tgt_id
union all select 'edge_without_role', count(*) from {schema}.edge e join ora_node s on s.id = e.node_src_id where not exists (select 1 from {schema}.ora_meta_edge m where m.edge_id = e.id)
union all select 'index_edge_kinds', count(*) from {schema}.ora_meta_edge m join {schema}.edge e on e.id = m.edge_id join ora_node s on s.id = e.node_src_id join {schema}.node d on d.id = e.node_tgt_id
    where m.role = 'index' and (s.surface <> 'ora_meta_index' or d.surface <> 'ora_meta_column')
union all select 'constraint_edge_kinds', count(*) from {schema}.ora_meta_edge m join {schema}.edge e on e.id = m.edge_id join ora_node s on s.id = e.node_src_id join {schema}.node d on d.id = e.node_tgt_id
    where m.role = 'constraint' and (s.surface <> 'ora_meta_constraint' or d.surface <> 'ora_meta_column')
union all select 'partition_edge_kinds', count(*) from {schema}.ora_meta_edge m join {schema}.edge e on e.id = m.edge_id join ora_node s on s.id = e.node_src_id join {schema}.node d on d.id = e.node_tgt_id
    where m.role = 'partition_key' and (s.surface <> 'ora_meta_table' or d.surface <> 'ora_meta_column')
union all select 'synonym_edge_kinds', count(*) from {schema}.ora_meta_edge m join {schema}.edge e on e.id = m.edge_id join ora_node s on s.id = e.node_src_id
    where m.role = 'synonym' and s.surface <> 'ora_meta_synonym'
union all select 'positional_without_ordinal', count(*) from {schema}.ora_meta_edge m join {schema}.edge e on e.id = m.edge_id join ora_node s on s.id = e.node_src_id where m.role in ('index', 'partition_key') and m.ordinal = 0
union all select 'surface_rows_ne_nodes', abs((select count(*) from ora_node) - (select count(*) from surface_rows r join ora_node n on n.id = r.node_id))
union all select 'surface_wrong_table', count(*) from surface_rows x join ora_node n on n.id = x.node_id where n.surface::text <> x.s
union all select 'node_without_surface', count(*) from ora_node n where not exists (select 1 from surface_rows s where s.node_id = n.id)
union all select 'column_name_ne_address', count(*) from {schema}.ora_meta_column c join ora_node n on n.id = c.node_id where c.name <> n.address->>'column'
union all select 'table_surface_ne_address', count(*) from {schema}.ora_meta_table c join ora_node n on n.id = c.node_id where c.name <> n.address->>'table' or c.schema_name <> n.address->>'schema'
union all select 'view_surface_ne_address', count(*) from {schema}.ora_meta_view c join ora_node n on n.id = c.node_id where c.name <> n.address->>'view' or c.schema_name <> n.address->>'schema'
union all select 'mview_surface_ne_address', count(*) from {schema}.ora_meta_mview c join ora_node n on n.id = c.node_id where c.name <> n.address->>'mview' or c.schema_name <> n.address->>'schema';
