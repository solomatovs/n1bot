/*
pg-indexer-fts, схема: аспекты PostgreSQL (общий словарь, создаётся идемпотентно каждым
индексатором) и таблица ix.pg_fts с индексами. Предусловие: ядро ix из
docs/knowledge-schema.sql. Внешнего ключа на ix.node нет намеренно.
*/
do $$ begin
    create type ix.pg_aspect_e as enum ();
exception when duplicate_object then null; end $$;

alter type ix.pg_aspect_e add value if not exists 'description';
alter type ix.pg_aspect_e add value if not exists 'comment';
alter type ix.pg_aspect_e add value if not exists 'columns';
alter type ix.pg_aspect_e add value if not exists 'summary';
alter type ix.pg_aspect_e add value if not exists 'name';
alter type ix.pg_aspect_e add value if not exists 'path';
alter type ix.pg_aspect_e add value if not exists 'words';

comment on type ix.pg_aspect_e is
    'Какой текст объекта PostgreSQL закодирован в строке поисковой таблицы. Значения только добавляются или переименовываются: на них ссылаются предикаты частичных индексов, и они следуют за переименованием.';

create table if not exists ix.pg_aspect (
    aspect       ix.pg_aspect_e primary key,
    description  varchar        not null
);

insert into ix.pg_aspect (aspect, description) values
    ('description', 'описание объекта, собранное индексатором из всего, что о нём известно; основной аспект поиска'),
    ('comment',     'комментарий из источника как есть (obj_description, col_description); пишется, только если не пуст'),
    ('columns',     'имена колонок таблицы через пробел; таблица находится по своим колонкам'),
    ('summary',     'описание от LLM (плагин describer); пишется, только когда оно есть'),
    ('name',        'имя объекта как есть (relname, attname); точное совпадение и подстрока'),
    ('path',        'путь через точку, как пишет пользователь: schema.table или schema.table.column; точное совпадение'),
    ('words',       'слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е; поиск с опечатками')
on conflict (aspect) do nothing;

create table if not exists ix.pg_fts (
    node_id    bigint   not null,
    surface       ix.surface_e not null references ix.surface,
    aspect     ix.pg_aspect_e not null references ix.pg_aspect,
    content    varchar  not null,
    tsv        tsvector not null,
    primary key (node_id, surface, aspect)
);

/*
Конфигурация russian стеммит и русский, и английский: order/orders,
заказ/заказы. Простой запрос без суммирования по node:

select node_id, surface, ts_rank_cd(tsv, q) as rank
from   ix.pg_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
where  tsv @@ q
order by rank desc
limit  20;

Один GIN по surface и tsv (btree_gin) обслуживает оба случая: запрос без
фильтра по виду идёт по нему же, запрос с фильтром по редкому виду
отбирает вид внутри индекса. Для частого вида планировщик сам оставляет
surface обычным фильтром после индекса: это дешевле, чем читать его список
из GIN.
*/
create index if not exists pg_fts__surface_tsv__gin on ix.pg_fts using gin (surface, tsv);
