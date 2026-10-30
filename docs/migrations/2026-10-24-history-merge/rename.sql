-- Таблица записей истории агента называется session_records (рядом с
-- session); выпуски до 2026-10-24 называли её history. Скрипт переименовывает
-- её на месте вместе с первичным ключом и ссылкой на session, так что база
-- выглядит так, как её создаёт текущее хранилище (boba-history-postgres,
-- schema/10_history.sql).
--
-- Выпуск, который читает history, после переименования истории не увидит, а
-- текущий без переименования заведёт пустую session_records и не увидит
-- старых записей. Поэтому скрипт запускают вместе со сменой выпуска: чат,
-- агент и служба агента остановлены, затем переименование, затем запуск
-- нового выпуска.
--
-- Если session_records уже есть (новый выпуск стартовал раньше скрипта и
-- завёл её пустой), пустая таблица уступает место старой; если в ней уже
-- есть записи, скрипт останавливается до изменений. Повторный запуск ничего
-- не меняет: history уже нет.
--
-- Переменная psql (-v), необязательная: boba_schema (boba) — схема истории.
--
-- Запуск:
--   PGDATABASE=<база> psql -f rename.sql

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?boba_schema}
\else
    \set boba_schema boba
\endif

select to_regclass(format('%I.history', :'boba_schema')) is null as renamed \gset
\if :renamed
    \echo 'There is no table' :boba_schema'.history in this database: the history is renamed already or was never kept here.'
    \quit
\endif

begin;

select to_regclass(format('%I.session_records', :'boba_schema')) is not null as has_new \gset
\if :has_new
    select exists (select 1 from :"boba_schema".session_records) as new_has_rows \gset
    \if :new_has_rows
        \echo 'Both' :boba_schema'.history and' :boba_schema'.session_records hold records.'
        do $$ begin raise exception 'The history is kept in two tables, so nothing was changed: move the records of session_records into history by hand and run again.'; end $$;
    \endif

    drop table :"boba_schema".session_records;
\endif

alter table :"boba_schema".history rename to session_records;

select to_regclass(format('%I.history_pkey', :'boba_schema')) is not null as has_old_key \gset
\if :has_old_key
    alter index :"boba_schema".history_pkey rename to session_records_pkey;
\endif

select exists (
    select 1
    from pg_constraint
    where conrelid = format('%I.session_records', :'boba_schema')::regclass
        and conname = 'history_session_id_fkey'
) as has_old_reference \gset
\if :has_old_reference
    alter table :"boba_schema".session_records
        rename constraint history_session_id_fkey to session_records_session_id_fkey;
\endif

commit;

\echo 'The table history is renamed to session_records.'
