-- Права рабочей базы чат-бота playground: пользователи приходят в неё своим
-- логином через соединение pg-krb-delegate (Kerberos-делегирование) и
-- создают и меняют свои объекты. Группа adusers получает создание схем в
-- базе и объектов в public. Базу заранее создаёт администратор
-- (createdb -O boba-svc playground), скрипт идёт под суперпользователем, в
-- самой базе. Повторный запуск ничего не меняет.
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

commit;

\echo 'The playground database is ready: adusers create their own objects.'
