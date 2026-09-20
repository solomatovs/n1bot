/*
pg-meta-scraper, схема, шаг 0a: значения aspect_e, которыми владеет скрапер.
Отдельным файлом по той же причине, что и значения surface_e.
*/
alter type {schema}.aspect_e add value if not exists 'meta_name';
alter type {schema}.aspect_e add value if not exists 'meta_path';
alter type {schema}.aspect_e add value if not exists 'meta_words';
alter type {schema}.aspect_e add value if not exists 'meta_description';
alter type {schema}.aspect_e add value if not exists 'meta_comment';
alter type {schema}.aspect_e add value if not exists 'meta_columns';
alter type {schema}.aspect_e add value if not exists 'meta_describer_input';
