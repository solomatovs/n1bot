/*
pg-ix-core, схема, шаг 2: узлы {schema}.node и их индексы.
*/
create table if not exists {schema}.node (
    id          bigserial       primary key,
    surface     {schema}.surface_e    not null references {schema}.surface,
    address     jsonb           not null,
    created_at  timestamptz     not null default now()
);

/*
Поиск node по адресу:
select id from {schema}.node
where
поиск всех node с указанными частями
    address @> '{"host":"dwh.local","port":5432,"database":"dwh","schema":"dm","table":"fact_orders"}';

поиск всех адресов postgresql
    address @> '{"scheme": "postgresql"}

поиск всех адресов с укзаанным host
    address @> '{"host": "dwh.local"}'
*/
create unique index if not exists node__address__uk on {schema}.node using btree (address);
create index if not exists node__address__gin on {schema}.node using gin (address jsonb_path_ops);
create index if not exists node__surface on {schema}.node using btree (surface);
