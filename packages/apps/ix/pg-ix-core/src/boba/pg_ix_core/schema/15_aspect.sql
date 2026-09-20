/*
pg-ix-core, схема, шаг 1a: классы аспектов, пустой тип aspect_e, словарь аспектов
{schema}.aspect и объявления {schema}.surface_aspect.

Аспект — это текст, который извлекается из node поверхности и который индексаторы
кладут в свои таблицы. Значения aspect_e и строки словаря добавляет пакет-владелец
поверхности, ровно как со surface_e и {schema}.surface. Класс говорит потребителю,
что это за текст, и потребитель подписывается на классы, а не на поверхности.
*/
do $$ begin
    create type {schema}.aspect_class_e as enum (
        'ident', 'words', 'description', 'describer_input'
    );
exception when duplicate_object then null; end $$;

comment on type {schema}.aspect_class_e is
'Класс аспекта, на который подписывается потребитель.
ident — короткий идентификатор: имя, путь; точное совпадение и префикс.
words — имя, разрезанное на слова, для поиска с опечатками.
description — проза: заголовок, комментарий, состав колонок, описание от модели.
describer_input — материал для генерации описания, вход описателя.';

do $$ begin
    create type {schema}.aspect_e as enum ();
exception when duplicate_object then null; end $$;

comment on type {schema}.aspect_e is
'Имя аспекта. Значения только добавляются владельцем поверхности (alter type add value
if not exists) или переименовываются; удалить значение enum postgres не умеет.';

create table if not exists {schema}.aspect (
    aspect       {schema}.aspect_e primary key,
    class        {schema}.aspect_class_e not null,
    description  varchar not null,
    owner        varchar not null
);

/*
Объявление: как из поверхности получить текст аспекта. body — запрос, который
возвращает ровно две колонки: node_id bigint и content varchar; строки с пустым
content потребитель отбрасывает сам. Схема в теле стоит плейсхолдером {schema},
его подставляет потребитель при чтении; в файле владельца он пишется удвоенным,
как у sql.SQL.format. Накат любого пакета проверяет каждое тело запросом
select * from (<body>) s limit 0 и сверяет колонки с контрактом.
*/
create table if not exists {schema}.surface_aspect (
    surface  {schema}.surface_e not null references {schema}.surface,
    aspect   {schema}.aspect_e not null references {schema}.aspect,
    body     varchar not null,
    primary key (surface, aspect)
);
