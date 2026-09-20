/*
pg-idx-vector, шаг 2: записать все чанки одного аспекта одним statement'ом. Чанки
приходят массивами одной длины: номера, тексты, векторы строками. Лишние старые чанки
(с номером не меньше нового числа) удаляются, остальные перезаписываются on conflict, так
что старый и новый набор никогда не смешиваются. content_hash это md5 полного текста
аспекта из очереди; если структура изменилась, пока считалась модель, хэш уже другой, и
следующая очередь выдаст аспект снова.
*/
-- @name write
-- @params node_id surface aspect content_hash chunk_count chunk_nos contents embs
with gone as (
    delete from {schema}.pg_idx_emb_e5_1024
    where
        node_id = %(node_id)s
        and surface = %(surface)s::{schema}.surface_e
        and aspect = %(aspect)s::{schema}.pg_idx_aspect_e
        and chunk_no >= %(chunk_count)s
    returning 1
)
insert into {schema}.pg_idx_emb_e5_1024
    (node_id, surface, aspect, chunk_no, content, content_hash, emb)
select
    %(node_id)s,
    %(surface)s::{schema}.surface_e,
    %(aspect)s::{schema}.pg_idx_aspect_e,
    c.chunk_no,
    c.content,
    %(content_hash)s,
    c.emb::halfvec(1024)
from
    unnest(
        %(chunk_nos)s::int[], %(contents)s::text[], %(embs)s::text[]
    ) as c(chunk_no, content, emb)
on conflict (node_id, surface, aspect, chunk_no) do update
    set content      = excluded.content,
        content_hash = excluded.content_hash,
        emb          = excluded.emb
where
    pg_idx_emb_e5_1024.content_hash is distinct from excluded.content_hash
    or pg_idx_emb_e5_1024.content is distinct from excluded.content;
