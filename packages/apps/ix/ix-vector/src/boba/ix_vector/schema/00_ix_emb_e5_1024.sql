/*
ix-vector, схема: таблица {schema}.ix_emb_e5_1024. Текст аспекта длиннее окна
модели режется на чанки: chunk_no это номер куска, content его текст, content_hash это
md5 полного текста аспекта, общий для всех его чанков; по нему очередь понимает, что
аспект пересчитывать не надо. Внешнего ключа на {schema}.node нет намеренно.
*/
create extension if not exists vector;

create table if not exists {schema}.ix_emb_e5_1024 (
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
from   {schema}.ix_emb_e5_1024
where  surface = 'pg_meta_table' and aspect = 'meta_description'
order by dist
limit  20;
*/

/*
Таблица в реестре индексов ядра: поиск читает реестр и не знает имён таблиц. Модель и
размерность вектора в реестр не идут — они живут в конфиге владельца.
*/
insert into {schema}.index_table (kind, name, owner) values
    ('vector', 'ix_emb_e5_1024', 'ix-vector')
on conflict (kind, name) do update
    set owner = excluded.owner;
