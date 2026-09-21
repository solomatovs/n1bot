/*
ch-meta-scraper, схема, шаг 0: значения surface_e, которыми владеет скрапер.
Значения отдельным файлом: использовать их можно только после коммита,
поэтому строки словаря идут следующим файлом. Все команды идемпотентны.
*/
alter type {schema}.surface_e add value if not exists 'ch_meta_server';
alter type {schema}.surface_e add value if not exists 'ch_meta_database';
alter type {schema}.surface_e add value if not exists 'ch_meta_table';
alter type {schema}.surface_e add value if not exists 'ch_meta_view';
alter type {schema}.surface_e add value if not exists 'ch_meta_column';
alter type {schema}.surface_e add value if not exists 'ch_meta_index';
alter type {schema}.surface_e add value if not exists 'ch_meta_projection';
alter type {schema}.surface_e add value if not exists 'ch_meta_dictionary';
alter type {schema}.surface_e add value if not exists 'ch_meta_function';
