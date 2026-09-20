/*
pg-meta-scraper, схема, шаг 1: строки словаря {schema}.surface.
*/
insert into {schema}.surface (name, description) values
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
