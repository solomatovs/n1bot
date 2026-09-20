/*
pg-llm-describer, схема, шаг 1: описания объектов от LLM. content это текст описания, input_hash это
md5 текста, который ушёл в модель (структура объекта из ix), indexer_hash это md5 модели,
системного промпта, шаблона входа и схемы ответа: смена любого из них переводит все строки
в очередь. Внешнего ключа на {schema}.node нет намеренно. Аспект llm_description объявляется в словаре
здесь, а строки surface_aspect описатель добавляет при старте цикла для каждой поверхности,
у которой есть аспект класса describer_input (run/05_declare.sql).
*/
create table if not exists {schema}.pg_llm_description (
    node_id       bigint not null primary key,
    surface       {schema}.surface_e not null references {schema}.surface,
    content       varchar not null,
    input_hash    varchar not null,
    indexer_hash  varchar not null,
    created_at    timestamptz not null default now()
);

insert into {schema}.aspect (aspect, class, description, owner) values
    ('llm_description', 'description', 'Описание объекта от модели по его структуре (аспект класса describer_input).', 'pg-llm-describer')
on conflict (aspect) do nothing;
