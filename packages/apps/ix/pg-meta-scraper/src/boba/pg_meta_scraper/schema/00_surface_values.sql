/*
pg-meta-scraper, схема, шаг 0: значения surface_e, которыми владеет скрапер.
Значения отдельным файлом: использовать их можно только после коммита,
поэтому строки словаря идут следующим файлом.
Предусловие: ядро ix из docs/knowledge-schema.sql (schema ix, тип {schema}.surface_e, таблицы
{schema}.surface, {schema}.node, {schema}.tree, {schema}.edge). Все команды идемпотентны.
*/
alter type {schema}.surface_e add value if not exists 'pg_meta_database';
alter type {schema}.surface_e add value if not exists 'pg_meta_schema';
alter type {schema}.surface_e add value if not exists 'pg_meta_table';
alter type {schema}.surface_e add value if not exists 'pg_meta_column';
alter type {schema}.surface_e add value if not exists 'pg_meta_view';
alter type {schema}.surface_e add value if not exists 'pg_meta_index';
alter type {schema}.surface_e add value if not exists 'pg_meta_sequence';
alter type {schema}.surface_e add value if not exists 'pg_meta_routine';
alter type {schema}.surface_e add value if not exists 'pg_meta_constraint';
alter type {schema}.surface_e add value if not exists 'pg_meta_trigger';
alter type {schema}.surface_e add value if not exists 'pg_meta_type';
alter type {schema}.surface_e add value if not exists 'pg_meta_statistics';
