/*
pg-ix-core, схема, шаг 4: рёбра {schema}.edge и их индексы.
*/
create table if not exists {schema}.edge (
    id          bigserial       not null primary key,
    node_src_id bigint          not null references {schema}.node on delete cascade,
    node_tgt_id bigint          not null references {schema}.node on delete cascade,
    surface     {schema}.surface_e    not null references {schema}.surface,
    weight      real            not null check (weight between 0 and 1)
);

create unique   index if not exists edge__uk                    on {schema}.edge using btree (node_src_id, node_tgt_id);
create          index if not exists edge__tgt_src_surface       on {schema}.edge using btree (node_tgt_id, node_src_id, surface) include (weight);
create          index if not exists edge__surface_src           on {schema}.edge using btree (surface, node_src_id);
