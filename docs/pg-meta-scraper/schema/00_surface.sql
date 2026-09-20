/*
pg-meta-scraper, схема, шаг 0: поверхности, которыми владеет скрапер.
Предусловие: ядро ix из docs/knowledge-schema.sql (schema ix, тип ix.surface_e, таблицы
ix.surface, ix.node, ix.tree, ix.edge). Все команды идемпотентны.
*/
alter type ix.surface_e add value if not exists 'pg_meta_database';
alter type ix.surface_e add value if not exists 'pg_meta_schema';
alter type ix.surface_e add value if not exists 'pg_meta_table';
alter type ix.surface_e add value if not exists 'pg_meta_column';
alter type ix.surface_e add value if not exists 'pg_meta_view';
alter type ix.surface_e add value if not exists 'pg_meta_index';
alter type ix.surface_e add value if not exists 'pg_meta_sequence';
alter type ix.surface_e add value if not exists 'pg_meta_routine';
alter type ix.surface_e add value if not exists 'pg_meta_constraint';
alter type ix.surface_e add value if not exists 'pg_meta_trigger';
alter type ix.surface_e add value if not exists 'pg_meta_type';
alter type ix.surface_e add value if not exists 'pg_meta_statistics';

insert into ix.surface (name, description) values
    ('pg_meta_database',           'База данных источника PostgreSQL или Greenplum; корень tree источника.'),
    ('pg_meta_schema',             'Схема базы данных.'),
    ('pg_meta_table',              'Таблица: обычная, секционированная, секция, внешняя.'),
    ('pg_meta_column',             'Колонка таблицы, представления или материализованного представления.'),
    ('pg_meta_view',               'Представление или материализованное представление.'),
    ('pg_meta_index',              'Индекс, в том числе индекс на секционированной таблице и его копии на секциях.'),
    ('pg_meta_sequence',           'Последовательность.'),
    ('pg_meta_routine',            'Функция, процедура, агрегат или оконная функция; каждая перегрузка отдельный node, args в адресе.'),
    ('pg_meta_constraint',         'Ограничение таблицы или домена: primary key, unique, foreign key, check, exclusion, not null (с PostgreSQL 18).'),
    ('pg_meta_trigger',            'Триггер таблицы; системные триггеры FK не индексируются.'),
    ('pg_meta_type',               'Пользовательский тип: домен, enum, составной, range.'),
    ('pg_meta_statistics',         'Расширенная статистика (create statistics).')
on conflict (name) do nothing;
