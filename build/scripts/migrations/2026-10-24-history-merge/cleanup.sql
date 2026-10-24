-- Последний шаг слияния, когда история проверена: удаление таблиц, которых
-- текущий выпуск не создаёт и не читает, — старой таблицы истории агента
-- history_records и слоя пользователя агента agent_user_settings. Сначала
-- выполняется сверка verify.sql; если она находит расхождение, скрипт
-- останавливается до удаления. Повторный запуск ничего не меняет.
--
-- Переменные psql (-v), необязательные: aid_schema (aid), boba_schema (boba).
--
-- Запуск:
--   PGDATABASE=<целевая база> psql -f cleanup.sql

\ir verify.sql

begin;

drop table if exists :"boba_schema".history_records;

drop table if exists :"aid_schema".agent_user_settings;

commit;

\echo 'The tables history_records and agent_user_settings are dropped.'
