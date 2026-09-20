/*
pg-llm-describer, шаг 2: записать описание одного объекта. input_hash и indexer_hash это те,
с которыми оно посчитано; если структура изменилась, пока отвечала модель, следующая
очередь выдаст объект снова.
*/
-- @name write
-- @params node_id surface content input_hash indexer_hash
insert into {schema}.pg_llm_description
    (node_id, surface, content, input_hash, indexer_hash)
values
    (
        %(node_id)s,
        %(surface)s::{schema}.surface_e,
        %(content)s,
        %(input_hash)s,
        %(indexer_hash)s
    )
on conflict (node_id) do update
    set surface      = excluded.surface,
        content      = excluded.content,
        input_hash   = excluded.input_hash,
        indexer_hash = excluded.indexer_hash,
        created_at   = now();
