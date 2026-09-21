/*
ch-meta-scraper, схема, шаг 1: строки словаря {schema}.surface и формулы ссылок
{schema}.surface_url.
*/
insert into {schema}.surface (name, description) values
    ('ch_meta_server',     'Сервер ClickHouse; корень tree источника: подключение идёт к серверу, а не к базе.'),
    ('ch_meta_database',   'База данных сервера.'),
    ('ch_meta_table',      'Таблица любого движка, кроме представлений и словарей; в том числе внутренние таблицы материализованных представлений.'),
    ('ch_meta_view',       'Представление: обычное, материализованное, live или window.'),
    ('ch_meta_column',     'Колонка таблицы, представления или словаря.'),
    ('ch_meta_index',      'Индекс пропуска данных (data skipping index) таблицы.'),
    ('ch_meta_projection', 'Проекция таблицы (с ClickHouse 24.4, когда появилась system.projections).'),
    ('ch_meta_dictionary', 'Словарь, объявленный DDL в базе; словари из xml-конфига не снимаются.'),
    ('ch_meta_function',   'SQL-функция пользователя (create function); объект сервера, базы у неё нет.')
on conflict (name) do nothing;

/*
Формулы ссылок: адрес объекта это clickhouse://host:port до сервера, база путём,
объект ролями query-параметрами. Колонка живёт в таблице, представлении и словаре,
кусок без своей части адреса исчезает целиком.
*/
insert into {schema}.surface_url (surface, template, owner) values
    ('ch_meta_server',     '{{origin}}', 'ch-meta-scraper'),
    ('ch_meta_database',   '{{origin}}/{{database}}', 'ch-meta-scraper'),
    ('ch_meta_table',      '{{origin}}/{{database}}?table={{table}}', 'ch-meta-scraper'),
    ('ch_meta_view',       '{{origin}}/{{database}}?view={{view}}', 'ch-meta-scraper'),
    ('ch_meta_dictionary', '{{origin}}/{{database}}?dictionary={{dictionary}}', 'ch-meta-scraper'),
    ('ch_meta_column',     '{{origin}}/{{database}}?[table={{table}}][view={{view}}][dictionary={{dictionary}}]&column={{column}}', 'ch-meta-scraper'),
    ('ch_meta_index',      '{{origin}}/{{database}}?table={{table}}&index={{index}}', 'ch-meta-scraper'),
    ('ch_meta_projection', '{{origin}}/{{database}}?table={{table}}&projection={{projection}}', 'ch-meta-scraper'),
    ('ch_meta_function',   '{{origin}}?function={{function}}', 'ch-meta-scraper')
on conflict (surface) do update set template = excluded.template, owner = excluded.owner;
