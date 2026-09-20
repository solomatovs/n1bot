-- Все рёбра: src зависит от tgt. Позиционные рёбра несут role, side, ordinal, is_key;
-- у остальных эти поля пусты. Одна пара src, tgt может прийти несколько раз: колонка
-- индекса из indkey и та же из pg_depend, ключ партиционирования и ключ распределения на
-- одной колонке, самоссылающийся FK. Позиционные строки различаются ролью, стороной и
-- позицией, непозиционный дубль отбрасывает on conflict do nothing.

-- Индекс перечисляет колонки
insert into stage_edge select i_n.address, c_n.address, 'index', 0, k.ord, k.ord <= i.indnkeyatts
from raw_index i
join lateral unnest(i.indkey) with ordinality as k(attnum, ord) on k.attnum > 0
join stage_node i_n on i_n.kind = 'rel' and i_n.oid = i.indexrelid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = i.indrelid and c_n.subid = k.attnum
on conflict do nothing;

-- Constraint перечисляет свои колонки
insert into stage_edge select con_n.address, c_n.address, 'constraint', 0, k.ord, true
from raw_constraint con
join lateral unnest(con.conkey) with ordinality as k(attnum, ord) on true
join stage_node con_n on con_n.kind = 'con' and con_n.oid = con.oid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = con.conrelid and c_n.subid = k.attnum
on conflict do nothing;

-- FK перечисляет целевые колонки
insert into stage_edge select con_n.address, c_n.address, 'constraint', 1, k.ord, true
from raw_constraint con
join lateral unnest(con.confkey) with ordinality as k(attnum, ord) on true
join stage_node con_n on con_n.kind = 'con' and con_n.oid = con.oid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = con.confrelid and c_n.subid = k.attnum
where con.contype = 'f'
on conflict do nothing;

-- Ключ партиционирования (PostgreSQL 10+ и Greenplum 7)
insert into stage_edge select t_n.address, c_n.address, 'partition_key', 0, k.ord, true
from raw_partitioned_table p
join lateral unnest(p.partattrs) with ordinality as k(attnum, ord) on k.attnum > 0
join stage_node t_n on t_n.kind = 'rel' and t_n.oid = p.partrelid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = p.partrelid and c_n.subid = k.attnum
on conflict do nothing;

-- Ключ партиционирования Greenplum 6
insert into stage_edge select t_n.address, c_n.address, 'partition_key', 0, k.ord, true
from raw_gp_partition p
join lateral unnest(p.paratts) with ordinality as k(attnum, ord) on k.attnum > 0
join stage_node t_n on t_n.kind = 'rel' and t_n.oid = p.parrelid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = p.parrelid and c_n.subid = k.attnum
on conflict do nothing;

-- Ключ распределения Greenplum
insert into stage_edge select t_n.address, c_n.address, 'distribution_key', 0, k.ord, true
from raw_gp_distribution_policy p
join lateral unnest(p.distkey) with ordinality as k(attnum, ord) on k.attnum > 0
join stage_node t_n on t_n.kind = 'rel' and t_n.oid = p.localoid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = p.localoid and c_n.subid = k.attnum
on conflict do nothing;

-- Триггер UPDATE OF перечисляет колонки
insert into stage_edge select t_n.address, c_n.address, 'trigger', 0, k.ord, true
from raw_trigger t
join lateral unnest(t.tgattr) with ordinality as k(attnum, ord) on true
join stage_node t_n on t_n.kind = 'trg' and t_n.oid = t.oid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = t.tgrelid and c_n.subid = k.attnum
on conflict do nothing;

-- Расширенная статистика перечисляет колонки
insert into stage_edge select s_n.address, c_n.address, 'statistics', 0, k.ord, true
from raw_statistic_ext s
join lateral unnest(s.stxkeys) with ordinality as k(attnum, ord) on true
join stage_node s_n on s_n.kind = 'stx' and s_n.oid = s.oid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = s.stxrelid and c_n.subid = k.attnum
on conflict do nothing;

-- Дальше рёбра без member

-- Constraint опирается на индекс (свой у PK/UNIQUE/EXCLUDE, чужой у FK)
insert into stage_edge select con_n.address, i_n.address, null, null, null, null
from raw_constraint con
join stage_node con_n on con_n.kind = 'con' and con_n.oid = con.oid
join stage_node i_n on i_n.kind = 'rel' and i_n.oid = con.conindid
where con.conindid <> 0
on conflict do nothing;

-- Индекс зависит от колонки через выражение или предикат. Deptype не фильтруется:
-- на 11 у дочерних индексов партиций он I, с 12 a; колонки из indkey уже вставлены выше.
insert into stage_edge select i_n.address, c_n.address, null, null, null, null
from raw_depend d
join stage_node i_n on i_n.kind = 'rel' and i_n.surface = 'pg_meta_index' and i_n.oid = d.objid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = d.refobjid and c_n.subid = d.refobjsubid
where d.classid = 1259 and d.refclassid = 1259 and d.refobjsubid > 0
on conflict do nothing;

-- View читает колонку, а таблицу целиком только когда колонок в запросе нет
insert into stage_edge select v_n.address, x_n.address, null, null, null, null
from raw_depend d
join raw_rewrite r on r.oid = d.objid
join stage_node v_n on v_n.kind = 'rel' and v_n.surface = 'pg_meta_view' and v_n.oid = r.ev_class
join stage_node x_n on (d.refobjsubid > 0 and x_n.kind = 'col' and x_n.oid = d.refobjid and x_n.subid = d.refobjsubid)
               or (d.refobjsubid = 0 and x_n.kind = 'rel' and x_n.oid = d.refobjid)
where d.classid = 2618 and d.refclassid = 1259 and d.deptype = 'n' and d.refobjid <> r.ev_class
on conflict do nothing;

-- Default тянет sequence или функцию, generated зависит от колонки (pg_depend 15+)
insert into stage_edge select c_n.address, x_n.address, null, null, null, null
from raw_depend d
join raw_attrdef ad on ad.oid = d.objid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = ad.adrelid and c_n.subid = ad.adnum
join stage_node x_n on (d.refclassid = 1259 and d.refobjsubid > 0 and x_n.kind = 'col' and x_n.oid = d.refobjid and x_n.subid = d.refobjsubid)
               or (d.refclassid = 1259 and d.refobjsubid = 0 and x_n.kind = 'rel' and x_n.oid = d.refobjid)
               or (d.refclassid = 1255 and x_n.kind = 'proc' and x_n.oid = d.refobjid)
where d.classid = 2604 and d.deptype = 'n'
on conflict do nothing;

-- Sequence принадлежит колонке: owned by (a) и identity (i)
insert into stage_edge select s_n.address, c_n.address, null, null, null, null
from raw_depend d
join stage_node s_n on s_n.kind = 'rel' and s_n.surface = 'pg_meta_sequence' and s_n.oid = d.objid
join stage_node c_n on c_n.kind = 'col' and c_n.oid = d.refobjid and c_n.subid = d.refobjsubid
where d.classid = 1259 and d.refclassid = 1259 and d.deptype in ('a', 'i') and d.refobjsubid > 0
on conflict do nothing;

-- Партиция, потомок, дочерний индекс партиции
insert into stage_edge select c_n.address, p_n.address, null, null, null, null
from raw_inherits i
join stage_node c_n on c_n.kind = 'rel' and c_n.oid = i.inhrelid
join stage_node p_n on p_n.kind = 'rel' and p_n.oid = i.inhparent
on conflict do nothing;

-- Триггер зовёт функцию
insert into stage_edge select t_n.address, p_n.address, null, null, null, null
from raw_trigger t
join stage_node t_n on t_n.kind = 'trg' and t_n.oid = t.oid
join stage_node p_n on p_n.kind = 'proc' and p_n.oid = t.tgfoid
on conflict do nothing;

-- Функция с телом begin atomic зависит от таблицы или колонки
insert into stage_edge select p_n.address, x_n.address, null, null, null, null
from raw_depend d
join stage_node p_n on p_n.kind = 'proc' and p_n.oid = d.objid
join stage_node x_n on (d.refobjsubid > 0 and x_n.kind = 'col' and x_n.oid = d.refobjid and x_n.subid = d.refobjsubid)
               or (d.refobjsubid = 0 and x_n.kind = 'rel' and x_n.oid = d.refobjid)
where d.classid = 1255 and d.refclassid = 1259 and d.deptype = 'n'
on conflict do nothing;

-- Колонка пользовательского типа: домен, enum, composite, range
insert into stage_edge select c_n.address, t_n.address, null, null, null, null
from raw_attribute at
join stage_node c_n on c_n.kind = 'col' and c_n.oid = at.attrelid and c_n.subid = at.attnum
join stage_node t_n on t_n.kind = 'typ' and t_n.oid = at.atttypid
on conflict do nothing;
