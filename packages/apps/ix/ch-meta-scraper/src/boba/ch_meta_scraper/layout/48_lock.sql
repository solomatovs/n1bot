-- Сессионный advisory-замок на scope источника: сервер целиком. Берётся в autocommit
-- до begin и держится через всю транзакцию 50_apply.sql, снимается 55_unlock.sql после
-- commit или при любой ошибке. Зачем он нужен, описано в pg-meta-scraper/layout/48_lock.sql:
-- замки for update защищают только строки, которые уже есть, а строки второго загрузчика
-- того же scope, закоммиченные после нашего снимка, on conflict do nothing пропустил бы молча.
select
    pg_advisory_lock(
        hashtextextended(scheme || '://' || host || ':' || port, 0)
    ) as scope_locked
from
    raw_source;
