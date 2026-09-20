/*
pg-idx-fts, схема, шаг 1: словарь аспектов и таблица {schema}.pg_idx_fts с индексами.
*/
create table if not exists {schema}.pg_idx_aspect (
    aspect       {schema}.pg_idx_aspect_e primary key,
    description  varchar not null
);

insert into {schema}.pg_idx_aspect (aspect, description) values
    ('meta_description', 'описание объекта, собранное индексатором из всего, что о нём известно; основной аспект поиска'),
    ('meta_comment',     'комментарий из источника как есть (obj_description, col_description); пишется, только если не пуст'),
    ('meta_columns',     'имена колонок таблицы через пробел; таблица находится по своим колонкам'),
    ('llm_description',     'описание от LLM (плагин describer); пишется, только когда оно есть'),
    ('meta_name',        'имя объекта как есть (relname, attname); точное совпадение и подстрока'),
    ('meta_path',        'путь через точку, как пишет пользователь: schema.table или schema.table.column; точное совпадение'),
    ('meta_words',       'слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е; поиск с опечатками')
on conflict (aspect) do nothing;

create table if not exists {schema}.pg_idx_fts (
    node_id  bigint not null,
    surface  {schema}.surface_e not null references {schema}.surface,
    aspect   {schema}.pg_idx_aspect_e not null references {schema}.pg_idx_aspect,
    content  varchar not null,
    tsv      tsvector not null,
    primary key (node_id, surface, aspect)
);

/*
Конфигурация russian стеммит и русский, и английский: order/orders,
заказ/заказы. Простой запрос без суммирования по node:

select node_id, surface, ts_rank_cd(tsv, q) as rank
from   {schema}.pg_idx_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
where  tsv @@ q
order by rank desc
limit  20;

Один GIN по surface и tsv (btree_gin) обслуживает оба случая: запрос без
фильтра по виду идёт по нему же, запрос с фильтром по редкому виду
отбирает вид внутри индекса. Для частого вида планировщик сам оставляет
surface обычным фильтром после индекса: это дешевле, чем читать его список
из GIN.
*/
create index if not exists pg_idx_fts__surface_tsv__gin on {schema}.pg_idx_fts using gin (surface, tsv);
