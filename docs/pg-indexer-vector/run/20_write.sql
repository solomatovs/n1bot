/*
pg-indexer-vector, шаг 2: запись одного эмбеддинга. content это тот же текст, что был
выдан очередью: по нему следующая очередь решит, что строка актуальна. Если структура
изменилась, пока считалась модель, content уже другой, и строка попадёт в очередь снова.
*/
-- @name write
-- @params node_id surface aspect content emb
insert into ix.pg_emb_e5_1024 (node_id, surface, aspect, content, emb)
values (%(node_id)s, %(surface)s::ix.surface_e, %(aspect)s::ix.pg_aspect_e, %(content)s, %(emb)s::halfvec(1024))
on conflict (node_id, surface, aspect) do update set content = excluded.content, emb = excluded.emb
where pg_emb_e5_1024.content is distinct from excluded.content;
