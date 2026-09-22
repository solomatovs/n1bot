/*
ora-meta-scraper, схема, шаг 2: роли рёбер. У ребра Oracle, как у PostgreSQL, есть
позиция колонки в списке индекса, constraint'а или ключа партиционирования, поэтому
строка роли повторяет pg_meta_edge: (edge_id, role, side, ordinal, is_key). У ролей без
позиции (dependency, synonym) side и ordinal равны 0.
*/
do $$ begin
    create type {schema}.ora_meta_edge_role_e as enum (
        'index', 'constraint', 'partition_key', 'dependency', 'synonym'
    );
exception when duplicate_object then null; end $$;

comment on type {schema}.ora_meta_edge_role_e is
'Роль ребра Oracle.
index — индекс перечисляет колонку на позиции ordinal.
constraint — ограничение перечисляет колонку: side = 0 свои колонки, side = 1 колонки constraint''а, на который ссылается FK.
partition_key — таблица перечисляет колонку ключа партиционирования.
dependency — src зависит от tgt по dependency$: представление, mview, подпрограмма или триггер от объекта.
synonym — синоним указывает на объект своей базы.';

create table if not exists {schema}.ora_meta_edge (
    edge_id  bigint not null references {schema}.edge on delete cascade,
    role     {schema}.ora_meta_edge_role_e not null,
    side     smallint not null,
    ordinal  smallint not null,
    is_key   boolean not null,
    primary key (edge_id, role, side, ordinal)
);
