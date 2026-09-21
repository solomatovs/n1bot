-- Стадия: node с адресами и ключами каталога, по которым строятся tree и edge.
-- kind + database + relation + name однозначно называют объект: srv, db/database,
-- rel/database/name, dict/database/name, col/database/relation/name, idx и proj так же,
-- fn/name. Пустая строка вместо отсутствующей части.
create temp table stage_node (
    kind      text                not null,
    database  text                not null default '',
    relation  text                not null default '',
    name      text                not null default '',
    surface   {schema}.surface_e  not null,
    address   jsonb               not null,
    primary key (kind, database, relation, name),
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
    role         text  not null,
    unique (src_address, tgt_address, role)
);
