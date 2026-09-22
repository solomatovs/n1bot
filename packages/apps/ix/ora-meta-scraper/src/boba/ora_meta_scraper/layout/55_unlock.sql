-- Снять сессионные advisory-замки после commit или ошибки apply.
select pg_advisory_unlock_all();
