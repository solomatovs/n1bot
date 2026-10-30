-- Четвёртый шаг слияния: сессиям тредов чата назначается владелец, время
-- тредов сдвигается к последней записи их сессий.
--
-- Владелец сессии (session.owner) — логин пользователя треда, который
-- называет сессию ключом threads.meta.agent_session; сессия без владельца не
-- видна пользователям службы агента. Назначается только сессиям, у которых
-- владельца ещё нет.
--
-- Время треда (threads.updated_at, если его уже завёл чат текущего
-- выпуска) сдвигается вперёд ко времени последней записи его сессии: старт
-- чата заводит колонку временем создания треда, и без сдвига список тредов
-- шёл бы по созданию, а не по последней беседе.
--
-- Повторный запуск ничего не меняет: владельцы назначены, время тредов
-- сдвинуто.
--
-- Переменные psql (-v), необязательные: aid_schema (aid), boba_schema (boba).
--
-- Запуск:
--   PGDATABASE=<целевая база> psql -f normalize.sql

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?aid_schema}
\else
    \set aid_schema aid
\endif
\if :{?boba_schema}
\else
    \set boba_schema boba
\endif

select to_regclass(format('%I.session_records', :'boba_schema')) is null as no_history \gset
\if :no_history
    \echo 'There is no table' :boba_schema'.session_records in this database: convert or merge the history first.'
    do $$ begin raise exception 'Nothing to normalize without the agent history tables, so nothing was changed.'; end $$;
\endif

begin;

set local client_min_messages = warning;

alter table :"boba_schema".session add column if not exists owner text;

create index if not exists session_owner_id_idx on :"boba_schema".session (owner, id);

create temporary table owned (id text) on commit drop;

with assigned as (
    update :"boba_schema".session as s
    set
        owner = u.identifier
    from
        :"aid_schema".threads as t
        join :"aid_schema".users as u
            on u.id = t.user_id
    where
        t.meta ->> 'agent_session' = s.id
        and s.owner is null
    returning s.id
)
insert into owned select id from assigned;

create temporary table moved_threads (id uuid) on commit drop;

select exists (
    select 1
    from information_schema.columns
    where table_schema = :'aid_schema'
        and table_name = 'threads'
        and column_name = 'updated_at'
) as has_updated_at \gset
\if :has_updated_at
    with lasts as (
        select
            h.session_id,
            max(h.recorded_at) as last
        from
            :"boba_schema".session_records as h
        group by
            h.session_id
    ),
    moved as (
        update :"aid_schema".threads as t
        set
            updated_at = l.last
        from
            lasts as l
        where
            l.session_id = t.meta ->> 'agent_session'
            and l.last > t.updated_at
        returning t.id
    )
    insert into moved_threads select id from moved;
\endif

\echo 'Sessions that got the login of their thread user as the owner:'
select count(*) as sessions from owned;

\echo 'Threads whose updated_at moved to the last record of their session:'
select count(*) as threads from moved_threads;

commit;

\echo 'The normalization is committed.'
