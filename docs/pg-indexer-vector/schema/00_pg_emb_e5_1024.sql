/*
pg-indexer-vector, схема: таблица ix.pg_emb_e5_1024 и частичные HNSW-индексы по парам
surface + aspect. Предусловие: ядро ix из docs/knowledge-schema.sql, значения surface_e из
docs/pg-scraper/schema/00_surface.sql, словарь аспектов из схемы любого индексатора
(pg-indexer-fts или pg-indexer-trgm). Внешнего ключа на ix.node нет намеренно.
*/
create extension if not exists vector;

create table if not exists ix.pg_emb_e5_1024 (
    node_id    bigint   not null,
    surface          ix.surface_e not null references ix.surface,
    aspect        ix.pg_aspect_e not null references ix.pg_aspect,
    content       varchar       not null,
    emb           halfvec(1024) not null,
    primary key (node_id, surface, aspect)
);

/*
Частичный HNSW на каждую существующую пару surface + aspect. HNSW отдаёт
k ближайших из своего индекса, и фильтр по общему индексу после обхода
усекал бы выдачу; с частичными индексами фильтр по surface и aspect попадает
в свой индекс. surface и aspect в предикате это значения enum, они следуют
за переименованием в словаре.

select node_id, emb <=> $1::halfvec(1024) as dist
from   ix.pg_emb_e5_1024
where  surface = 'pg_table' and aspect = 'description'
order by dist
limit  20;
*/
create index if not exists pg_emb_e5_1024__pg_database_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_database' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_database_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_database' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_schema_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_schema' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_schema_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_schema' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_table_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_table' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_table_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_table' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_table_columns__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_table' and aspect = 'columns';
create index if not exists pg_emb_e5_1024__pg_table_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_table' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_column_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_column' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_column_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_column' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_column_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_column' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_view_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_view' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_view_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_view' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_view_columns__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_view' and aspect = 'columns';
create index if not exists pg_emb_e5_1024__pg_view_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_view' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_index_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_index' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_sequence_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_sequence' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_sequence_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_sequence' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_routine_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_routine' and aspect = 'description';
create index if not exists pg_emb_e5_1024__pg_routine_comment__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_routine' and aspect = 'comment';
create index if not exists pg_emb_e5_1024__pg_routine_summary__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_routine' and aspect = 'summary';
create index if not exists pg_emb_e5_1024__pg_constraint_description__hnsw
    on ix.pg_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_constraint' and aspect = 'description';
