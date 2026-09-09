-- Профили web: base_url -> части адреса httpx.URL (scheme, host, port, path, query,
-- fragment). Ключ base_url из data убирается. Строка с учётными данными в base_url
-- или с адресом не по форме URL не переводится: скрипт падает с перечнем имён,
-- такие строки правятся или удаляются вручную.
-- Запуск на каждую схему приложения:
--   psql -v schema=chainlit   -f 2026-09-09-web-address-parts.sql
--   psql -v schema=automation -f 2026-09-09-web-address-parts.sql
set search_path to :schema;

begin;

do $$
declare
    bad text;
begin
    select string_agg(format('%s (%s)', name, data ->> 'base_url'), ', ' order by name)
      into bad
      from connections
     where data ->> 'kind' = 'web'
       and data ? 'base_url'
       and (data ->> 'base_url') !~ '^https?://[^@/?#]+(/[^?#]*)?(\?[^#]*)?(#.*)?$';

    if bad is not null then
        raise exception 'connections has web row(s) with a base_url that cannot be split into address parts: %; fix or delete these rows by hand', bad;
    end if;
end $$;

with parsed as (
    select
        id,
        regexp_match(
            data ->> 'base_url',
            '^(https?)://([^:/?#]+)(?::([0-9]+))?(/[^?#]*)?(?:\?([^#]*))?(?:#(.*))?$'
        ) as m
    from
        connections
    where 1=1
        and data ->> 'kind' = 'web'
        and data ? 'base_url'
)
update connections c
   set data = (c.data - 'base_url') || jsonb_strip_nulls(jsonb_build_object(
           'scheme',   p.m[1],
           'host',     lower(p.m[2]),
           'port',     p.m[3]::int,
           'path',     regexp_replace(p.m[4], '/+$', ''),
           'query',    p.m[5],
           'fragment', p.m[6]
       ))
  from parsed p
 where p.id = c.id;

commit;
