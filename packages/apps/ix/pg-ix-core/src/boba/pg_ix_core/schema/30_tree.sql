/*
pg-ix-core, схема, шаг 3: дерево {schema}.tree и его индексы.
*/
create table if not exists {schema}.tree (
    id          bigserial not null primary key,
    node_id     bigint not null references {schema}.node on delete cascade,
    parent_id   bigint null references {schema}.node on delete cascade,
    created_at  timestamptz not null default now()
);
create unique index if not exists tree__uk on {schema}.tree using btree (node_id, parent_id);

/*
Выбрать всех детей:
    select node_id from {schema}.tree where parent_id = $1

Выбрать всех корневых родителей:
    select parent_id from {schema}.tree where parent_id is null

Выбрать все поддерево:
    with recursive sub as (
        select $1::bigint as id
        union all
        select t.node_id from {schema}.tree t join sub on t.parent_id = sub.id)
    select id from sub

Отдельно стоит отметить что при удалении в node объектов
сработает cascade delete который удалит его строки и в tree однако дети остануться.
Поэтому для удаления всего поддерева индексатор должен это сделать
отдельным рекурсивным запросом
*/
create index if not exists tree__parent on {schema}.tree using btree (parent_id, node_id);
