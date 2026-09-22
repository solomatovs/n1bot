-- Стадия: node с адресами и ключами словаря, по которым строятся tree и edge.
-- kind + obj_id + sub_id однозначно называют объект: db/0, sch/user#, rel/obj# (таблица,
-- представление, mview по obj# контейнерной таблицы), seq/obj#, syn/obj#, rtn/obj#,
-- col/obj# таблицы/intcol#, con/con#, idx/obj#, trg/obj#.
create temp table stage_node (
    kind     text                not null,
    obj_id   bigint              not null,
    sub_id   int                 not null default 0,
    surface  {schema}.surface_e  not null,
    address  jsonb               not null,
    primary key (kind, obj_id, sub_id),
    unique (address)
);

create temp table stage_tree (
    node_address    jsonb not null,
    parent_address  jsonb,
    primary key (node_address)
);

-- Рёбра: src зависит от tgt, у каждого ребра роль; позиционные (index, constraint,
-- partition_key) несут side, ordinal, is_key, у остальных side = 0, ordinal = 0.
create temp table stage_edge (
    src_address  jsonb     not null,
    tgt_address  jsonb     not null,
    role         text      not null,
    side         smallint  not null default 0,
    ordinal      smallint  not null default 0,
    is_key       bool      not null default false,
    unique (src_address, tgt_address, role, side, ordinal)
);
