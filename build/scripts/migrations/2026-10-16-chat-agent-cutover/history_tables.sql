-- Таблицы истории агента: те же определения, что у PostgresHistoryStore
-- (boba-history-postgres); их совпадение проверяет тест хранилища. Файл
-- выполняется из migrate.sql с search_path на схеме истории.
create table if not exists session (
    id         text        primary key,
    created_at timestamptz not null
);

create table if not exists history (
    session_id  text        not null references session on delete cascade,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);
