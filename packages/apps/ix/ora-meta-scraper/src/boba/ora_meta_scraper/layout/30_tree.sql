insert into stage_tree
    (node_address, parent_address)
select
    n.address, null
from
    stage_node n
where
    n.kind = 'db';

-- Схемы лежат под базой
insert into stage_tree
    (node_address, parent_address)
select
    n.address, db.address
from
    stage_node n,
    stage_node db
where
    n.kind = 'sch'
    and db.kind = 'db';

-- Таблицы, представления, mview, последовательности, синонимы и подпрограммы лежат в
-- схеме владельца
insert into stage_tree
    (node_address, parent_address)
select
    n.address, sch.address
from
    stage_node n
    join raw_objects o on o.obj_id = n.obj_id
    join stage_node sch on sch.kind = 'sch' and sch.obj_id = o.owner_id
where
    n.kind in ('rel', 'seq', 'syn', 'rtn');

-- Колонки лежат под своим отношением
insert into stage_tree
    (node_address, parent_address)
select
    n.address, rel.address
from
    stage_node n
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = n.obj_id
where
    n.kind = 'col';

-- Constraint'ы под таблицей или представлением
insert into stage_tree
    (node_address, parent_address)
select
    n.address, rel.address
from
    stage_node n
    join raw_cdef d on d.con_id = n.obj_id
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = d.obj_id
where
    n.kind = 'con';

-- Индексы под таблицей или mview
insert into stage_tree
    (node_address, parent_address)
select
    n.address, rel.address
from
    stage_node n
    join raw_indexes i on i.obj_id = n.obj_id
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = i.bo_id
where
    n.kind = 'idx';

-- Триггеры под таблицей или представлением
insert into stage_tree
    (node_address, parent_address)
select
    n.address, rel.address
from
    stage_node n
    join raw_triggers t on t.obj_id = n.obj_id
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = t.base_obj_id
where
    n.kind = 'trg';
