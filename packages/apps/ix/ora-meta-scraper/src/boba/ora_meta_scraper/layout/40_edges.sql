-- Все рёбра: src зависит от tgt. Позиционные рёбра (index, constraint, partition_key)
-- несут ordinal из словаря, у dependency и synonym позиции нет. Одна пара src, tgt
-- может прийти несколько раз: FK на колонку своей же таблицы, синоним на mview и её
-- контейнер; дубль отбрасывает on conflict do nothing.

-- Индекс перечисляет колонки; выражение функционального индекса живёт в скрытой
-- колонке, node у неё нет, поэтому такая позиция ребра не даёт
insert into stage_edge
select
    idx.address, col.address, 'index', 0, ic.pos, true
from
    raw_icol ic
    join stage_node idx on idx.kind = 'idx' and idx.obj_id = ic.obj_id
    join stage_node col
        on  col.kind = 'col'
        and col.obj_id = ic.bo_id
        and col.sub_id = ic.intcol_id
on conflict do nothing;

-- Constraint перечисляет свои колонки
insert into stage_edge
select
    con.address, col.address, 'constraint', 0, coalesce(cc.pos, 0), true
from
    raw_ccol cc
    join stage_node con on con.kind = 'con' and con.obj_id = cc.con_id
    join stage_node col
        on  col.kind = 'col'
        and col.obj_id = cc.obj_id
        and col.sub_id = cc.intcol_id
on conflict do nothing;

-- FK перечисляет колонки constraint'а, на который ссылается
insert into stage_edge
select
    con.address, col.address, 'constraint', 1, coalesce(rc.pos, 0), true
from
    raw_cdef d
    join raw_ccol rc on rc.con_id = d.rcon_id
    join stage_node con on con.kind = 'con' and con.obj_id = d.con_id
    join stage_node col
        on  col.kind = 'col'
        and col.obj_id = rc.obj_id
        and col.sub_id = rc.intcol_id
where
    d.type_id = 4
on conflict do nothing;

-- Ключ партиционирования таблицы
insert into stage_edge
select
    rel.address, col.address, 'partition_key', 0, pc.pos, true
from
    raw_partcol pc
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = pc.obj_id
    join stage_node col
        on  col.kind = 'col'
        and col.obj_id = pc.obj_id
        and col.sub_id = pc.intcol_id
on conflict do nothing;

-- dependency$: представление, mview, подпрограмма или триггер зависит от объекта
insert into stage_edge
select
    s.address, t.address, 'dependency', 0, 0, false
from
    raw_dependencies dp
    join stage_alias s on s.obj_id = dp.d_obj_id
    join stage_alias t on t.obj_id = dp.p_obj_id
where
    s.address <> t.address
on conflict do nothing;

-- Синоним указывает на объект своей базы; синонимы на объекты Oracle и по db link
-- ребра не дают
insert into stage_edge
select
    syn.address, t.address, 'synonym', 0, 0, false
from
    raw_synonyms sy
    join stage_node syn on syn.kind = 'syn' and syn.obj_id = sy.obj_id
    join raw_users u on u.name = sy.owner_name
    join raw_objects o
        on  o.owner_id = u.user_id
        and o.name = sy.name
        and o.type_id in (2, 4, 5, 6, 7, 8, 9, 13, 42)
    join stage_alias t on t.obj_id = o.obj_id
where
    sy.node is null
    and syn.address <> t.address
on conflict do nothing;
