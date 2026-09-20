-- инварианты структуры ix; каждая строка должна дать 0
select 'node_without_tree' as chk, count(*) from ix.node n left join ix.tree t on t.node_id = n.id where t.id is null
union all select 'node_with_many_tree', count(*) from (select node_id from ix.tree group by node_id having count(*) > 1) x
union all select 'root_not_database', count(*) from ix.tree t join ix.node n on n.id = t.node_id where t.parent_id is null and n.surface <> 'pg_meta_database'
union all select 'database_with_parent', count(*) from ix.tree t join ix.node n on n.id = t.node_id where t.parent_id is not null and n.surface = 'pg_meta_database'
union all select 'tree_cycle_or_broken', count(*) from (
    with recursive up as (select n.id, t.parent_id, 1 as depth from ix.node n join ix.tree t on t.node_id = n.id
                          union all select up.id, t.parent_id, up.depth + 1 from up join ix.tree t on t.node_id = up.parent_id where up.depth < 20)
    select id from up group by id having max(depth) >= 20 or bool_and(parent_id is not null)) x
union all select 'column_parent_kind', count(*) from ix.tree t join ix.node c on c.id = t.node_id join ix.node p on p.id = t.parent_id where c.surface = 'pg_meta_column' and p.surface not in ('pg_meta_table', 'pg_meta_view')
union all select 'index_parent_kind', count(*) from ix.tree t join ix.node c on c.id = t.node_id join ix.node p on p.id = t.parent_id where c.surface = 'pg_meta_index' and p.surface <> 'pg_meta_table'
union all select 'edge_cross_source', count(*) from ix.edge e join ix.node s on s.id = e.node_src_id join ix.node d on d.id = e.node_tgt_id where s.address->>'host' <> d.address->>'host'
union all select 'edge_self', count(*) from ix.edge where node_src_id = node_tgt_id
union all select 'pg_edge_side_not_fk', count(*) from ix.pg_meta_edge m join ix.edge e on e.id = m.edge_id join ix.node s on s.id = e.node_src_id where m.side = 1 and s.surface <> 'pg_meta_constraint'
union all select 'surface_rows_ne_nodes', abs((select count(*) from ix.node) - (
    (select count(*) from ix.pg_meta_database) + (select count(*) from ix.pg_meta_schema) + (select count(*) from ix.pg_meta_table) + (select count(*) from ix.pg_meta_column)
  + (select count(*) from ix.pg_meta_view) + (select count(*) from ix.pg_meta_index) + (select count(*) from ix.pg_meta_sequence) + (select count(*) from ix.pg_meta_routine)
  + (select count(*) from ix.pg_meta_constraint) + (select count(*) from ix.pg_meta_trigger) + (select count(*) from ix.pg_meta_type) + (select count(*) from ix.pg_meta_statistics)))
union all select 'surface_wrong_table', count(*) from (
    select node_id, 'pg_meta_database' s from ix.pg_meta_database union all select node_id, 'pg_meta_schema' from ix.pg_meta_schema union all select node_id, 'pg_meta_table' from ix.pg_meta_table
    union all select node_id, 'pg_meta_column' from ix.pg_meta_column union all select node_id, 'pg_meta_view' from ix.pg_meta_view union all select node_id, 'pg_meta_index' from ix.pg_meta_index
    union all select node_id, 'pg_meta_sequence' from ix.pg_meta_sequence union all select node_id, 'pg_meta_routine' from ix.pg_meta_routine union all select node_id, 'pg_meta_constraint' from ix.pg_meta_constraint
    union all select node_id, 'pg_meta_trigger' from ix.pg_meta_trigger union all select node_id, 'pg_meta_type' from ix.pg_meta_type union all select node_id, 'pg_meta_statistics' from ix.pg_meta_statistics) x
    join ix.node n on n.id = x.node_id where n.surface::text <> x.s
union all select 'node_without_surface', count(*) from ix.node n where not exists (
    select 1 from (select node_id from ix.pg_meta_database union all select node_id from ix.pg_meta_schema union all select node_id from ix.pg_meta_table union all select node_id from ix.pg_meta_column
    union all select node_id from ix.pg_meta_view union all select node_id from ix.pg_meta_index union all select node_id from ix.pg_meta_sequence union all select node_id from ix.pg_meta_routine
    union all select node_id from ix.pg_meta_constraint union all select node_id from ix.pg_meta_trigger union all select node_id from ix.pg_meta_type union all select node_id from ix.pg_meta_statistics) s where s.node_id = n.id)
union all select 'surface_name_ne_address', count(*) from ix.pg_meta_column c join ix.node n on n.id = c.node_id where c.name <> n.address->>'column'
union all select 'table_surface_ne_address', count(*) from ix.pg_meta_table c join ix.node n on n.id = c.node_id where c.name <> n.address->>'table' or c.schema_name <> n.address->>'schema';
