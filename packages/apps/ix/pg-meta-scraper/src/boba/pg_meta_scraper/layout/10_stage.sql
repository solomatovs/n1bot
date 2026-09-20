-- Стадия: node с адресами и ключами каталога, по которым строятся tree и edge.
-- kind + oid + subid однозначно называют объект каталога: rel/oid, col/attrelid/attnum,
-- con/oid, proc/oid, trg/oid, typ/oid, stx/oid, nsp/oid, db/oid.
create temp table stage_node (
    kind     text          not null,
    oid      oid           not null,
    subid    int           not null default 0,
    surface  {schema}.surface_e  not null,
    address  jsonb         not null,
    primary key (kind, oid, subid),
    unique (address)
);

create temp table stage_tree (
    node_address    jsonb not null,
    parent_address  jsonb,
    primary key (node_address)
);

create temp table stage_edge (
    src_address  jsonb not null,
    tgt_address  jsonb not null,
    role         text,
    side         smallint,
    ordinal      smallint,
    is_key       bool,
    unique nulls not distinct (src_address, tgt_address, role, side, ordinal)
);
