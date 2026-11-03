create table if not exists {schema}.session (
    id         text        primary key,
    created_at timestamptz not null,
    user_id    uuid references {schema}.users(id)
);


create index if not exists session_user_id_id_idx
    on {schema}.session (user_id, id);

create table if not exists {schema}.session_records (
    session_id  text        not null references {schema}.session on delete cascade,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);

comment on table {schema}.session is
'Сессии агента. user_id ссылается на users; NULL означает историю локального stdio без пользователя службы.';

comment on column {schema}.session.user_id is
'UUID пользователя службы. Удаление пользователя с историей запрещает внешний ключ.';

comment on table {schema}.session_records is
'Записи истории сессии. Пользователь определяется через session_id, удаление сессии удаляет её записи.';
