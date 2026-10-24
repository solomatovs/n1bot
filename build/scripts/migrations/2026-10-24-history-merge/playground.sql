-- Права рабочей базы чат-бота playground: пользователи приходят в неё своим
-- логином через соединение pg-krb-delegate (Kerberos-делегирование) и
-- создают и меняют свои объекты. Группа adusers получает создание схем в
-- базе и объектов в public; роль readonly, хоть и состоит в adusers, объекты
-- не создаёт и не меняет: её DDL отклоняет триггер событий. Базу заранее
-- создаёт администратор (createdb -O boba-svc playground), скрипт идёт под
-- суперпользователем, в самой базе. Повторный запуск ничего не меняет.
--
-- Запуск:
--   createdb -O boba-svc playground
--   PGDATABASE=playground psql -f playground.sql

\set ON_ERROR_STOP on
\set QUIET on

begin;

comment on database :"DBNAME" is 'Working database of the chat bot: users who come through the pg-krb-delegate connection (Kerberos delegation, their own login) create and change their own objects here.';

grant create on database :"DBNAME" to adusers;

grant usage, create on schema public to adusers;

create or replace function public.deny_readonly_ddl()
returns event_trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
    if current_user = 'readonly' or session_user = 'readonly' then
        raise exception 'role readonly cannot create or change objects in the playground database: it is read-only here';
    end if;
end
$$;

revoke all on function public.deny_readonly_ddl() from public;

drop event trigger if exists deny_readonly_ddl;

create event trigger deny_readonly_ddl on ddl_command_start
    execute function public.deny_readonly_ddl();

commit;

\echo 'The playground database is ready: adusers create their own objects, readonly is refused.'
