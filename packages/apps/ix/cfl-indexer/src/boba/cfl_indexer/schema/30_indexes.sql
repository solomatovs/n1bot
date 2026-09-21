/*
cfl-indexer, схема, шаг 3: три таблицы индексов поверхностей Confluence. Устроены как
pg_idx_*: колонка aspect ссылается на словарь ядра, внешнего ключа на {schema}.node нет
намеренно, строки удалённых node снимает сам индексатор при чистке спейса.
*/
create table if not exists {schema}.cfl_idx_trgm (
    node_id  bigint not null,
    surface  {schema}.surface_e not null references {schema}.surface,
    aspect   {schema}.aspect_e not null references {schema}.aspect,
    content  varchar not null,
    primary key (node_id, surface, aspect)
);
create index if not exists cfl_idx_trgm__content__gist on {schema}.cfl_idx_trgm using gist (content gist_trgm_ops);
create index if not exists cfl_idx_trgm__aspect_lower_content on {schema}.cfl_idx_trgm using btree (aspect, lower(content));
create index if not exists cfl_idx_trgm__aspect_lower_content__prefix
    on {schema}.cfl_idx_trgm using btree (aspect, lower(content) varchar_pattern_ops);
create index if not exists cfl_idx_trgm__surface_aspect on {schema}.cfl_idx_trgm using btree (surface, aspect);

create table if not exists {schema}.cfl_idx_fts (
    node_id  bigint not null,
    surface  {schema}.surface_e not null references {schema}.surface,
    aspect   {schema}.aspect_e not null references {schema}.aspect,
    content  varchar not null,
    tsv      tsvector not null,
    primary key (node_id, surface, aspect)
);
create index if not exists cfl_idx_fts__surface_tsv__gin on {schema}.cfl_idx_fts using gin (surface, tsv);

/*
Текст длиннее окна модели режется на чанки: chunk_no это номер куска, content_hash это
md5 полного текста аспекта, общий для всех его чанков. Частичный HNSW на каждую пару
surface + aspect, по которой ищут: пары известны пакету, поэтому индексы здесь.
*/
create table if not exists {schema}.cfl_idx_emb_e5_1024 (
    node_id       bigint not null,
    surface       {schema}.surface_e not null references {schema}.surface,
    aspect        {schema}.aspect_e not null references {schema}.aspect,
    chunk_no      smallint not null,
    content       varchar not null,
    content_hash  varchar not null,
    emb           halfvec(1024) not null,
    primary key (node_id, surface, aspect, chunk_no)
);
create index if not exists cfl_idx_emb_e5_1024__cfl_space_card__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_space' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_page_card__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_page' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_page_body__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_page' and aspect = 'body';
create index if not exists cfl_idx_emb_e5_1024__cfl_blogpost_card__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_blogpost' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_blogpost_body__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_blogpost' and aspect = 'body';
create index if not exists cfl_idx_emb_e5_1024__cfl_attachment_card__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_attachment' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_attachment_body__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_attachment' and aspect = 'body';
create index if not exists cfl_idx_emb_e5_1024__cfl_attachment_ocr__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_attachment' and aspect = 'ocr';
create index if not exists cfl_idx_emb_e5_1024__cfl_comment_card__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_comment' and aspect = 'card';
create index if not exists cfl_idx_emb_e5_1024__cfl_comment_body__hnsw
    on {schema}.cfl_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = 'cfl_comment' and aspect = 'body';
