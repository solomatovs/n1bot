insert into stage_tree (node_address, parent_address)
select n.address, null from stage_node n where n.kind = 'db';

insert into stage_tree (node_address, parent_address)
select n.address, d.address from stage_node n, stage_node d where n.kind = 'nsp' and d.kind = 'db';

-- Таблицы, view, sequence, функции, типы, статистика лежат в схеме
insert into stage_tree (node_address, parent_address)
select n.address, p.address
from stage_node n
join raw_class c on n.kind = 'rel' and c.oid = n.oid and c.relkind not in ('i', 'I')
join stage_node p on p.kind = 'nsp' and p.oid = c.relnamespace;

insert into stage_tree (node_address, parent_address)
select n.address, p.address from stage_node n join raw_proc x on n.kind = 'proc' and x.oid = n.oid join stage_node p on p.kind = 'nsp' and p.oid = x.pronamespace;

insert into stage_tree (node_address, parent_address)
select n.address, p.address from stage_node n join raw_type x on n.kind = 'typ' and x.oid = n.oid join stage_node p on p.kind = 'nsp' and p.oid = x.typnamespace;

insert into stage_tree (node_address, parent_address)
select n.address, p.address from stage_node n join raw_statistic_ext x on n.kind = 'stx' and x.oid = n.oid join stage_node p on p.kind = 'nsp' and p.oid = x.stxnamespace;

-- Индекс лежит под своей таблицей
insert into stage_tree (node_address, parent_address)
select n.address, p.address from stage_node n join raw_index i on n.kind = 'rel' and i.indexrelid = n.oid join stage_node p on p.kind = 'rel' and p.oid = i.indrelid;

-- Колонка, constraint, триггер лежат под отношением; constraint домена под типом
insert into stage_tree (node_address, parent_address)
select n.address, p.address from stage_node n join stage_node p on p.kind = 'rel' and p.oid = n.oid where n.kind = 'col';

insert into stage_tree (node_address, parent_address)
select n.address, p.address from stage_node n join raw_constraint c on n.kind = 'con' and c.oid = n.oid
join stage_node p on (c.conrelid <> 0 and p.kind = 'rel' and p.oid = c.conrelid) or (c.conrelid = 0 and p.kind = 'typ' and p.oid = c.contypid);

insert into stage_tree (node_address, parent_address)
select n.address, p.address from stage_node n join raw_trigger t on n.kind = 'trg' and t.oid = n.oid join stage_node p on p.kind = 'rel' and p.oid = t.tgrelid;
