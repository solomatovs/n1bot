/*
pg-idx-vector, схема: таблица {schema}.pg_idx_emb_e5_1024. Текст аспекта длиннее окна
модели режется на чанки: chunk_no это номер куска, content его текст, content_hash это
md5 полного текста аспекта, общий для всех его чанков; по нему очередь понимает, что
аспект пересчитывать не надо. Внешнего ключа на {schema}.node нет намеренно.
*/
create extension if not exists vector;

create table if not exists {schema}.pg_idx_emb_e5_1024 (
    node_id       bigint not null,
    surface       {schema}.surface_e not null references {schema}.surface,
    aspect        {schema}.aspect_e not null references {schema}.aspect,
    chunk_no      smallint not null,
    content       varchar not null,
    content_hash  varchar not null,
    emb           halfvec(1024) not null,
    primary key (node_id, surface, aspect, chunk_no)
);

/*
Частичный HNSW на каждую пару surface + aspect, которую индексатор обслуживает. HNSW
отдаёт k ближайших из своего индекса, и фильтр по общему индексу после обхода усекал
бы выдачу; с частичными индексами фильтр по surface и aspect попадает в свой индекс.
Пары берутся из объявлений {schema}.surface_aspect, поэтому индексы создаёт воркер при
старте цикла файлом run/05_index.sql, а не этот файл.

select node_id, emb <=> $1::halfvec(1024) as dist
from   {schema}.pg_idx_emb_e5_1024
where  surface = 'pg_meta_table' and aspect = 'meta_description'
order by dist
limit  20;
*/
