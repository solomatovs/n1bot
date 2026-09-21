/*
ix-core, схема, шаг 1b: реестр таблиц поисковых индексов {schema}.index_table.

Таблицу индекса создаёт и наполняет её владелец (ix-trgm, ix-fts, ix-vector), а поиск
не должен знать её имя: он читает реестр нужного вида и опрашивает перечисленные
таблицы. Строку вписывает владелец своим файлом схемы, и новый вид индекса или новая
таблица появляются в поиске после его перезапуска, без правок запросов.

В реестре только то, что нужно обеим сторонам: вид индекса, имя таблицы и владелец.
Специфика вида — модель эмбеддинга, размерность, языковая конфигурация полнотекста —
живёт у владельца, в его конфиге и его схеме; ядру она не нужна и здесь её нет.
*/
do $$ begin
    create type {schema}.index_kind_e as enum ('trgm', 'fts', 'vector');
exception when duplicate_object then null; end $$;

comment on type {schema}.index_kind_e is
'Вид поискового индекса: trgm — подстроки и префиксы, fts — полнотекст, vector — эмбеддинги.';

create table if not exists {schema}.index_table (
    kind   {schema}.index_kind_e not null,
    name   varchar not null,
    owner  varchar not null,
    primary key (kind, name)
);

alter table {schema}.index_table drop constraint if exists index_table__vector_model;
alter table {schema}.index_table drop column if exists model;
alter table {schema}.index_table drop column if exists dim;
