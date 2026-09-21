/*
cfl-indexer, схема, шаг 2: поверхности Confluence. В них идентификаторы, метаданные и
хэши; ни тела страницы, ни файла здесь нет. content_hash это sha256 оригинала (HTML
страницы, байты файла), indexer_hash это md5 параметров преобразования и модели:
по ним индексатор решает, что переиндексировать. Ключ спейса и id контента уникальны
только внутри одного сервера Confluence, сервер задаёт адрес node, поэтому индексы по
ним не уникальные.
*/
create table if not exists {schema}.cfl_space (
    node_id       bigint primary key references {schema}.node on delete cascade,
    space_key     varchar not null,
    name          varchar not null,
    space_type    varchar not null,
    status        varchar not null,
    description   varchar not null,
    content_hash  varchar not null,
    indexer_hash  varchar not null
);
create index if not exists cfl_space__space_key on {schema}.cfl_space using btree (space_key);

create table if not exists {schema}.cfl_page (
    node_id          bigint primary key references {schema}.node on delete cascade,
    space_key        varchar not null,
    content_id       varchar not null,
    title            varchar not null,
    status           varchar not null,
    version          integer not null,
    created_at       timestamptz not null,
    updated_at       timestamptz not null,
    author           varchar not null,
    last_editor      varchar not null,
    labels           varchar[] not null,
    ancestor_titles  varchar[] not null,
    content_hash     varchar not null,
    indexer_hash     varchar not null
);
create index if not exists cfl_page__content_id on {schema}.cfl_page using btree (content_id);
create index if not exists cfl_page__space_key_title on {schema}.cfl_page using btree (space_key, title);

create table if not exists {schema}.cfl_blogpost (
    node_id       bigint primary key references {schema}.node on delete cascade,
    space_key     varchar not null,
    content_id    varchar not null,
    title         varchar not null,
    status        varchar not null,
    version       integer not null,
    created_at    timestamptz not null,
    updated_at    timestamptz not null,
    author        varchar not null,
    last_editor   varchar not null,
    labels        varchar[] not null,
    content_hash  varchar not null,
    indexer_hash  varchar not null
);
create index if not exists cfl_blogpost__content_id on {schema}.cfl_blogpost using btree (content_id);
create index if not exists cfl_blogpost__space_key on {schema}.cfl_blogpost using btree (space_key);

create table if not exists {schema}.cfl_attachment (
    node_id        bigint primary key references {schema}.node on delete cascade,
    space_key      varchar not null,
    page_id        varchar not null,
    attachment_id  varchar not null,
    title          varchar not null,
    media_type     varchar not null,
    file_size      bigint not null,
    version        integer not null,
    created_at     timestamptz not null,
    updated_at     timestamptz not null,
    author         varchar not null,
    content_hash   varchar not null,
    indexer_hash   varchar not null
);
create index if not exists cfl_attachment__space_key on {schema}.cfl_attachment using btree (space_key);
create index if not exists cfl_attachment__page_id on {schema}.cfl_attachment using btree (page_id);

create table if not exists {schema}.cfl_comment (
    node_id       bigint primary key references {schema}.node on delete cascade,
    space_key     varchar not null,
    page_id       varchar not null,
    comment_id    varchar not null,
    location      varchar not null,
    version       integer not null,
    created_at    timestamptz not null,
    updated_at    timestamptz not null,
    author        varchar not null,
    content_hash  varchar not null,
    indexer_hash  varchar not null
);
create index if not exists cfl_comment__space_key on {schema}.cfl_comment using btree (space_key);
create index if not exists cfl_comment__page_id on {schema}.cfl_comment using btree (page_id);

/*
Ребро страница -> страница по ссылке в теле; kind говорит, как ссылка была записана:
по id, по заголовку или макросом.
*/
create table if not exists {schema}.cfl_page_link (
    edge_id  bigint primary key references {schema}.edge on delete cascade,
    kind     varchar not null
);
