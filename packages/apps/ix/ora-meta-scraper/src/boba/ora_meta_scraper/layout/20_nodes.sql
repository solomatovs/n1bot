-- Адреса всех node из raw_*. Битовые поля словаря numeric, бит k проверяется как
-- mod(floor(x / 2^k), 2) = 1.

-- База: корень tree источника
insert into stage_node
    (kind, obj_id, surface, address)
select
    'db', 0, 'ora_meta_database',
    jsonb_build_object(
        'scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database
    )
from
    raw_source s;

-- Схемы: пользователи, не принадлежащие Oracle
insert into stage_node
    (kind, obj_id, surface, address)
select
    'sch', u.user_id, 'ora_meta_schema',
    db.address || jsonb_build_object('schema', u.name)
from
    raw_users u,
    stage_node db
where
    db.kind = 'db';

-- Таблицы: объект type# 2 со строкой tab$, кроме overflow-сегмента IOT (512),
-- вложенной таблицы (8192) и контейнера материализованного представления (2^26)
insert into stage_node
    (kind, obj_id, surface, address)
select
    'rel', o.obj_id, 'ora_meta_table',
    sch.address || jsonb_build_object('table', o.name)
from
    raw_objects o
    join raw_tables t on t.obj_id = o.obj_id
    join stage_node sch on sch.kind = 'sch' and sch.obj_id = o.owner_id
where
    o.type_id = 2
    and mod(floor(t.property / 512), 2) = 0
    and mod(floor(t.property / 8192), 2) = 0
    and mod(floor(t.property / 67108864), 2) = 0;

-- Представления
insert into stage_node
    (kind, obj_id, surface, address)
select
    'rel', o.obj_id, 'ora_meta_view',
    sch.address || jsonb_build_object('view', o.name)
from
    raw_objects o
    join stage_node sch on sch.kind = 'sch' and sch.obj_id = o.owner_id
where
    o.type_id = 4;

-- Материализованные представления: node по obj# контейнерной таблицы, потому что
-- колонки, индексы и constraint'ы словарь вешает на неё; объект type# 42 живёт в
-- stage_alias ниже
insert into stage_node
    (kind, obj_id, surface, address)
select
    'rel', c.obj_id, 'ora_meta_mview',
    sch.address || jsonb_build_object('mview', m.name)
from
    raw_mviews m
    join raw_users u on u.name = m.owner_name
    join raw_objects c
        on  c.owner_id = u.user_id
        and c.name = m.container_name
        and c.type_id = 2
    join stage_node sch on sch.kind = 'sch' and sch.obj_id = u.user_id;

-- Последовательности, синонимы, подпрограммы и типы
insert into stage_node
    (kind, obj_id, surface, address)
select
    case o.type_id when 6 then 'seq' when 5 then 'syn' else 'rtn' end,
    o.obj_id,
    case o.type_id
        when 6 then 'ora_meta_sequence'
        when 5 then 'ora_meta_synonym'
        else 'ora_meta_routine'
    end::{schema}.surface_e,
    sch.address || jsonb_build_object(
        case o.type_id when 6 then 'sequence' when 5 then 'synonym' else 'routine' end,
        o.name
    )
from
    raw_objects o
    join stage_node sch on sch.kind = 'sch' and sch.obj_id = o.owner_id
where
    o.type_id in (5, 6, 7, 8, 9, 13);

-- Колонки таблиц, представлений и mview, кроме скрытых (бит 32: unused, колонки
-- функциональных индексов, системные)
insert into stage_node
    (kind, obj_id, sub_id, surface, address)
select
    'col', c.obj_id, c.intcol_id, 'ora_meta_column',
    rel.address || jsonb_build_object('column', c.name)
from
    raw_columns c
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = c.obj_id
where
    mod(floor(c.property / 32), 2) = 0;

-- Constraint'ы: check (1), primary (2), unique (3), foreign (4), view check option (5),
-- view read only (6); not null (7) и служебные виды не снимаются
insert into stage_node
    (kind, obj_id, surface, address)
select
    'con', d.con_id, 'ora_meta_constraint',
    rel.address || jsonb_build_object('constraint', cn.name)
from
    raw_cdef d
    join raw_con cn on cn.con_id = d.con_id
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = d.obj_id
where
    d.type_id in (1, 2, 3, 4, 5, 6);

-- Индексы под таблицей или mview, кроме индексов LOB (8) и вложенных IOT (5)
insert into stage_node
    (kind, obj_id, surface, address)
select
    'idx', i.obj_id, 'ora_meta_index',
    rel.address || jsonb_build_object('index', o.name)
from
    raw_indexes i
    join raw_objects o on o.obj_id = i.obj_id
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = i.bo_id
where
    i.type_id in (1, 2, 3, 4, 6, 7, 9);

-- Триггеры под таблицей или представлением
insert into stage_node
    (kind, obj_id, surface, address)
select
    'trg', t.obj_id, 'ora_meta_trigger',
    rel.address || jsonb_build_object('trigger', o.name)
from
    raw_triggers t
    join raw_objects o on o.obj_id = t.obj_id
    join stage_node rel on rel.kind = 'rel' and rel.obj_id = t.base_obj_id;

-- Объект словаря -> адрес node: все объекты схемы по obj# плюс объект mview (type# 42),
-- на который ссылаются dependency$ и синонимы, в адрес его node
create temp table stage_alias (
    obj_id   bigint primary key,
    address  jsonb  not null
);

insert into stage_alias
select
    n.obj_id, n.address
from
    stage_node n
where
    n.kind in ('rel', 'seq', 'syn', 'rtn');

insert into stage_alias
select
    o.obj_id, n.address
from
    raw_objects o
    join raw_users u on u.user_id = o.owner_id
    join raw_mviews m on m.owner_name = u.name and m.name = o.name
    join raw_objects c
        on  c.owner_id = u.user_id
        and c.name = m.container_name
        and c.type_id = 2
    join stage_node n on n.kind = 'rel' and n.obj_id = c.obj_id
where
    o.type_id = 42
on conflict do nothing;
