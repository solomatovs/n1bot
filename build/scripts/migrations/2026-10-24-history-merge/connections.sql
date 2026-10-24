-- Соединения пользователей, которые смотрят на удаляемые базы: соединение
-- remove удаляется вместе со своими грантами (как ConnectionStore.remove), у
-- postgres-соединения repoint база меняется на dbname. База — открытое поле
-- профиля, секреты профиля (шифр enc:v1: ключом [connections] чата)
-- остаются как были. Любая из двух правок необязательна.
--
-- Скрипт останавливается до изменений, если соединения repoint нет или оно
-- не postgres. Соединения remove, которого уже нет, — заметка. Повторный
-- запуск ничего не меняет.
--
-- Переменные psql (-v): remove — имя удаляемого соединения; repoint и
-- dbname — имя postgres-соединения и его новая база; aid_schema (aid) —
-- схема чата.
--
-- Запуск:
--   PGDATABASE=<база чата> psql -v remove=pg-solomatovs \
--        -v repoint=pg-krb-delegate -v dbname=playground -f connections.sql

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?aid_schema}
\else
    \set aid_schema aid
\endif

begin;

\if :{?remove}
    create temporary table removed on commit drop as
    select id from :"aid_schema".connections where name = :'remove';

    select count(*) = 0 as nothing_to_remove from removed \gset
    \if :nothing_to_remove
        \echo 'There is no connection' :remove'; nothing to remove.'
    \else
        delete from :"aid_schema".grants as g
        using removed as r
        where g.src_kind = 'connections' and g.src_kind_id = r.id;

        delete from :"aid_schema".connections as c
        using removed as r
        where c.id = r.id;

        \echo 'The connection' :remove 'and its grants are removed.'
    \endif
\endif

\if :{?repoint}
    \if :{?dbname}
    \else
        do $$ begin raise exception 'repoint needs the new database name in the psql variable dbname, so nothing was changed.'; end $$;
    \endif

    select count(*) <> 1 as repoint_missing
    from :"aid_schema".connections
    where name = :'repoint' and data ->> 'kind' = 'postgres' \gset
    \if :repoint_missing
        \echo 'Expected exactly one postgres connection named' :repoint'.'
        do $$ begin raise exception 'The connection to repoint is not found or is not a postgres connection, so nothing was changed.'; end $$;
    \endif

    update :"aid_schema".connections
    set data = jsonb_set(data, '{dbname}', to_jsonb(:'dbname'::text))
    where name = :'repoint';

    \echo 'The connection' :repoint 'now goes to the database' :dbname'.'
\endif

commit;
