/*
pg-idx-vector, схема: таблица {schema}.pg_idx_emb_e5_1024 и частичные HNSW-индексы по парам
surface + aspect. Текст аспекта длиннее окна модели режется на чанки: chunk_no это номер
куска, content его текст, content_hash это md5 полного текста аспекта, общий для всех его
чанков; по нему очередь понимает, что аспект пересчитывать не надо. Предусловие: ядро ix из docs/knowledge-schema.sql, значения surface_e из
packages/apps/ix/pg-meta-scraper/src/boba/pg_meta_scraper/schema/00_surface.sql, словарь аспектов из схемы любого индексатора
(pg-idx-fts или pg-idx-trgm). Внешнего ключа на {schema}.node нет намеренно.
*/
create extension if not exists vector;

create table if not exists {schema}.pg_idx_emb_e5_1024 (
    node_id       bigint         not null,
    surface       {schema}.surface_e   not null references {schema}.surface,
    aspect        {schema}.pg_idx_aspect_e not null references {schema}.pg_idx_aspect,
    chunk_no      smallint       not null,
    content       varchar        not null,
    content_hash  varchar        not null,
    emb           halfvec(1024)  not null,
    primary key (node_id, surface, aspect, chunk_no)
);

/*
Частичный HNSW на каждую существующую пару surface + aspect. HNSW отдаёт
k ближайших из своего индекса, и фильтр по общему индексу после обхода
усекал бы выдачу; с частичными индексами фильтр по surface и aspect попадает
в свой индекс. surface и aspect в предикате это значения enum, они следуют
за переименованием в словаре.

select node_id, emb <=> $1::halfvec(1024) as dist
from   {schema}.pg_idx_emb_e5_1024
where  surface = 'pg_meta_table' and aspect = 'meta_description'
order by dist
limit  20;
*/
create index if not exists pg_idx_emb_e5_1024__pg_meta_database_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_database' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_database_meta_comment__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_database' and aspect = 'meta_comment';
create index if not exists pg_idx_emb_e5_1024__pg_meta_meta_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_schema' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_schema_meta_comment__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_schema' and aspect = 'meta_comment';
create index if not exists pg_idx_emb_e5_1024__pg_meta_table_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_table' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_table_meta_comment__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_table' and aspect = 'meta_comment';
create index if not exists pg_idx_emb_e5_1024__pg_meta_table_meta_columns__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_table' and aspect = 'meta_columns';
create index if not exists pg_idx_emb_e5_1024__pg_meta_table_llm_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_table' and aspect = 'llm_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_column_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_column' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_column_meta_comment__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_column' and aspect = 'meta_comment';
create index if not exists pg_idx_emb_e5_1024__pg_meta_column_llm_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_column' and aspect = 'llm_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_view_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_view' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_view_meta_comment__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_view' and aspect = 'meta_comment';
create index if not exists pg_idx_emb_e5_1024__pg_meta_view_meta_columns__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_view' and aspect = 'meta_columns';
create index if not exists pg_idx_emb_e5_1024__pg_meta_view_llm_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_view' and aspect = 'llm_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_index_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_index' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_sequence_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_sequence' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_sequence_meta_comment__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_sequence' and aspect = 'meta_comment';
create index if not exists pg_idx_emb_e5_1024__pg_meta_routine_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_routine' and aspect = 'meta_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_routine_meta_comment__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_routine' and aspect = 'meta_comment';
create index if not exists pg_idx_emb_e5_1024__pg_meta_routine_llm_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_routine' and aspect = 'llm_description';
create index if not exists pg_idx_emb_e5_1024__pg_meta_constraint_description__hnsw
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'pg_meta_constraint' and aspect = 'meta_description';
