/*
ix-core, схема, шаг 0: расширения, схема графа и пустой тип surface_e.
*/
create extension if not exists pg_trgm;
create extension if not exists vector;
create extension if not exists btree_gin;

create schema if not exists {schema};
do $$ begin
    create type {schema}.surface_e as enum ();
exception when duplicate_object then null; end $$;


comment on type {schema}.surface_e is
'surface name: имя surface-таблицы, в которой лежат атрибуты (properties graph).
Значения в этот enum могут только добавляться (alter type add value if not exists) или переименоваться
Удаление из enum в postgres невозможно. для этого требуется создание нового enum
';
