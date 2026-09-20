/*
pg-meta-scraper, схема, шаг 1: позиционные строки рёбер.
*/
do $$ begin
    create type ix.pg_meta_edge_role_e as enum ('index', 'constraint', 'partition_key', 'distribution_key', 'trigger', 'statistics');
exception when duplicate_object then null; end $$;

create table if not exists ix.pg_meta_edge (
    edge_id   bigint              not null references ix.edge on delete cascade,
    role      ix.pg_meta_edge_role_e   not null,
    side      smallint            not null,
    ordinal   smallint            not null,
    is_key    boolean             not null,
    primary key (edge_id, role, side, ordinal)
);
