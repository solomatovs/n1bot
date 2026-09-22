/*
ora-meta-scraper, схема, шаг 0: значения surface_e, которыми владеет скрапер.
Значения отдельным файлом: использовать их можно только после коммита,
поэтому строки словаря идут следующим файлом. Все команды идемпотентны.
*/
alter type {schema}.surface_e add value if not exists 'ora_meta_database';
alter type {schema}.surface_e add value if not exists 'ora_meta_schema';
alter type {schema}.surface_e add value if not exists 'ora_meta_table';
alter type {schema}.surface_e add value if not exists 'ora_meta_view';
alter type {schema}.surface_e add value if not exists 'ora_meta_mview';
alter type {schema}.surface_e add value if not exists 'ora_meta_column';
alter type {schema}.surface_e add value if not exists 'ora_meta_constraint';
alter type {schema}.surface_e add value if not exists 'ora_meta_index';
alter type {schema}.surface_e add value if not exists 'ora_meta_sequence';
alter type {schema}.surface_e add value if not exists 'ora_meta_synonym';
alter type {schema}.surface_e add value if not exists 'ora_meta_trigger';
alter type {schema}.surface_e add value if not exists 'ora_meta_routine';
