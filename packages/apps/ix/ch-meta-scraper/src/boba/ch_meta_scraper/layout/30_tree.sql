insert into stage_tree
    (node_address, parent_address)
select
    n.address, null
from
    stage_node n
where
    n.kind = 'srv';

-- Базы и функции лежат под сервером
insert into stage_tree
    (node_address, parent_address)
select
    n.address, srv.address
from
    stage_node n,
    stage_node srv
where
    n.kind in ('db', 'fn')
    and srv.kind = 'srv';

-- Таблицы, представления и словари лежат в базе
insert into stage_tree
    (node_address, parent_address)
select
    n.address, p.address
from
    stage_node n
    join stage_node p on p.kind = 'db' and p.database = n.database
where
    n.kind in ('rel', 'dict');

-- Колонки, индексы и проекции лежат под своим отношением
insert into stage_tree
    (node_address, parent_address)
select
    n.address, p.address
from
    stage_node n
    join stage_node p
        on  p.kind in ('rel', 'dict')
        and p.database = n.database
        and p.relation = n.relation
where
    n.kind in ('col', 'idx', 'proj');
