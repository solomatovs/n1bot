/*
pg-llm-describer, схема: описания объектов от LLM. content это текст описания, input_hash это
md5 текста, который ушёл в модель (структура объекта из ix), indexer_hash это md5 модели,
системного промпта, шаблона входа и схемы ответа: смена любого из них переводит все строки
в очередь. Внешнего ключа на {schema}.node нет намеренно.
*/
create table if not exists {schema}.pg_llm_description (
    node_id       bigint       not null primary key,
    surface       {schema}.surface_e not null references {schema}.surface,
    content       varchar      not null,
    input_hash    varchar      not null,
    indexer_hash  varchar      not null,
    created_at    timestamptz  not null default now()
);
