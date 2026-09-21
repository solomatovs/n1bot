/*
cfl-indexer, схема, шаг 0: значения surface_e и aspect_e, которыми владеет индексатор
Confluence. Отдельным файлом: использовать значения enum можно только после коммита,
словарь и объявления идут следующими файлами.
*/
alter type {schema}.surface_e add value if not exists 'cfl_space';
alter type {schema}.surface_e add value if not exists 'cfl_page';
alter type {schema}.surface_e add value if not exists 'cfl_blogpost';
alter type {schema}.surface_e add value if not exists 'cfl_attachment';
alter type {schema}.surface_e add value if not exists 'cfl_comment';
alter type {schema}.surface_e add value if not exists 'cfl_page_link';

alter type {schema}.aspect_e add value if not exists 'title';
alter type {schema}.aspect_e add value if not exists 'path';
alter type {schema}.aspect_e add value if not exists 'words';
alter type {schema}.aspect_e add value if not exists 'labels';
alter type {schema}.aspect_e add value if not exists 'card';
alter type {schema}.aspect_e add value if not exists 'body';
alter type {schema}.aspect_e add value if not exists 'ocr';
