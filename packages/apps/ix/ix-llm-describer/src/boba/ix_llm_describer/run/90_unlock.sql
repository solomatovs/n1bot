/*
ix-llm-describer: снять захваты очереди после записи пачки или при любой ошибке.
*/
-- @name unlock
select pg_advisory_unlock_all() as unlocked;
