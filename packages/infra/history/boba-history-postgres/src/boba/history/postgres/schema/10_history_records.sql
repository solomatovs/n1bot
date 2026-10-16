create table if not exists {schema}.history_records (
    session_id text        not null,
    seq        integer     not null,
    type       text        not null,
    record     jsonb       not null,
    created_at timestamptz not null default now(),
    primary key (session_id, seq)
);

comment on table {schema}.history_records is
'Журнал сессий агента: по записи на строку, в порядке seq внутри сессии; документ записи такой же, как строка журнала JSONL';

comment on column {schema}.history_records.seq is
'Порядковый номер записи в сессии с 1; выдаёт сессия агента';

comment on column {schema}.history_records.type is
'Вид записи: user, assistant, attachment, system, queue-operation, permission-mode, permission-rule, api-request';
