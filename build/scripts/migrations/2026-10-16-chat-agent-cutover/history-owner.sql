-- Необязательный шаг перехода для тех, кто разводит роли: история агента
-- переходит своей роли. На стенде разработки чат и агент ходят одной учёткой,
-- и этот шаг не нужен. Роль истории
-- становится владельцем схемы агента (boba) и всех её таблиц и
-- последовательностей; у роли чата и у public прав на эту схему не остаётся.
-- Агент входит в базу ролью истории ([postgres_history] конфига чата) и сам
-- доводит свои таблицы до раскладки текущего выпуска, поэтому владеть ими
-- должна она.
--
-- Выполняет администратор базы (суперпользователь) в новой базе после
-- migrate.sql, а на стенде, переведённом convert-history.sql, — после него.
-- Роли заводятся заранее: роль истории с правом входа (паролем либо
-- kerberos: имя роли — короткое имя принципала) и роль чата, под которой
-- работает чат. Роль чата не должна быть суперпользователем и членом роли
-- истории: иначе права на историю у неё остаются в обход владельца.
--
-- Переменные psql (передаются через -v):
--   history_role — обязательна: роль истории агента, например boba_history;
--   chat_role    — обязательна: роль чата, например boba-svc;
--   boba_schema  — схема агента, по умолчанию boba.
--
-- Запуск:
--   PGDATABASE=<новая база> psql -v history_role=boba_history \
--        -v chat_role=boba-svc -f history-owner.sql
--
-- Всё идёт одной транзакцией; повторный запуск ничего не меняет.

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?boba_schema}
\else
    \set boba_schema boba
\endif
\if :{?history_role}
\else
    \echo 'Pass the role of the agent history, for example -v history_role=boba_history.'
    do $$ begin raise exception 'The step needs the psql variable history_role, so nothing was changed.'; end $$;
\endif
\if :{?chat_role}
\else
    \echo 'Pass the role the chat signs in with, for example -v chat_role=boba-svc.'
    do $$ begin raise exception 'The step needs the psql variable chat_role, so nothing was changed.'; end $$;
\endif

select to_regnamespace(:'boba_schema') is null as no_schema \gset
\if :no_schema
    \echo 'There is no schema' :boba_schema 'in this database: run migrate.sql first or pass -v boba_schema=<schema of [agent.history]>.'
    do $$ begin raise exception 'The agent schema is missing, so nothing was changed.'; end $$;
\endif

select not exists (select 1 from pg_roles where rolname = :'history_role' and rolcanlogin) as no_history_role \gset
\if :no_history_role
    \echo 'There is no role' :history_role 'with LOGIN: create it first, for example create role' :history_role 'login password ''...''.'
    do $$ begin raise exception 'The history role is missing or cannot sign in, so nothing was changed.'; end $$;
\endif

select not exists (select 1 from pg_roles where rolname = :'chat_role') as no_chat_role \gset
\if :no_chat_role
    \echo 'There is no role' :chat_role 'in this cluster: pass the role the chat signs in with.'
    do $$ begin raise exception 'The chat role is missing, so nothing was changed.'; end $$;
\endif

select :'history_role' = :'chat_role' as same_roles \gset
\if :same_roles
    \echo 'The history role and the chat role are both' :chat_role': the agent history needs a role of its own.'
    do $$ begin raise exception 'The history role equals the chat role, so nothing was changed.'; end $$;
\endif

select rolsuper or pg_has_role(:'chat_role', :'history_role', 'member') as chat_sees_history
from pg_roles
where rolname = :'chat_role' \gset
\if :chat_sees_history
    \echo 'The chat role' :chat_role 'is a superuser or a member of' :history_role': it would keep reading the history whoever owns it.'
    do $$ begin raise exception 'The chat role reaches the history role, so nothing was changed.'; end $$;
\endif

begin;

select format('grant connect on database %I to %I', current_database(), :'history_role') \gexec

select format('alter schema %I owner to %I', :'boba_schema', :'history_role') \gexec

-- последовательности identity-колонок переходят вместе со своими таблицами
select format('alter table %I.%I owner to %I', n.nspname, c.relname, :'history_role')
from pg_class as c
join pg_namespace as n on n.oid = c.relnamespace
where
    n.nspname = :'boba_schema'
    and c.relkind in ('r', 'p')
order by c.relname \gexec

select format('alter sequence %I.%I owner to %I', n.nspname, c.relname, :'history_role')
from pg_class as c
join pg_namespace as n on n.oid = c.relnamespace
where
    n.nspname = :'boba_schema'
    and c.relkind = 'S'
    and not exists (
        select 1 from pg_depend as d
        where d.objid = c.oid and d.deptype in ('a', 'i')
    )
order by c.relname \gexec

select format('revoke all on schema %I from public', :'boba_schema') \gexec
select format('revoke all on schema %I from %I', :'boba_schema', :'chat_role') \gexec
select format('revoke all on all tables in schema %I from %I', :'boba_schema', :'chat_role') \gexec
select format('revoke all on all sequences in schema %I from %I', :'boba_schema', :'chat_role') \gexec

select count(*) as left_to_chat
from pg_class as c
join pg_namespace as n on n.oid = c.relnamespace
where
    n.nspname = :'boba_schema'
    and c.relkind in ('r', 'p', 'S')
    and (
        has_table_privilege(:'chat_role', c.oid, 'select')
        or has_table_privilege(:'chat_role', c.oid, 'insert')
        or has_schema_privilege(:'chat_role', n.oid, 'usage')
    ) \gset

select count(*) as not_owned
from pg_class as c
join pg_namespace as n on n.oid = c.relnamespace
where
    n.nspname = :'boba_schema'
    and c.relkind in ('r', 'p', 'S')
    and pg_get_userbyid(c.relowner) <> :'history_role' \gset

select :left_to_chat > 0 or :not_owned > 0 as failed \gset
\if :failed
    \echo 'After the change' :left_to_chat 'objects of' :boba_schema 'are still reachable by' :chat_role 'and' :not_owned 'are not owned by' :history_role'.'
    rollback;
    do $$ begin raise exception 'The history is not handed over to its role, so nothing was changed.'; end $$;
\endif

commit;

\echo 'The schema' :boba_schema 'and its tables belong to' :history_role'; the chat role' :chat_role 'has no rights on them.'
