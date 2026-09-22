/*
ix-core, схема, шаг 2: узлы {schema}.node и их индексы.
*/
create table if not exists {schema}.node (
    id          bigserial primary key,
    surface     {schema}.surface_e not null references {schema}.surface,
    address     jsonb not null,
    created_at  timestamptz not null default now()
);

/*
Поиск node по адресу: containment по jsonb, части адреса перечисляются объектом.
Все node с указанными частями:
select id from {schema}.node
where
    address @> jsonb_build_object(
        'host', 'dwh.local', 'port', 5432, 'database', 'dwh',
        'schema', 'dm', 'table', 'fact_orders'
    );

Все адреса postgresql:
    address @> jsonb_build_object('scheme', 'postgresql')

Все адреса с указанным host:
    address @> jsonb_build_object('host', 'dwh.local')
*/
create unique index if not exists node__address__uk on {schema}.node using btree (address);
create index if not exists node__address__gin on {schema}.node using gin (address jsonb_path_ops);
create index if not exists node__surface on {schema}.node using btree (surface);
