/*
ch-meta-scraper, схема, шаг 2: роли рёбер. У ребра ClickHouse нет позиции в списке
колонок, каталог отдаёт только флаги is_in_* и списки зависимостей, поэтому строка
роли это ключ (edge_id, role): одно ребро таблица -> колонка несёт столько ролей, в
скольких ключах колонка участвует.
*/
do $$ begin
    create type {schema}.ch_meta_edge_role_e as enum (
        'partition_key', 'sorting_key', 'primary_key', 'sampling_key',
        'dependency', 'loading', 'target'
    );
exception when duplicate_object then null; end $$;

comment on type {schema}.ch_meta_edge_role_e is
'Роль ребра ClickHouse.
partition_key, sorting_key, primary_key, sampling_key — таблица перечисляет колонку своего ключа.
dependency — src зависит от tgt по system.tables.dependencies_*: материализованное представление над таблицей.
loading — src не загрузится без tgt по loading_dependencies_*: целевая таблица представления, таблица-источник словаря.
target — материализованное представление пишет в целевую таблицу (system.tables.target_*, с 26.6).';

create table if not exists {schema}.ch_meta_edge (
    edge_id  bigint not null references {schema}.edge on delete cascade,
    role     {schema}.ch_meta_edge_role_e not null,
    primary key (edge_id, role)
);
