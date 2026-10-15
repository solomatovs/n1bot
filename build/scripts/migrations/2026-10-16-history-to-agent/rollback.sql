-- Откат переноса истории чата, сделанного migrate.sql.
--
-- Перенесённую сессию откат узнаёт по метке version в её записях: перенос
-- пишет её во все свои записи, а агент пишет туда свою версию. Откат удаляет
-- из истории агента такие сессии и снимает ключ agent_session у тредов, которые
-- на них указывают; metadata треда, которая до переноса была пустой (null),
-- после отката остаётся пустым объектом {}. Источник чекпойнтов перенос не
-- менял, поэтому после отката перенос можно повторить.
--
-- Если в перенесённой сессии уже есть записи агента, значит человек продолжил
-- беседу или правил вопрос. Тогда откат останавливается, ничего не тронув, и
-- выводит такие сессии списком: решение по ним принимается вручную.
--
-- Переменные psql те же, что у migrate.sql: history_schema, chainlit_schema и
-- version (по умолчанию history-migrate-2026-10).
--
-- Запуск:
--   psql -d <база> -v history_schema=agent_history -v chainlit_schema=chainlit \
--        -f rollback.sql

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\set missing false
\if :{?history_schema}
\else
    \echo 'Pass the schema of the agent history, for example -v history_schema=agent_history.'
    \set missing true
\endif
\if :{?chainlit_schema}
\else
    \echo 'Pass the schema of the chat data, for example -v chainlit_schema=chainlit.'
    \set missing true
\endif
\if :{?version}
\else
    \set version 'history-migrate-2026-10'
\endif
\if :missing
    do $$ begin raise exception 'The rollback needs the psql variables named above, so nothing was changed.'; end $$;
\endif

begin;

create temporary table migrated on commit drop as
select
    session_id,
    count(*) filter (where record ->> 'version' = :'version') as migrated,
    count(*) as total
from
    :"history_schema".history_records
group by
    session_id
having
    count(*) filter (where record ->> 'version' = :'version') > 0;

select count(*) > 0 as continued from migrated where total > migrated \gset
\if :continued
    \echo 'These migrated sessions already have records written by the agent:'
    select session_id, migrated, total from migrated where total > migrated order by session_id;
    do $$ begin raise exception 'Some migrated sessions were continued in the chat (listed above), so nothing was rolled back. Decide on them by hand first.'; end $$;
\endif

delete from
    :"history_schema".history_records as h
using
    migrated as m
where
    h.session_id = m.session_id;

\echo 'Result of the rollback:'
with unbound as (
    update
        :"chainlit_schema".threads as t
    set
        meta = t.meta - 'agent_session'
    from
        migrated as m
    where
        t.meta ->> 'agent_session' = m.session_id
    returning
        t.id
)
select
    (select count(*) from migrated) as sessions_removed,
    (select coalesce(sum(migrated), 0) from migrated) as records_removed,
    (select count(*) from unbound) as threads_unbound,
    :'version' as version;

commit;

\echo 'The rollback is committed.'
