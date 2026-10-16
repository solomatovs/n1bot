create table if not exists {schema}.node (
    id          bigserial primary key,
    scope_kind  varchar not null,
    scope_id    uuid not null,
    kind        varchar not null,
    address     jsonb not null,
    url_address varchar not null,
    description varchar not null,
    s__wrt_ts   timestamptz not null default now()
);

create unique index if not exists node_uk
    on {schema}.node (scope_id, address);

create index if not exists node_address_gin
    on {schema}.node using gin (address jsonb_path_ops);

create index if not exists node_kind_btree on {schema}.node (kind);

create table if not exists {schema}.edge (
    id          bigserial primary key,
    source_id   bigint not null references {schema}.node on delete cascade,
    target_id   bigint not null references {schema}.node on delete cascade,
    kind        varchar not null,
    description varchar not null,
    s__wrt_ts   timestamptz not null default now(),
    unique (source_id, target_id, kind)
);
