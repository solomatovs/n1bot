/*
ix-core, схема, шаг 1d: промпты описания {schema}.surface_prompt.

Аспект класса describer_input отдаёт материал объекта, но как этот материал объяснять
модели, знает не описатель, а владелец поверхности: у таблицы PostgreSQL это структура
с колонками и ключами, у страницы Confluence это текст статьи. Поэтому промпт лежит
строкой на пару «поверхность, аспект», владелец кладёт её своим файлом схемы, а
описатель читает реестр и применяет.

system_prompt это роль и правила для модели, user_template это шаблон запроса с
подстановкой {{input}} вместо материала. Схема ответа общая для всех поверхностей и
живёт в пакете описателя: наружу всегда идёт один текст описания.

Пары без строки не описываются вовсе: описатель их не берёт в очередь, и это видно в
его логе. Общего промпта «на всякий случай» нет намеренно, иначе страница получила бы
правила, написанные для таблицы.
*/
create table if not exists {schema}.surface_prompt (
    surface        {schema}.surface_e not null,
    aspect         {schema}.aspect_e not null,
    system_prompt  varchar not null,
    user_template  varchar not null,
    owner          varchar not null,
    primary key (surface, aspect),
    foreign key (surface, aspect)
        references {schema}.surface_aspect (surface, aspect) on delete cascade
);
