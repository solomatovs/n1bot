-- Шина: live_locks хранит users.id захватившего; таблица unlogged и эфемерна,
-- поэтому строки не переносятся. Запуск: psql -v schema=live -f 2026-08-29-uuid-ids-live.sql
set search_path to :schema;

begin;
truncate table live_locks;
alter table live_locks alter column user_id type uuid using gen_random_uuid();
commit;
