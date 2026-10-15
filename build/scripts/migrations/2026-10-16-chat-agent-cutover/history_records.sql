-- Таблица истории агента: то же определение, что у PostgresHistoryStore
-- (boba-history-postgres); их совпадение проверяет тест хранилища. Файл
-- выполняется из migrate.sql с search_path на схеме истории.
create table if not exists history_records (
    session_id text        not null,
    seq        integer     not null,
    type       text        not null,
    record     jsonb       not null,
    created_at timestamptz not null default now(),
    primary key (session_id, seq)
);
