/*
ix-core, схема, шаг 1: словарь поверхностей {schema}.surface; строки добавляют пакеты-владельцы.
*/
create table if not exists {schema}.surface (
    name         {schema}.surface_e primary key,
    description  varchar not null
);
