/*
ix-fts, схема: таблица {schema}.ix_fts с индексами.
*/
create table if not exists {schema}.ix_fts (
    node_id  bigint not null,
    surface  {schema}.surface_e not null references {schema}.surface,
    aspect   {schema}.aspect_e not null references {schema}.aspect,
    content  varchar not null,
    tsv      tsvector not null,
    primary key (node_id, surface, aspect)
);

/*
Конфигурация russian стеммит и русский, и английский: order/orders,
заказ/заказы. Простой запрос без суммирования по node:

select node_id, surface, ts_rank_cd(tsv, q) as rank
from   {schema}.ix_fts, websearch_to_tsquery('russian', 'заказы клиентов') q
where  tsv @@ q
order by rank desc
limit  20;

Один GIN по surface и tsv (btree_gin) обслуживает оба случая: запрос без
фильтра по виду идёт по нему же, запрос с фильтром по редкому виду
отбирает вид внутри индекса. Для частого вида планировщик сам оставляет
surface обычным фильтром после индекса: это дешевле, чем читать его список
из GIN.
*/
create index if not exists ix_fts__surface_tsv__gin on {schema}.ix_fts using gin (surface, tsv);

/*
Таблица в реестре индексов ядра: поиск читает реестр и не знает имён таблиц. Модель и
размерность вектора в реестр не идут — они живут в конфиге владельца.
*/
insert into {schema}.index_table (kind, name, owner) values
    ('fts', 'ix_fts', 'ix-fts')
on conflict (kind, name) do update
    set owner = excluded.owner;
