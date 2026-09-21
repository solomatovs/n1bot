/*
pg-meta-scraper, схема, шаг 1: строки словаря {schema}.surface и формулы ссылок
{schema}.surface_url.
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

/*
Формулы ссылок: адрес объекта каталога это libpq URI до базы, а сам объект — роли
query-параметрами в порядке, в котором их объявляет адрес (docs/knowledge-graph-plan.md,
2.2.3). Колонка живёт и в таблице, и в представлении, ограничение — и в таблице, и в
типе: кусок без своей части адреса исчезает целиком.
*/
insert into {schema}.surface_url (surface, template, owner) values
    ('pg_meta_database',   '{{origin}}/{{database}}', 'pg-meta-scraper'),
    ('pg_meta_schema',     '{{origin}}/{{database}}?schema={{schema}}', 'pg-meta-scraper'),
    ('pg_meta_table',      '{{origin}}/{{database}}?schema={{schema}}&table={{table}}', 'pg-meta-scraper'),
    ('pg_meta_view',       '{{origin}}/{{database}}?schema={{schema}}&view={{view}}', 'pg-meta-scraper'),
    ('pg_meta_index',      '{{origin}}/{{database}}?schema={{schema}}&index={{index}}', 'pg-meta-scraper'),
    ('pg_meta_sequence',   '{{origin}}/{{database}}?schema={{schema}}&sequence={{sequence}}', 'pg-meta-scraper'),
    ('pg_meta_type',       '{{origin}}/{{database}}?schema={{schema}}&type={{type}}', 'pg-meta-scraper'),
    ('pg_meta_statistics', '{{origin}}/{{database}}?schema={{schema}}&statistics={{statistics}}', 'pg-meta-scraper'),
    ('pg_meta_routine',    '{{origin}}/{{database}}?schema={{schema}}&function={{function}}&args={{args}}', 'pg-meta-scraper'),
    ('pg_meta_trigger',    '{{origin}}/{{database}}?schema={{schema}}&table={{table}}&trigger={{trigger}}', 'pg-meta-scraper'),
    ('pg_meta_column',     '{{origin}}/{{database}}?schema={{schema}}[&table={{table}}][&view={{view}}]&column={{column}}', 'pg-meta-scraper'),
    ('pg_meta_constraint', '{{origin}}/{{database}}?schema={{schema}}[&table={{table}}][&type={{type}}]&constraint={{constraint}}', 'pg-meta-scraper')
on conflict (surface) do update set template = excluded.template, owner = excluded.owner;
