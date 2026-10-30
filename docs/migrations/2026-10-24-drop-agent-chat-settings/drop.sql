-- Снятие таблицы настроек агента треда: чат её больше не пишет и не читает,
-- удаление треда собирает всё из текущего состояния. Схема чата — переменная
-- psql aid_schema, по умолчанию aid.

\if :{?aid_schema}
\else
\set aid_schema aid
\endif

drop table if exists :"aid_schema".agent_chat_settings;
