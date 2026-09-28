-- Шаблон тестовых баз стенда: наборы и процессы xdist создают свои базы из него, а
-- расширения ставит только суперпользователь. Запуск суперпользователем на сервере
-- приложения: psql -U postgres -v owner=boba-svc -f template.sql
\set ON_ERROR_STOP on
select format('create database boba_stand_template owner %I', :'owner')
where not exists (select 1 from pg_database where datname = 'boba_stand_template') \gexec
\connect boba_stand_template
create extension if not exists vector schema public;
create extension if not exists pg_trgm schema public;
create extension if not exists unaccent schema public;
create extension if not exists btree_gin schema public;
alter database boba_stand_template is_template true;
