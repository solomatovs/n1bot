-- инварианты структуры ix; каждая строка должна дать 0
select 'node_without_tree' as chk, count(*) from {schema}.node n left join {schema}.tree t on t.node_id = n.id where t.id is null
union all select 'node_with_many_tree', count(*) from (select node_id from {schema}.tree group by node_id having count(*) > 1) x
union all select 'root_not_database', count(*) from {schema}.tree t join {schema}.node n on n.id = t.node_id where t.parent_id is null and n.surface <> 'pg_meta_database'
union all select 'database_with_parent', count(*) from {schema}.tree t join {schema}.node n on n.id = t.node_id where t.parent_id is not null and n.surface = 'pg_meta_database'
union all select 'tree_cycle_or_broken', count(*) from (
    with recursive up as (select n.id, t.parent_id, 1 as depth from {schema}.node n join {schema}.tree t on t.node_id = n.id
                          union all select up.id, t.parent_id, up.depth + 1 from up join {schema}.tree t on t.node_id = up.parent_id where up.depth < 20)
    select id from up group by id having max(depth) >= 20 or bool_and(parent_id is not null)) x
union all select 'column_parent_kind', count(*) from {schema}.tree t join {schema}.node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface = 'pg_meta_column' and p.surface not in ('pg_meta_table', 'pg_meta_view')
union all select 'index_parent_kind', count(*) from {schema}.tree t join {schema}.node c on c.id = t.node_id join {schema}.node p on p.id = t.parent_id where c.surface = 'pg_meta_index' and p.surface <> 'pg_meta_table'
union all select 'edge_cross_source', count(*) from {schema}.edge e join {schema}.node s on s.id = e.node_src_id join {schema}.node d on d.id = e.node_tgt_id where s.address->>'host' <> d.address->>'host'
union all select 'edge_self', count(*) from {schema}.edge where node_src_id = node_tgt_id
union all select 'pg_edge_side_not_fk', count(*) from {schema}.pg_meta_edge m join {schema}.edge e on e.id = m.edge_id join {schema}.node s on s.id = e.node_src_id where m.side = 1 and s.surface <> 'pg_meta_constraint'
union all select 'surface_rows_ne_nodes', abs((select count(*) from {schema}.node) - (
    (select count(*) from {schema}.pg_meta_database) + (select count(*) from {schema}.pg_meta_schema) + (select count(*) from {schema}.pg_meta_table) + (select count(*) from {schema}.pg_meta_column)
  + (select count(*) from {schema}.pg_meta_view) + (select count(*) from {schema}.pg_meta_index) + (select count(*) from {schema}.pg_meta_sequence) + (select count(*) from {schema}.pg_meta_routine)
  + (select count(*) from {schema}.pg_meta_constraint) + (select count(*) from {schema}.pg_meta_trigger) + (select count(*) from {schema}.pg_meta_type) + (select count(*) from {schema}.pg_meta_statistics)))
union all select 'surface_wrong_table', count(*) from (
    select node_id, 'pg_meta_database' s from {schema}.pg_meta_database union all select node_id, 'pg_meta_schema' from {schema}.pg_meta_schema union all select node_id, 'pg_meta_table' from {schema}.pg_meta_table
    union all select node_id, 'pg_meta_column' from {schema}.pg_meta_column union all select node_id, 'pg_meta_view' from {schema}.pg_meta_view union all select node_id, 'pg_meta_index' from {schema}.pg_meta_index
    union all select node_id, 'pg_meta_sequence' from {schema}.pg_meta_sequence union all select node_id, 'pg_meta_routine' from {schema}.pg_meta_routine union all select node_id, 'pg_meta_constraint' from {schema}.pg_meta_constraint
    union all select node_id, 'pg_meta_trigger' from {schema}.pg_meta_trigger union all select node_id, 'pg_meta_type' from {schema}.pg_meta_type union all select node_id, 'pg_meta_statistics' from {schema}.pg_meta_statistics) x
    join {schema}.node n on n.id = x.node_id where n.surface::text <> x.s
union all select 'node_without_surface', count(*) from {schema}.node n where not exists (
    select 1 from (select node_id from {schema}.pg_meta_database union all select node_id from {schema}.pg_meta_schema union all select node_id from {schema}.pg_meta_table union all select node_id from {schema}.pg_meta_column
    union all select node_id from {schema}.pg_meta_view union all select node_id from {schema}.pg_meta_index union all select node_id from {schema}.pg_meta_sequence union all select node_id from {schema}.pg_meta_routine
    union all select node_id from {schema}.pg_meta_constraint union all select node_id from {schema}.pg_meta_trigger union all select node_id from {schema}.pg_meta_type union all select node_id from {schema}.pg_meta_statistics) s where s.node_id = n.id)
union all select 'surface_name_ne_address', count(*) from {schema}.pg_meta_column c join {schema}.node n on n.id = c.node_id where c.name <> n.address->>'column'
union all select 'table_surface_ne_address', count(*) from {schema}.pg_meta_table c join {schema}.node n on n.id = c.node_id where c.name <> n.address->>'table' or c.schema_name <> n.address->>'schema';
