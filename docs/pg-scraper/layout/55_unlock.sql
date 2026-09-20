-- Снятие сессионного замка scope после commit или отката.
select pg_advisory_unlock_all() as scope_unlocked;
