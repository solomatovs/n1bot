/*
ix-llm-describer, схема, шаг 1: описания объектов от LLM.

Таблица называется llm_description без префикса происхождения: описатель работает по
объявлениям аспекта describer_input, а их даёт любой владелец поверхности — и скрапер
PostgreSQL, и индексатор Confluence. Прежнее имя pg_llm_description переименовывается
на месте, данные не теряются. content это текст описания, input_hash это
md5 текста, который ушёл в модель (структура объекта из ix), indexer_hash это md5 модели,
системного промпта, шаблона входа и схемы ответа: смена любого из них переводит все строки
в очередь. Внешнего ключа на {schema}.node нет намеренно. Аспект llm_description объявляется в словаре
здесь, а строки surface_aspect описатель добавляет при старте цикла для каждой поверхности,
у которой есть аспект класса describer_input (run/05_declare.sql).
*/
do $$ begin
    if to_regclass('{schema}.pg_llm_description') is not null
       and to_regclass('{schema}.llm_description') is null then
        alter table {schema}.pg_llm_description rename to llm_description;
    end if;
end $$;

create table if not exists {schema}.llm_description (
    node_id       bigint not null primary key,
    surface       {schema}.surface_e not null references {schema}.surface,
    content       varchar not null,
    input_hash    varchar not null,
    indexer_hash  varchar not null,
    created_at    timestamptz not null default now()
);

insert into {schema}.aspect (aspect, class, description, owner) values
    ('llm_description', 'description', 'Описание объекта от модели по его структуре (аспект класса describer_input).', 'ix-llm-describer')
on conflict (aspect) do nothing;
