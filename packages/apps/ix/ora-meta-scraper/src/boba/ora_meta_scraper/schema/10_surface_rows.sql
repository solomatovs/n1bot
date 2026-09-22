/*
ora-meta-scraper, схема, шаг 1: строки словаря {schema}.surface и формулы ссылок
{schema}.surface_url.
*/
insert into {schema}.surface (name, description) values
    ('ora_meta_database',   'База Oracle: сервис (PDB или экземпляр), к которому идёт соединение; корень tree источника.'),
    ('ora_meta_schema',     'Схема: пользователь-владелец объектов, не принадлежащий Oracle.'),
    ('ora_meta_table',      'Таблица, в том числе партиционированная, временная и index-organized; контейнеры mview не снимаются.'),
    ('ora_meta_view',       'Представление.'),
    ('ora_meta_mview',      'Материализованное представление: объект и его контейнерная таблица одной node.'),
    ('ora_meta_column',     'Колонка таблицы, представления или mview; скрытые колонки не снимаются.'),
    ('ora_meta_constraint', 'Ограничение таблицы или представления: check, primary, unique, foreign, view check option, read only.'),
    ('ora_meta_index',      'Индекс таблицы или mview, кроме индексов LOB.'),
    ('ora_meta_sequence',   'Последовательность.'),
    ('ora_meta_synonym',    'Синоним схемы; ребро synonym ведёт на объект своей базы.'),
    ('ora_meta_trigger',    'Триггер таблицы или представления.'),
    ('ora_meta_routine',    'Подпрограмма схемы: процедура, функция, пакет или тип; тела и аргументы не снимаются.')
on conflict (name) do nothing;

/*
Формулы ссылок: адрес объекта это oracle://host:port/service, схема и объект ролями
query-параметрами. Колонка живёт в таблице, представлении и mview, индекс в таблице и
mview, constraint и триггер в таблице и представлении; кусок без своей части адреса
исчезает целиком.
*/
insert into {schema}.surface_url (surface, template, owner) values
    ('ora_meta_database',   '{{origin}}/{{database}}', 'ora-meta-scraper'),
    ('ora_meta_schema',     '{{origin}}/{{database}}?schema={{schema}}', 'ora-meta-scraper'),
    ('ora_meta_table',      '{{origin}}/{{database}}?schema={{schema}}&table={{table}}', 'ora-meta-scraper'),
    ('ora_meta_view',       '{{origin}}/{{database}}?schema={{schema}}&view={{view}}', 'ora-meta-scraper'),
    ('ora_meta_mview',      '{{origin}}/{{database}}?schema={{schema}}&mview={{mview}}', 'ora-meta-scraper'),
    ('ora_meta_column',     '{{origin}}/{{database}}?schema={{schema}}[&table={{table}}][&view={{view}}][&mview={{mview}}]&column={{column}}', 'ora-meta-scraper'),
    ('ora_meta_constraint', '{{origin}}/{{database}}?schema={{schema}}[&table={{table}}][&view={{view}}]&constraint={{constraint}}', 'ora-meta-scraper'),
    ('ora_meta_index',      '{{origin}}/{{database}}?schema={{schema}}[&table={{table}}][&mview={{mview}}]&index={{index}}', 'ora-meta-scraper'),
    ('ora_meta_sequence',   '{{origin}}/{{database}}?schema={{schema}}&sequence={{sequence}}', 'ora-meta-scraper'),
    ('ora_meta_synonym',    '{{origin}}/{{database}}?schema={{schema}}&synonym={{synonym}}', 'ora-meta-scraper'),
    ('ora_meta_trigger',    '{{origin}}/{{database}}?schema={{schema}}[&table={{table}}][&view={{view}}]&trigger={{trigger}}', 'ora-meta-scraper'),
    ('ora_meta_routine',    '{{origin}}/{{database}}?schema={{schema}}&routine={{routine}}', 'ora-meta-scraper')
on conflict (surface) do update set template = excluded.template, owner = excluded.owner;
