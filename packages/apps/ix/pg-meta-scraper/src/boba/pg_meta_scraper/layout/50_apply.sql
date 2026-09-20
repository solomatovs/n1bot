-- Согласование ix со стадией: всё или ничего.
-- Транзакция открыта клиентом как repeatable read с коротким lock_timeout
-- (begin isolation level repeatable read; set local lock_timeout = '2s'). Порядок:
--   1. замок for update на весь срез ix по scope источника: node, tree, edge, pg_edge,
--      surface-таблицы, в порядке id. Занятая строка ждёт не дольше lock_timeout, потом
--      ошибка; строка, изменённая другим сеансом после нашего снимка, даёт ошибку
--      сериализации. В обоих случаях транзакция откатывается целиком, клиент повторяет прогон;
--   2. для каждой таблицы ix два statement'а: удаление строк среза, которых нет в стадии
--      или которые отличаются, и вставка строк стадии, которых нет в срезе. Срез читается
--      заново в каждом statement'е из ix, поэтому строки, ушедшие каскадом, в план не
--      попадают. Промежуточные множества это CTE, а не temp-таблицы: у CTE нет строки в
--      pg_class и замка в общей таблице замков, а temp-таблица занимает слот
--      max_locks_per_transaction до коммита (она сама, её toast и индексы);
--   3. insert с on conflict do nothing: адрес, появившийся у другого сеанса после снимка,
--      в repeatable read даёт ошибку сериализации, а не пропуск;
--   4. apply_log это сводка planned/applied. Внутри удавшейся транзакции они равны всегда;
--      неравенство это ошибка пакета, а не повод для повтора.
-- Обновлений нет: surface однозначно следует из address, а других полей у node нет.
-- Изменившийся родитель, позиционная строка или строка surface это удаление плюс вставка.
-- Deadlock исключён порядком: все загрузчики берут замки node -> tree -> edge -> pg_edge ->
-- surface-таблицы по id.
-- Temp-таблиц здесь три: apply_log, scope_node (id node в scope) и node_map (адрес -> id).

create temp table apply_log (op text, planned bigint, applied bigint);

create temp table scope_node as
select n.id
from ix.node n, raw_source s
where n.address @> jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database);

-- 1. Замок на срез
select count(*) as locked_node from (
    select n.id from ix.node n join scope_node sc on sc.id = n.id order by n.id for update of n
) l;

select count(*) as locked_tree from (
    select t.id from ix.tree t join scope_node sc on sc.id = t.node_id order by t.id for update of t
) l;

select count(*) as locked_edge from (
    select e.id from ix.edge e
    where exists (select 1 from scope_node sc where sc.id = e.node_src_id or sc.id = e.node_tgt_id)
    order by e.id for update of e
) l;

select count(*) as locked_pg_meta_edge from (
    select m.edge_id, m.role, m.side, m.ordinal from ix.pg_meta_edge m
    join ix.edge e on e.id = m.edge_id
    where exists (select 1 from scope_node sc where sc.id = e.node_src_id or sc.id = e.node_tgt_id)
    order by m.edge_id, m.role, m.side, m.ordinal for update of m
) l;

select count(*) as locked_pg_meta_database from (
    select x.node_id from ix.pg_meta_database x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_schema from (
    select x.node_id from ix.pg_meta_schema x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_table from (
    select x.node_id from ix.pg_meta_table x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_column from (
    select x.node_id from ix.pg_meta_column x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_view from (
    select x.node_id from ix.pg_meta_view x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_index from (
    select x.node_id from ix.pg_meta_index x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_sequence from (
    select x.node_id from ix.pg_meta_sequence x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_routine from (
    select x.node_id from ix.pg_meta_routine x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_constraint from (
    select x.node_id from ix.pg_meta_constraint x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_trigger from (
    select x.node_id from ix.pg_meta_trigger x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_type from (
    select x.node_id from ix.pg_meta_type x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;

select count(*) as locked_pg_meta_statistics from (
    select x.node_id from ix.pg_meta_statistics x join scope_node sc on sc.id = x.node_id order by x.node_id for update of x
) l;


-- 2. node
with cur as (
    select n.id, n.surface, n.address from ix.node n join scope_node sc on sc.id = n.id
), del as (
    select c.id from cur c
    left join stage_node s on s.address = c.address
    where s.address is null or s.surface <> c.surface
), done as (
    delete from ix.node x using del d where x.id = d.id returning x.id
)
insert into apply_log select 'node_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select n.surface, n.address from ix.node n join scope_node sc on sc.id = n.id
), ins as (
    select s.surface, s.address from stage_node s
    left join cur c on c.address = s.address
    where c.address is null
), done as (
    insert into ix.node (surface, address)
    select surface, address from ins
    on conflict (address) do nothing
    returning id
)
insert into apply_log select 'node_insert', (select count(*) from ins), (select count(*) from done);

-- scope после вставки: все node источника, старые и новые
truncate scope_node;
insert into scope_node
select n.id
from ix.node n, raw_source s
where n.address @> jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database);

create temp table node_map as
select s.kind, s.oid, s.subid, s.address, s.surface, n.id
from stage_node s
join ix.node n on n.address = s.address;

-- 3. tree
with cur as (
    select t.id, t.node_id, t.parent_id from ix.tree t join scope_node sc on sc.id = t.node_id
), want as (
    select n.id as node_id, p.id as parent_id
    from stage_tree s
    join node_map n on n.address = s.node_address
    left join node_map p on p.address = s.parent_address
), del as (
    select c.id from cur c
    left join want w on w.node_id = c.node_id and w.parent_id is not distinct from c.parent_id
    where w.node_id is null
), done as (
    delete from ix.tree x using del d where x.id = d.id returning x.id
)
insert into apply_log select 'tree_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select t.id, t.node_id, t.parent_id from ix.tree t join scope_node sc on sc.id = t.node_id
), want as (
    select n.id as node_id, p.id as parent_id
    from stage_tree s
    join node_map n on n.address = s.node_address
    left join node_map p on p.address = s.parent_address
), ins as (
    select w.node_id, w.parent_id from want w
    left join cur c on c.node_id = w.node_id and c.parent_id is not distinct from w.parent_id
    where c.id is null
), done as (
    insert into ix.tree (node_id, parent_id)
    select node_id, parent_id from ins
    on conflict (node_id, parent_id) do nothing
    returning id
)
insert into apply_log select 'tree_insert', (select count(*) from ins), (select count(*) from done);

-- 4. edge: пары src, tgt
with cur as (
    select e.id, e.node_src_id, e.node_tgt_id from ix.edge e join scope_node sc on sc.id = e.node_src_id
), want as (
    select distinct sn.id as node_src_id, tn.id as node_tgt_id
    from stage_edge s
    join node_map sn on sn.address = s.src_address
    join node_map tn on tn.address = s.tgt_address
), del as (
    select c.id from cur c
    left join want w on w.node_src_id = c.node_src_id and w.node_tgt_id = c.node_tgt_id
    where w.node_src_id is null
), done as (
    delete from ix.edge x using del d where x.id = d.id returning x.id
)
insert into apply_log select 'edge_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select e.id, e.node_src_id, e.node_tgt_id from ix.edge e join scope_node sc on sc.id = e.node_src_id
), want as (
    select distinct sn.id as node_src_id, tn.id as node_tgt_id, sn.surface
    from stage_edge s
    join node_map sn on sn.address = s.src_address
    join node_map tn on tn.address = s.tgt_address
), ins as (
    select w.node_src_id, w.node_tgt_id, w.surface from want w
    left join cur c on c.node_src_id = w.node_src_id and c.node_tgt_id = w.node_tgt_id
    where c.id is null
), done as (
    insert into ix.edge (node_src_id, node_tgt_id, surface, weight)
    select node_src_id, node_tgt_id, surface, 1.0 from ins
    on conflict (node_src_id, node_tgt_id) do nothing
    returning id
)
insert into apply_log select 'edge_insert', (select count(*) from ins), (select count(*) from done);

-- 5. pg_edge: позиционные строки рёбер
with cur as (
    select m.edge_id, m.role::text as role, m.side, m.ordinal, m.is_key
    from ix.pg_meta_edge m join ix.edge e on e.id = m.edge_id join scope_node sc on sc.id = e.node_src_id
), want as (
    select distinct e.id as edge_id, s.role, s.side, s.ordinal, s.is_key
    from stage_edge s
    join node_map sn on sn.address = s.src_address
    join node_map tn on tn.address = s.tgt_address
    join ix.edge e on e.node_src_id = sn.id and e.node_tgt_id = tn.id
    where s.role is not null
), del as (
    select c.edge_id, c.role, c.side, c.ordinal from cur c
    left join want w on w.edge_id = c.edge_id and w.role = c.role and w.side = c.side
                    and w.ordinal = c.ordinal and w.is_key = c.is_key
    where w.edge_id is null
), done as (
    delete from ix.pg_meta_edge x using del d
    where x.edge_id = d.edge_id and x.role = d.role::ix.pg_meta_edge_role_e and x.side = d.side and x.ordinal = d.ordinal
    returning x.edge_id
)
insert into apply_log select 'pg_meta_edge_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select m.edge_id, m.role::text as role, m.side, m.ordinal, m.is_key
    from ix.pg_meta_edge m join ix.edge e on e.id = m.edge_id join scope_node sc on sc.id = e.node_src_id
), want as (
    select distinct e.id as edge_id, s.role, s.side, s.ordinal, s.is_key
    from stage_edge s
    join node_map sn on sn.address = s.src_address
    join node_map tn on tn.address = s.tgt_address
    join ix.edge e on e.node_src_id = sn.id and e.node_tgt_id = tn.id
    where s.role is not null
), ins as (
    select w.edge_id, w.role, w.side, w.ordinal, w.is_key from want w
    left join cur c on c.edge_id = w.edge_id and c.role = w.role and c.side = w.side
                   and c.ordinal = w.ordinal and c.is_key = w.is_key
    where c.edge_id is null
), done as (
    insert into ix.pg_meta_edge (edge_id, role, side, ordinal, is_key)
    select edge_id, role::ix.pg_meta_edge_role_e, side, ordinal, is_key from ins
    on conflict (edge_id, role, side, ordinal) do nothing
    returning edge_id
)
insert into apply_log select 'pg_meta_edge_insert', (select count(*) from ins), (select count(*) from done);

-- 6. surface node: строка целиком против строки целиком, ключ node_id

with cur as (
    select x.node_id, name, owner, encoding, collate_name, ctype, comment from ix.pg_meta_database x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.name, st.owner, st.encoding, st.collate_name, st.ctype, st.comment from stage_pg_meta_database st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.name, w.owner, w.encoding, w.collate_name, w.ctype, w.comment) is not distinct from row(c.name, c.owner, c.encoding, c.collate_name, c.ctype, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_database x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_database_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, name, owner, encoding, collate_name, ctype, comment from ix.pg_meta_database x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.name, st.owner, st.encoding, st.collate_name, st.ctype, st.comment from stage_pg_meta_database st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.name, c.owner, c.encoding, c.collate_name, c.ctype, c.comment) is not distinct from row(w.name, w.owner, w.encoding, w.collate_name, w.ctype, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_database (node_id, name, owner, encoding, collate_name, ctype, comment)
    select node_id, name, owner, encoding, collate_name, ctype, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_database_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, name, owner, comment from ix.pg_meta_schema x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.name, st.owner, st.comment from stage_pg_meta_schema st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.name, w.owner, w.comment) is not distinct from row(c.name, c.owner, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_schema x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_schema_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, name, owner, comment from ix.pg_meta_schema x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.name, st.owner, st.comment from stage_pg_meta_schema st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.name, c.owner, c.comment) is not distinct from row(w.name, w.owner, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_schema (node_id, name, owner, comment)
    select node_id, name, owner, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_schema_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, owner, tablespace, persistence, partition_bound, row_estimate, pages, has_index, has_triggers, distribution, storage, comment from ix.pg_meta_table x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.owner, st.tablespace, st.persistence, st.partition_bound, st.row_estimate, st.pages, st.has_index, st.has_triggers, st.distribution, st.storage, st.comment from stage_pg_meta_table st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.name, w.kind, w.owner, w.tablespace, w.persistence, w.partition_bound, w.row_estimate, w.pages, w.has_index, w.has_triggers, w.distribution, w.storage, w.comment) is not distinct from row(c.schema_name, c.name, c.kind, c.owner, c.tablespace, c.persistence, c.partition_bound, c.row_estimate, c.pages, c.has_index, c.has_triggers, c.distribution, c.storage, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_table x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_table_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, owner, tablespace, persistence, partition_bound, row_estimate, pages, has_index, has_triggers, distribution, storage, comment from ix.pg_meta_table x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.owner, st.tablespace, st.persistence, st.partition_bound, st.row_estimate, st.pages, st.has_index, st.has_triggers, st.distribution, st.storage, st.comment from stage_pg_meta_table st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.name, c.kind, c.owner, c.tablespace, c.persistence, c.partition_bound, c.row_estimate, c.pages, c.has_index, c.has_triggers, c.distribution, c.storage, c.comment) is not distinct from row(w.schema_name, w.name, w.kind, w.owner, w.tablespace, w.persistence, w.partition_bound, w.row_estimate, w.pages, w.has_index, w.has_triggers, w.distribution, w.storage, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_table (node_id, schema_name, name, kind, owner, tablespace, persistence, partition_bound, row_estimate, pages, has_index, has_triggers, distribution, storage, comment)
    select node_id, schema_name, name, kind, owner, tablespace, persistence, partition_bound, row_estimate, pages, has_index, has_triggers, distribution, storage, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_table_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, relation_name, relation_kind, name, ordinal, data_type, not_null, default_expr, identity, generated, comment from ix.pg_meta_column x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.relation_name, st.relation_kind, st.name, st.ordinal, st.data_type, st.not_null, st.default_expr, st.identity, st.generated, st.comment from stage_pg_meta_column st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.relation_name, w.relation_kind, w.name, w.ordinal, w.data_type, w.not_null, w.default_expr, w.identity, w.generated, w.comment) is not distinct from row(c.schema_name, c.relation_name, c.relation_kind, c.name, c.ordinal, c.data_type, c.not_null, c.default_expr, c.identity, c.generated, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_column x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_column_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, relation_name, relation_kind, name, ordinal, data_type, not_null, default_expr, identity, generated, comment from ix.pg_meta_column x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.relation_name, st.relation_kind, st.name, st.ordinal, st.data_type, st.not_null, st.default_expr, st.identity, st.generated, st.comment from stage_pg_meta_column st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.relation_name, c.relation_kind, c.name, c.ordinal, c.data_type, c.not_null, c.default_expr, c.identity, c.generated, c.comment) is not distinct from row(w.schema_name, w.relation_name, w.relation_kind, w.name, w.ordinal, w.data_type, w.not_null, w.default_expr, w.identity, w.generated, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_column (node_id, schema_name, relation_name, relation_kind, name, ordinal, data_type, not_null, default_expr, identity, generated, comment)
    select node_id, schema_name, relation_name, relation_kind, name, ordinal, data_type, not_null, default_expr, identity, generated, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_column_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, owner, comment from ix.pg_meta_view x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.owner, st.comment from stage_pg_meta_view st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.name, w.kind, w.owner, w.comment) is not distinct from row(c.schema_name, c.name, c.kind, c.owner, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_view x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_view_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, owner, comment from ix.pg_meta_view x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.owner, st.comment from stage_pg_meta_view st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.name, c.kind, c.owner, c.comment) is not distinct from row(w.schema_name, w.name, w.kind, w.owner, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_view (node_id, schema_name, name, kind, owner, comment)
    select node_id, schema_name, name, kind, owner, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_view_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, table_name, name, access_method, is_unique, is_primary, is_exclusion, is_valid, columns, expression, predicate, comment from ix.pg_meta_index x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.table_name, st.name, st.access_method, st.is_unique, st.is_primary, st.is_exclusion, st.is_valid, st.columns, st.expression, st.predicate, st.comment from stage_pg_meta_index st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.table_name, w.name, w.access_method, w.is_unique, w.is_primary, w.is_exclusion, w.is_valid, w.columns, w.expression, w.predicate, w.comment) is not distinct from row(c.schema_name, c.table_name, c.name, c.access_method, c.is_unique, c.is_primary, c.is_exclusion, c.is_valid, c.columns, c.expression, c.predicate, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_index x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_index_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, table_name, name, access_method, is_unique, is_primary, is_exclusion, is_valid, columns, expression, predicate, comment from ix.pg_meta_index x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.table_name, st.name, st.access_method, st.is_unique, st.is_primary, st.is_exclusion, st.is_valid, st.columns, st.expression, st.predicate, st.comment from stage_pg_meta_index st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.table_name, c.name, c.access_method, c.is_unique, c.is_primary, c.is_exclusion, c.is_valid, c.columns, c.expression, c.predicate, c.comment) is not distinct from row(w.schema_name, w.table_name, w.name, w.access_method, w.is_unique, w.is_primary, w.is_exclusion, w.is_valid, w.columns, w.expression, w.predicate, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_index (node_id, schema_name, table_name, name, access_method, is_unique, is_primary, is_exclusion, is_valid, columns, expression, predicate, comment)
    select node_id, schema_name, table_name, name, access_method, is_unique, is_primary, is_exclusion, is_valid, columns, expression, predicate, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_index_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, owner, data_type, start_value, increment, min_value, max_value, cycle, comment from ix.pg_meta_sequence x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.owner, st.data_type, st.start_value, st.increment, st.min_value, st.max_value, st.cycle, st.comment from stage_pg_meta_sequence st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.name, w.owner, w.data_type, w.start_value, w.increment, w.min_value, w.max_value, w.cycle, w.comment) is not distinct from row(c.schema_name, c.name, c.owner, c.data_type, c.start_value, c.increment, c.min_value, c.max_value, c.cycle, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_sequence x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_sequence_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, owner, data_type, start_value, increment, min_value, max_value, cycle, comment from ix.pg_meta_sequence x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.owner, st.data_type, st.start_value, st.increment, st.min_value, st.max_value, st.cycle, st.comment from stage_pg_meta_sequence st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.name, c.owner, c.data_type, c.start_value, c.increment, c.min_value, c.max_value, c.cycle, c.comment) is not distinct from row(w.schema_name, w.name, w.owner, w.data_type, w.start_value, w.increment, w.min_value, w.max_value, w.cycle, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_sequence (node_id, schema_name, name, owner, data_type, start_value, increment, min_value, max_value, cycle, comment)
    select node_id, schema_name, name, owner, data_type, start_value, increment, min_value, max_value, cycle, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_sequence_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, language, identity_args, result_type, volatility, security_definer, owner, comment from ix.pg_meta_routine x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.language, st.identity_args, st.result_type, st.volatility, st.security_definer, st.owner, st.comment from stage_pg_meta_routine st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.name, w.kind, w.language, w.identity_args, w.result_type, w.volatility, w.security_definer, w.owner, w.comment) is not distinct from row(c.schema_name, c.name, c.kind, c.language, c.identity_args, c.result_type, c.volatility, c.security_definer, c.owner, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_routine x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_routine_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, language, identity_args, result_type, volatility, security_definer, owner, comment from ix.pg_meta_routine x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.language, st.identity_args, st.result_type, st.volatility, st.security_definer, st.owner, st.comment from stage_pg_meta_routine st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.name, c.kind, c.language, c.identity_args, c.result_type, c.volatility, c.security_definer, c.owner, c.comment) is not distinct from row(w.schema_name, w.name, w.kind, w.language, w.identity_args, w.result_type, w.volatility, w.security_definer, w.owner, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_routine (node_id, schema_name, name, kind, language, identity_args, result_type, volatility, security_definer, owner, comment)
    select node_id, schema_name, name, kind, language, identity_args, result_type, volatility, security_definer, owner, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_routine_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, table_name, name, kind, definition, is_deferrable, is_deferred, is_validated, on_update, on_delete, match_type, comment from ix.pg_meta_constraint x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.table_name, st.name, st.kind, st.definition, st.is_deferrable, st.is_deferred, st.is_validated, st.on_update, st.on_delete, st.match_type, st.comment from stage_pg_meta_constraint st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.table_name, w.name, w.kind, w.definition, w.is_deferrable, w.is_deferred, w.is_validated, w.on_update, w.on_delete, w.match_type, w.comment) is not distinct from row(c.schema_name, c.table_name, c.name, c.kind, c.definition, c.is_deferrable, c.is_deferred, c.is_validated, c.on_update, c.on_delete, c.match_type, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_constraint x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_constraint_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, table_name, name, kind, definition, is_deferrable, is_deferred, is_validated, on_update, on_delete, match_type, comment from ix.pg_meta_constraint x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.table_name, st.name, st.kind, st.definition, st.is_deferrable, st.is_deferred, st.is_validated, st.on_update, st.on_delete, st.match_type, st.comment from stage_pg_meta_constraint st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.table_name, c.name, c.kind, c.definition, c.is_deferrable, c.is_deferred, c.is_validated, c.on_update, c.on_delete, c.match_type, c.comment) is not distinct from row(w.schema_name, w.table_name, w.name, w.kind, w.definition, w.is_deferrable, w.is_deferred, w.is_validated, w.on_update, w.on_delete, w.match_type, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_constraint (node_id, schema_name, table_name, name, kind, definition, is_deferrable, is_deferred, is_validated, on_update, on_delete, match_type, comment)
    select node_id, schema_name, table_name, name, kind, definition, is_deferrable, is_deferred, is_validated, on_update, on_delete, match_type, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_constraint_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, table_name, name, timing, events, row_level, enabled, comment from ix.pg_meta_trigger x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.table_name, st.name, st.timing, st.events, st.row_level, st.enabled, st.comment from stage_pg_meta_trigger st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.table_name, w.name, w.timing, w.events, w.row_level, w.enabled, w.comment) is not distinct from row(c.schema_name, c.table_name, c.name, c.timing, c.events, c.row_level, c.enabled, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_trigger x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_trigger_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, table_name, name, timing, events, row_level, enabled, comment from ix.pg_meta_trigger x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.table_name, st.name, st.timing, st.events, st.row_level, st.enabled, st.comment from stage_pg_meta_trigger st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.table_name, c.name, c.timing, c.events, c.row_level, c.enabled, c.comment) is not distinct from row(w.schema_name, w.table_name, w.name, w.timing, w.events, w.row_level, w.enabled, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_trigger (node_id, schema_name, table_name, name, timing, events, row_level, enabled, comment)
    select node_id, schema_name, table_name, name, timing, events, row_level, enabled, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_trigger_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, base_type, enum_labels, comment from ix.pg_meta_type x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.base_type, st.enum_labels, st.comment from stage_pg_meta_type st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.name, w.kind, w.base_type, w.enum_labels, w.comment) is not distinct from row(c.schema_name, c.name, c.kind, c.base_type, c.enum_labels, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_type x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_type_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, kind, base_type, enum_labels, comment from ix.pg_meta_type x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.kind, st.base_type, st.enum_labels, st.comment from stage_pg_meta_type st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.name, c.kind, c.base_type, c.enum_labels, c.comment) is not distinct from row(w.schema_name, w.name, w.kind, w.base_type, w.enum_labels, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_type (node_id, schema_name, name, kind, base_type, enum_labels, comment)
    select node_id, schema_name, name, kind, base_type, enum_labels, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_type_insert', (select count(*) from ins), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, table_name, kinds, comment from ix.pg_meta_statistics x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.table_name, st.kinds, st.comment from stage_pg_meta_statistics st join node_map nm on nm.address = st.address
), del as (
    select c.node_id from cur c
    left join want w on w.node_id = c.node_id and row(w.schema_name, w.name, w.table_name, w.kinds, w.comment) is not distinct from row(c.schema_name, c.name, c.table_name, c.kinds, c.comment)
    where w.node_id is null
), done as (
    delete from ix.pg_meta_statistics x using del d where x.node_id = d.node_id returning x.node_id
)
insert into apply_log select 'pg_meta_statistics_delete', (select count(*) from del), (select count(*) from done);

with cur as (
    select x.node_id, schema_name, name, table_name, kinds, comment from ix.pg_meta_statistics x join scope_node sc on sc.id = x.node_id
), want as (
    select nm.id as node_id, st.schema_name, st.name, st.table_name, st.kinds, st.comment from stage_pg_meta_statistics st join node_map nm on nm.address = st.address
), ins as (
    select w.* from want w
    left join cur c on c.node_id = w.node_id and row(c.schema_name, c.name, c.table_name, c.kinds, c.comment) is not distinct from row(w.schema_name, w.name, w.table_name, w.kinds, w.comment)
    where c.node_id is null
), done as (
    insert into ix.pg_meta_statistics (node_id, schema_name, name, table_name, kinds, comment)
    select node_id, schema_name, name, table_name, kinds, comment from ins
    on conflict (node_id) do nothing
    returning node_id
)
insert into apply_log select 'pg_meta_statistics_insert', (select count(*) from ins), (select count(*) from done);

-- 7. Сводка
select op, planned, applied from apply_log;
