-- psql: psql (PostgreSQL) 12.12
-- server: PostgreSQL 12.12 (Greenplum Database 7.0.0-b2 build dev) on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 11.3.0-1ubuntu1~22.04) 11.3.0, 64-bit compiled on Apr 18 2023 18:08:42 Bhuvnesh C.

-- ===== \l =====
SELECT d.datname as "Name",
       pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
       pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
       d.datcollate as "Collate",
       d.datctype as "Ctype",
       pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_database d
ORDER BY 1;

-- ===== \l+ =====
SELECT d.datname as "Name",
       pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
       pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
       d.datcollate as "Collate",
       d.datctype as "Ctype",
       pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges",
       CASE WHEN pg_catalog.has_database_privilege(d.datname, 'CONNECT')
            THEN pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname))
            ELSE 'No Access'
       END as "Size",
       t.spcname as "Tablespace",
       pg_catalog.shobj_description(d.oid, 'pg_database') as "Description"
FROM pg_catalog.pg_database d
  JOIN pg_catalog.pg_tablespace t on d.dattablespace = t.oid
ORDER BY 1;

-- ===== \l postgres =====
SELECT d.datname as "Name",
       pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
       pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
       d.datcollate as "Collate",
       d.datctype as "Ctype",
       pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_database d
WHERE d.datname OPERATOR(pg_catalog.~) '^(postgres)$' COLLATE pg_catalog.default
ORDER BY 1;

-- ===== \du =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit, r.rolvaliduntil,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
, r.rolcreaterextgpfd
, r.rolcreatewextgpfd
, r.rolcreaterexthttp
, r.rolreplication
, r.rolbypassrls
FROM pg_catalog.pg_roles r
WHERE r.rolname !~ '^pg_'
ORDER BY 1;

-- ===== \du+ =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit, r.rolvaliduntil,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
, r.rolcreaterextgpfd
, r.rolcreatewextgpfd
, r.rolcreaterexthttp
, pg_catalog.shobj_description(r.oid, 'pg_authid') AS description
, r.rolreplication
, r.rolbypassrls
FROM pg_catalog.pg_roles r
WHERE r.rolname !~ '^pg_'
ORDER BY 1;

-- ===== \du scrape_login =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit, r.rolvaliduntil,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
, r.rolcreaterextgpfd
, r.rolcreatewextgpfd
, r.rolcreaterexthttp
, r.rolreplication
, r.rolbypassrls
FROM pg_catalog.pg_roles r
WHERE r.rolname OPERATOR(pg_catalog.~) '^(scrape_login)$' COLLATE pg_catalog.default
ORDER BY 1;

-- ===== \dg =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit, r.rolvaliduntil,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
, r.rolcreaterextgpfd
, r.rolcreatewextgpfd
, r.rolcreaterexthttp
, r.rolreplication
, r.rolbypassrls
FROM pg_catalog.pg_roles r
WHERE r.rolname !~ '^pg_'
ORDER BY 1;

-- ===== \dg+ =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit, r.rolvaliduntil,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
, r.rolcreaterextgpfd
, r.rolcreatewextgpfd
, r.rolcreaterexthttp
, pg_catalog.shobj_description(r.oid, 'pg_authid') AS description
, r.rolreplication
, r.rolbypassrls
FROM pg_catalog.pg_roles r
WHERE r.rolname !~ '^pg_'
ORDER BY 1;

-- ===== \drg =====
-- (no queries sent)

-- ===== \drds =====
SELECT rolname AS "Role", datname AS "Database",
pg_catalog.array_to_string(setconfig, E'\n') AS "Settings"
FROM pg_catalog.pg_db_role_setting s
LEFT JOIN pg_catalog.pg_database d ON d.oid = setdatabase
LEFT JOIN pg_catalog.pg_roles r ON r.oid = setrole
ORDER BY 1, 2;

-- ===== \drds scrape_login =====
SELECT rolname AS "Role", datname AS "Database",
pg_catalog.array_to_string(setconfig, E'\n') AS "Settings"
FROM pg_catalog.pg_db_role_setting s
LEFT JOIN pg_catalog.pg_database d ON d.oid = setdatabase
LEFT JOIN pg_catalog.pg_roles r ON r.oid = setrole
WHERE r.rolname OPERATOR(pg_catalog.~) '^(scrape_login)$' COLLATE pg_catalog.default
ORDER BY 1, 2;

-- ===== \dn =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
FROM pg_catalog.pg_namespace n
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
ORDER BY 1;

-- ===== \dn+ =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner",
  pg_catalog.array_to_string(n.nspacl, E'\n') AS "Access privileges",
  pg_catalog.obj_description(n.oid, 'pg_namespace') AS "Description"
FROM pg_catalog.pg_namespace n
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
ORDER BY 1;

-- ===== \dnS =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
FROM pg_catalog.pg_namespace n
ORDER BY 1;

-- ===== \dn app =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
FROM pg_catalog.pg_namespace n
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1;

-- ===== \d =====
select pg_catalog.version();
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','v','m','S','f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \d+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','v','m','S','f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','v','m','S','s','f','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dS+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','v','m','S','s','f','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \d app.* =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16466';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16466' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16466' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16466'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16466'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16466')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16466' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16466' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16466');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16456';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16456' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16456' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16456'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16456'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16456')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16456' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16456' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16456');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16420';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16420' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16420' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT true as sametable, conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef,
  conrelid::pg_catalog.regclass AS ontable
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16420' AND r.contype = 'f'
     AND conparentid = 0
ORDER BY conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16420')
                     UNION ALL VALUES ('16420'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16420' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16420'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16420'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16420')
ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16420' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D') 
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend WHERE objid = t.oid 
        AND refclassid = 'pg_catalog.pg_trigger'::pg_catalog.regclass))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16420' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16420' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16420');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16418';
SELECT pg_catalog.format_type(seqtypid, NULL) AS "Type",
       seqstart AS "Start",
       seqmin AS "Minimum",
       seqmax AS "Maximum",
       seqincrement AS "Increment",
       CASE WHEN seqcycle THEN 'yes' ELSE 'no' END AS "Cycles?",
       seqcache AS "Cache"
FROM pg_catalog.pg_sequence
WHERE seqrelid = '16418';
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname),
   d.deptype
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16418'
 AND d.deptype IN ('a', 'i');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16427';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  CASE WHEN a.attnum <= (SELECT i.indnkeyatts FROM pg_catalog.pg_index i WHERE i.indexrelid = '16427') THEN 'yes' ELSE 'no' END AS is_key,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16427' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16427' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16485';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16485' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16485' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16485'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16485'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16485')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16485' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16485' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16485' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16485');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16488';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16488' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16488' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16488'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16488'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16488')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16488' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16488' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16488' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16488');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16507';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16507' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16507' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16507'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16507'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16507')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16507' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16507' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16507' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16507');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16473';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16473' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pg_catalog.pg_get_partkeydef('16473'::pg_catalog.oid);
SELECT conrelid = '16473'::pg_catalog.regclass AS sametable,
       conname,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef,
       conrelid::pg_catalog.regclass AS ontable
  FROM pg_catalog.pg_constraint,
       pg_catalog.pg_partition_ancestors('16473')
 WHERE conrelid = relid AND contype = 'f' AND conparentid = 0
ORDER BY sametable DESC, conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16473')
                     UNION ALL VALUES ('16473'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16473' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16473'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16473'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16473')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16473' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16473' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16473');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16479';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16479' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16479';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16479' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16479'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16479'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16479')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16479' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16479' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16479');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16482';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16482' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16482';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16482' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16482'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16482'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16482')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16482' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16482' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16482');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16476';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16476' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16476';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16476' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16476'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16476'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16476')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16476' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16476' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16476');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16659';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16659' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16659' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16659'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16659'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16659')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16659' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16659' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16659');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16657';
SELECT pg_catalog.format_type(seqtypid, NULL) AS "Type",
       seqstart AS "Start",
       seqmin AS "Minimum",
       seqmax AS "Maximum",
       seqincrement AS "Increment",
       CASE WHEN seqcycle THEN 'yes' ELSE 'no' END AS "Cycles?",
       seqcache AS "Cache"
FROM pg_catalog.pg_sequence
WHERE seqrelid = '16657';
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname),
   d.deptype
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16657'
 AND d.deptype IN ('a', 'i');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16434';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16434' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16434' AND r.contype = 'c'
ORDER BY 1;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16434' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16434'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16434'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16434')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16434' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16434' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16434');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16395';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16395' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16405';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16405' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16405' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16405' AND r.contype = 'c'
ORDER BY 1;
SELECT true as sametable, conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef,
  conrelid::pg_catalog.regclass AS ontable
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16405' AND r.contype = 'f'
     AND conparentid = 0
ORDER BY conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16405')
                     UNION ALL VALUES ('16405'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16405' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16405'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16405'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16405')
ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16405' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D') 
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend WHERE objid = t.oid 
        AND refclassid = 'pg_catalog.pg_trigger'::pg_catalog.regclass))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16405' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16405' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16405');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16417';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  CASE WHEN a.attnum <= (SELECT i.indnkeyatts FROM pg_catalog.pg_index i WHERE i.indexrelid = '16417') THEN 'yes' ELSE 'no' END AS is_key,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16417' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16417' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16403';
SELECT pg_catalog.format_type(seqtypid, NULL) AS "Type",
       seqstart AS "Start",
       seqmin AS "Minimum",
       seqmax AS "Maximum",
       seqincrement AS "Increment",
       CASE WHEN seqcycle THEN 'yes' ELSE 'no' END AS "Cycles?",
       seqcache AS "Cache"
FROM pg_catalog.pg_sequence
WHERE seqrelid = '16403';
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname),
   d.deptype
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16403'
 AND d.deptype IN ('a', 'i');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16636';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16636' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16636' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16636' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16636'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16636'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16636')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16636' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16636' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16636');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16643';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  CASE WHEN a.attnum <= (SELECT i.indnkeyatts FROM pg_catalog.pg_index i WHERE i.indexrelid = '16643') THEN 'yes' ELSE 'no' END AS is_key,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16643' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16643' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16415';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  CASE WHEN a.attnum <= (SELECT i.indnkeyatts FROM pg_catalog.pg_index i WHERE i.indexrelid = '16415') THEN 'yes' ELSE 'no' END AS is_key,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16415' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16415' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16491';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16491' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16647';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16647' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pg_catalog.pg_get_partkeydef('16647'::pg_catalog.oid);
SELECT conrelid = '16647'::pg_catalog.regclass AS sametable,
       conname,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef,
       conrelid::pg_catalog.regclass AS ontable
  FROM pg_catalog.pg_constraint,
       pg_catalog.pg_partition_ancestors('16647')
 WHERE conrelid = relid AND contype = 'f' AND conparentid = 0
ORDER BY sametable DESC, conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16647')
                     UNION ALL VALUES ('16647'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16647' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16647'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16647'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16647')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16647' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16647' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16647');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16650';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16650' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16650';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16650' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16650'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16650'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16650')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16650' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16650' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16650');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16444';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16444' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16444' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16444'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16444'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16444')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16444' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16444' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16444');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16450';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16450' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16450' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16450'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16450'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16450')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16450' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16450' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16450');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16401';
SELECT pg_catalog.format_type(seqtypid, NULL) AS "Type",
       seqstart AS "Start",
       seqmin AS "Minimum",
       seqmax AS "Maximum",
       seqincrement AS "Increment",
       CASE WHEN seqcycle THEN 'yes' ELSE 'no' END AS "Cycles?",
       seqcache AS "Cache"
FROM pg_catalog.pg_sequence
WHERE seqrelid = '16401';
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname),
   d.deptype
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16401'
 AND d.deptype IN ('a', 'i');

-- ===== \d app.parent =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16405';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16405' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16405' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16405' AND r.contype = 'c'
ORDER BY 1;
SELECT true as sametable, conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef,
  conrelid::pg_catalog.regclass AS ontable
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16405' AND r.contype = 'f'
     AND conparentid = 0
ORDER BY conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16405')
                     UNION ALL VALUES ('16405'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16405' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16405'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16405'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16405')
ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16405' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D') 
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend WHERE objid = t.oid 
        AND refclassid = 'pg_catalog.pg_trigger'::pg_catalog.regclass))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16405' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16405' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16405');

-- ===== \d+ app.parent =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16405';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16405' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16405' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16405' AND r.contype = 'c'
ORDER BY 1;
SELECT true as sametable, conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef,
  conrelid::pg_catalog.regclass AS ontable
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16405' AND r.contype = 'f'
     AND conparentid = 0
ORDER BY conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16405')
                     UNION ALL VALUES ('16405'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16405' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16405'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16405'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16405')
ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16405' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D') 
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend WHERE objid = t.oid 
        AND refclassid = 'pg_catalog.pg_trigger'::pg_catalog.regclass))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16405' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16405' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16405');

-- ===== \d app.child =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(child)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16420';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16420' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16420' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT true as sametable, conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef,
  conrelid::pg_catalog.regclass AS ontable
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16420' AND r.contype = 'f'
     AND conparentid = 0
ORDER BY conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16420')
                     UNION ALL VALUES ('16420'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16420' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16420'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16420'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16420')
ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16420' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D') 
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend WHERE objid = t.oid 
        AND refclassid = 'pg_catalog.pg_trigger'::pg_catalog.regclass))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16420' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16420' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16420');

-- ===== \d+ app.child =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(child)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16420';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16420' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16420' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT true as sametable, conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef,
  conrelid::pg_catalog.regclass AS ontable
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16420' AND r.contype = 'f'
     AND conparentid = 0
ORDER BY conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16420')
                     UNION ALL VALUES ('16420'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16420' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16420'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16420'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16420')
ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16420' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D') 
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend WHERE objid = t.oid 
        AND refclassid = 'pg_catalog.pg_trigger'::pg_catalog.regclass))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16420' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16420' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16420');

-- ===== \d app.inherited =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(inherited)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16434';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16434' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16434' AND r.contype = 'c'
ORDER BY 1;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16434' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16434'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16434'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16434')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16434' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16434' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16434');

-- ===== \d+ app.inherited =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(inherited)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16434';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16434' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16434' AND r.contype = 'c'
ORDER BY 1;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16434' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16434'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16434'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16434')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16434' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16434' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16434');

-- ===== \d app.random_dist =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(random_dist)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16444';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16444' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16444' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16444'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16444'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16444')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16444' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16444' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16444');

-- ===== \d+ app.random_dist =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(random_dist)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16444';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16444' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16444' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16444'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16444'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16444')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16444' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16444' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16444');

-- ===== \d app.replicated =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(replicated)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16450';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16450' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16450' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16450'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16450'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16450')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16450' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16450' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16450');

-- ===== \d+ app.replicated =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(replicated)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16450';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16450' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16450' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16450'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16450'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16450')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16450' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16450' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16450');

-- ===== \d app.ao_row =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_row)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16456';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16456' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16456' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16456'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16456'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16456')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16456' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16456' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16456');

-- ===== \d+ app.ao_row =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_row)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16456';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16456' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16456' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16456'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16456'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16456')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16456' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16456' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16456');

-- ===== \d app.ao_col =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_col)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16466';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16466' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16466' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16466'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16466'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16466')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16466' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16466' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16466');

-- ===== \d+ app.ao_col =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_col)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16466';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.array_to_string(e.attoptions, ','),
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16466' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16466' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16466'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16466'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16466')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16466' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16466' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16466');

-- ===== \d app.gp_part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16473';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16473' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pg_catalog.pg_get_partkeydef('16473'::pg_catalog.oid);
SELECT conrelid = '16473'::pg_catalog.regclass AS sametable,
       conname,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef,
       conrelid::pg_catalog.regclass AS ontable
  FROM pg_catalog.pg_constraint,
       pg_catalog.pg_partition_ancestors('16473')
 WHERE conrelid = relid AND contype = 'f' AND conparentid = 0
ORDER BY sametable DESC, conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16473')
                     UNION ALL VALUES ('16473'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16473' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16473'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16473'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16473')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16473' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16473' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16473');

-- ===== \d+ app.gp_part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16473';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16473' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pg_catalog.pg_get_partkeydef('16473'::pg_catalog.oid);
SELECT conrelid = '16473'::pg_catalog.regclass AS sametable,
       conname,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef,
       conrelid::pg_catalog.regclass AS ontable
  FROM pg_catalog.pg_constraint,
       pg_catalog.pg_partition_ancestors('16473')
 WHERE conrelid = relid AND contype = 'f' AND conparentid = 0
ORDER BY sametable DESC, conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16473')
                     UNION ALL VALUES ('16473'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16473' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16473'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16473'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16473')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16473' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16473' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16473');

-- ===== \d app.gp_part_1_prt_other =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part_1_prt_other)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16476';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16476' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16476';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16476' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16476'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16476'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16476')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16476' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16476' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16476');

-- ===== \d+ app.gp_part_1_prt_other =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part_1_prt_other)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16476';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16476' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid),
  pg_catalog.pg_get_partition_constraintdef(c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16476';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16476' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16476'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16476'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16476')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16476' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16476' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16476');

-- ===== \d app.ext =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16485';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16485' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16485' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16485'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16485'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16485')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16485' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16485' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16485' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16485');

-- ===== \d+ app.ext =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16485';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16485' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16485' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16485'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16485'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16485')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16485' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16485' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16485' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16485');

-- ===== \d app.ext_w =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext_w)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16488';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16488' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16488' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16488'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16488'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16488')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16488' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16488' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16488' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16488');

-- ===== \d+ app.ext_w =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext_w)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16488';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16488' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16488' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16488'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16488'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16488')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16488' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16488' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16488' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16488');

-- ===== \d app.parent_view =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_view)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16491';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16491' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;

-- ===== \d+ app.parent_view =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_view)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16491';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16491' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pg_catalog.pg_get_viewdef('16491'::pg_catalog.oid, true);
SELECT r.rulename, trim(trailing ';' from pg_catalog.pg_get_ruledef(r.oid, true))
FROM pg_catalog.pg_rewrite r
WHERE r.ev_class = '16491' AND r.rulename != '_RETURN' ORDER BY 1;

-- ===== \d app.parent_mv =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_mv)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16636';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16636' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16636' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16636' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16636'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16636'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16636')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16636' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16636' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16636');

-- ===== \d+ app.parent_mv =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_mv)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16636';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16636' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16636' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16636' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16636'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16636'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16636')
ORDER BY 1;
SELECT pg_catalog.pg_get_viewdef('16636'::pg_catalog.oid, true);
SELECT r.rulename, trim(trailing ';' from pg_catalog.pg_get_ruledef(r.oid, true))
FROM pg_catalog.pg_rewrite r
WHERE r.ev_class = '16636' AND r.rulename != '_RETURN' ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16636' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16636' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16636');

-- ===== \d app.seq_manual =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(seq_manual)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16401';
SELECT pg_catalog.format_type(seqtypid, NULL) AS "Type",
       seqstart AS "Start",
       seqmin AS "Minimum",
       seqmax AS "Maximum",
       seqincrement AS "Increment",
       CASE WHEN seqcycle THEN 'yes' ELSE 'no' END AS "Cycles?",
       seqcache AS "Cache"
FROM pg_catalog.pg_sequence
WHERE seqrelid = '16401';
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname),
   d.deptype
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16401'
 AND d.deptype IN ('a', 'i');

-- ===== \d+ app.seq_manual =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(seq_manual)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16401';
SELECT pg_catalog.format_type(seqtypid, NULL) AS "Type",
       seqstart AS "Start",
       seqmin AS "Minimum",
       seqmax AS "Maximum",
       seqincrement AS "Increment",
       CASE WHEN seqcycle THEN 'yes' ELSE 'no' END AS "Cycles?",
       seqcache AS "Cache"
FROM pg_catalog.pg_sequence
WHERE seqrelid = '16401';
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname),
   d.deptype
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16401'
 AND d.deptype IN ('a', 'i');

-- ===== \d app.parent_id_seq =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_id_seq)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16403';
SELECT pg_catalog.format_type(seqtypid, NULL) AS "Type",
       seqstart AS "Start",
       seqmin AS "Minimum",
       seqmax AS "Maximum",
       seqincrement AS "Increment",
       CASE WHEN seqcycle THEN 'yes' ELSE 'no' END AS "Cycles?",
       seqcache AS "Cache"
FROM pg_catalog.pg_sequence
WHERE seqrelid = '16403';
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname),
   d.deptype
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16403'
 AND d.deptype IN ('a', 'i');

-- ===== \d app.parent_pkey =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_pkey)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16415';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  CASE WHEN a.attnum <= (SELECT i.indnkeyatts FROM pg_catalog.pg_index i WHERE i.indexrelid = '16415') THEN 'yes' ELSE 'no' END AS is_key,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16415' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16415' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d+ app.parent_pkey =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_pkey)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16415';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  CASE WHEN a.attnum <= (SELECT i.indnkeyatts FROM pg_catalog.pg_index i WHERE i.indexrelid = '16415') THEN 'yes' ELSE 'no' END AS is_key,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16415' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16415' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d app.parent_created_idx =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_created_idx)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16417';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  CASE WHEN a.attnum <= (SELECT i.indnkeyatts FROM pg_catalog.pg_index i WHERE i.indexrelid = '16417') THEN 'yes' ELSE 'no' END AS is_key,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16417' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16417' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d app.part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16647';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16647' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pg_catalog.pg_get_partkeydef('16647'::pg_catalog.oid);
SELECT conrelid = '16647'::pg_catalog.regclass AS sametable,
       conname,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef,
       conrelid::pg_catalog.regclass AS ontable
  FROM pg_catalog.pg_constraint,
       pg_catalog.pg_partition_ancestors('16647')
 WHERE conrelid = relid AND contype = 'f' AND conparentid = 0
ORDER BY sametable DESC, conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16647')
                     UNION ALL VALUES ('16647'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16647' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16647'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16647'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16647')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16647' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16647' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16647');

-- ===== \d+ app.part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16647';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16647' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pg_catalog.pg_get_partkeydef('16647'::pg_catalog.oid);
SELECT conrelid = '16647'::pg_catalog.regclass AS sametable,
       conname,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef,
       conrelid::pg_catalog.regclass AS ontable
  FROM pg_catalog.pg_constraint,
       pg_catalog.pg_partition_ancestors('16647')
 WHERE conrelid = relid AND contype = 'f' AND conparentid = 0
ORDER BY sametable DESC, conname;
SELECT conname, conrelid::pg_catalog.regclass AS ontable,
       pg_catalog.pg_get_constraintdef(oid, true) AS condef
  FROM pg_catalog.pg_constraint c
 WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16647')
                     UNION ALL VALUES ('16647'::pg_catalog.regclass))
       AND contype = 'f' AND conparentid = 0
ORDER BY conname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16647' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16647'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16647'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16647')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16647' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16647' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16647');

-- ===== \d app.part_2024 =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part_2024)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16650';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16650' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16650';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16650' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16650'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16650'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16650')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16650' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16650' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16650');

-- ===== \d+ app.part_2024 =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part_2024)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16650';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16650' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT inhparent::pg_catalog.regclass,
  pg_catalog.pg_get_expr(c.relpartbound, c.oid),
  pg_catalog.pg_get_partition_constraintdef(c.oid)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_inherits i ON c.oid = inhrelid
WHERE c.oid = '16650';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16650' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16650'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16650'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16650')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16650' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16650' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16650');

-- ===== \d app.ident =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ident)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16659';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16659' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16659' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16659'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16659'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16659')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16659' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16659' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16659');

-- ===== \d+ app.ident =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ident)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16659';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16659' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16659' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16659'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16659'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16659')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16659' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16659' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16659');

-- ===== \d app.ft =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ft)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16507';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16507' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16507' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16507'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16507'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16507')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16507' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16507' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16507' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16507');

-- ===== \d+ app.ft =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ft)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16507';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16507' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '16507' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '16507'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '16507'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('16507')
ORDER BY 1;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16507' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16507' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16507' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16507');

-- ===== \d app.pair =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pair)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16395';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16395' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;

-- ===== \d+ app.pair =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pair)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '16395';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '16395' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;

-- ===== \d pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pg_class)$' COLLATE pg_catalog.default
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '1259' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '1259'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '1259'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('1259')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('1259');

-- ===== \d+ pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pg_class)$' COLLATE pg_catalog.default
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated,
  a.attstorage,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum) FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '1259' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '1259'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '1259'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('1259')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('1259');

-- ===== \d pg_catalog.pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pg_class)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(pg_catalog)$' COLLATE pg_catalog.default
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '1259' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '1259'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '1259'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('1259')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('1259');

-- ===== \d gp_segment_configuration =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_segment_configuration)$' COLLATE pg_catalog.default
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relrowsecurity, c.relforcerowsecurity, false AS relhasoids, c.relispartition, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', '),c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident, am.amname
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
LEFT JOIN pg_catalog.pg_am am ON (c.relam = am.oid)
WHERE c.oid = '5036';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity,
  a.attgenerated FROM pg_catalog.pg_attribute a
 LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
 ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
 LEFT JOIN pg_catalog.pg_class c
 ON c.oid=a.attrelid
 WHERE a.attrelid = '5036' AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '5036' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT spcname FROM pg_catalog.pg_tablespace
WHERE oid = '1664';
SELECT spcname FROM pg_catalog.pg_tablespace
WHERE oid = '1664';
SELECT pol.polname, pol.polpermissive,
  CASE WHEN pol.polroles = '{0}' THEN NULL ELSE pg_catalog.array_to_string(array(select rolname from pg_catalog.pg_roles where oid = any (pol.polroles) order by 1),',') END,
  pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
  pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid),
  CASE pol.polcmd
    WHEN 'r' THEN 'SELECT'
    WHEN 'a' THEN 'INSERT'
    WHEN 'w' THEN 'UPDATE'
    WHEN 'd' THEN 'DELETE'
    END AS cmd
FROM pg_catalog.pg_policy pol
WHERE pol.polrelid = '5036' ORDER BY 1;
SELECT oid, stxrelid::pg_catalog.regclass, stxnamespace::pg_catalog.regnamespace AS nsp, stxname,
  (SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),', ')
   FROM pg_catalog.unnest(stxkeys) s(attnum)
   JOIN pg_catalog.pg_attribute a ON (stxrelid = a.attrelid AND
        a.attnum = s.attnum AND NOT attisdropped)) AS columns,
  'd' = any(stxkind) AS ndist_enabled,
  'f' = any(stxkind) AS deps_enabled,
  'm' = any(stxkind) AS mcv_enabled
FROM pg_catalog.pg_statistic_ext stat WHERE stxrelid = '5036'
ORDER BY 1;
SELECT pubname
FROM pg_catalog.pg_publication p
JOIN pg_catalog.pg_publication_rel pr ON p.oid = pr.prpubid
WHERE pr.prrelid = '5036'
UNION ALL
SELECT pubname
FROM pg_catalog.pg_publication p
WHERE p.puballtables AND pg_catalog.pg_relation_is_publishable('5036')
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '5036' AND c.relkind != 'p' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass,       pg_catalog.pg_get_expr(c.relpartbound, c.oid),       c.relkind FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '5036' ORDER BY pg_catalog.pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT',          c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('5036');
SELECT spcname FROM pg_catalog.pg_tablespace
WHERE oid = '1664';

-- ===== \dt =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dt+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dt app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','s','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1,2;

-- ===== \dt+ app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
WHERE c.relkind IN ('r','p','s','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1,2;

-- ===== \di =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','I','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \di+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','I','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \diS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','I','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \di app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','I','s','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1,2;

-- ===== \ds =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('S','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \ds+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('S','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dsS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('S','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dv =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dv+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dvS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dm =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('m','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dm+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('m','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dE =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dE+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtvsimE =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"
,
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','p','v','m','i','I','S','f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtvsimE+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", a.amname as "Storage"
,
 c2.relname as "Table",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am a ON a.oid = c.relam
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','p','v','m','i','I','S','f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \da =====
SELECT n.nspname as "Schema",
  p.proname AS "Name",
  pg_catalog.format_type(p.prorettype, NULL) AS "Result data type",
  CASE WHEN p.pronargs = 0
    THEN CAST('*' AS pg_catalog.text)
    ELSE pg_catalog.pg_get_function_arguments(p.oid)
  END AS "Argument data types",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind = 'a'
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_function_is_visible(p.oid)
ORDER BY 1, 2, 4;

-- ===== \da+ =====
SELECT n.nspname as "Schema",
  p.proname AS "Name",
  pg_catalog.format_type(p.prorettype, NULL) AS "Result data type",
  CASE WHEN p.pronargs = 0
    THEN CAST('*' AS pg_catalog.text)
    ELSE pg_catalog.pg_get_function_arguments(p.oid)
  END AS "Argument data types",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind = 'a'
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_function_is_visible(p.oid)
ORDER BY 1, 2, 4;

-- ===== \daS =====
SELECT n.nspname as "Schema",
  p.proname AS "Name",
  pg_catalog.format_type(p.prorettype, NULL) AS "Result data type",
  CASE WHEN p.pronargs = 0
    THEN CAST('*' AS pg_catalog.text)
    ELSE pg_catalog.pg_get_function_arguments(p.oid)
  END AS "Argument data types",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind = 'a'
  AND pg_catalog.pg_function_is_visible(p.oid)
ORDER BY 1, 2, 4;

-- ===== \da app.* =====
SELECT n.nspname as "Schema",
  p.proname AS "Name",
  pg_catalog.format_type(p.prorettype, NULL) AS "Result data type",
  CASE WHEN p.pronargs = 0
    THEN CAST('*' AS pg_catalog.text)
    ELSE pg_catalog.pg_get_function_arguments(p.oid)
  END AS "Argument data types",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind = 'a'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2, 4;

-- ===== \dA =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dA+ =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type",
  amhandler AS "Handler",
  pg_catalog.obj_description(oid, 'pg_am') AS "Description"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAc =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAc+ =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type",
  amhandler AS "Handler",
  pg_catalog.obj_description(oid, 'pg_am') AS "Description"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAf =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAf+ =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type",
  amhandler AS "Handler",
  pg_catalog.obj_description(oid, 'pg_am') AS "Description"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAo =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAo+ =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type",
  amhandler AS "Handler",
  pg_catalog.obj_description(oid, 'pg_am') AS "Description"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAp =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \dAp+ =====
SELECT amname AS "Name",
  CASE amtype WHEN 'i' THEN 'Index' WHEN 't' THEN 'Table' END AS "Type",
  amhandler AS "Handler",
  pg_catalog.obj_description(oid, 'pg_am') AS "Description"
FROM pg_catalog.pg_am
ORDER BY 1;

-- ===== \db =====
SELECT spcname AS "Name",
  pg_catalog.pg_get_userbyid(spcowner) AS "Owner",
  pg_catalog.pg_tablespace_location(oid) AS "Location"
FROM pg_catalog.pg_tablespace
ORDER BY 1;

-- ===== \db+ =====
SELECT spcname AS "Name",
  pg_catalog.pg_get_userbyid(spcowner) AS "Owner",
  pg_catalog.pg_tablespace_location(oid) AS "Location",
  pg_catalog.array_to_string(spcacl, E'\n') AS "Access privileges",
  spcoptions AS "Options",
  pg_catalog.pg_size_pretty(pg_catalog.pg_tablespace_size(oid)) AS "Size",
  pg_catalog.shobj_description(oid, 'pg_tablespace') AS "Description"
FROM pg_catalog.pg_tablespace
ORDER BY 1;

-- ===== \dc =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
WHERE true
  AND n.nspname <> 'pg_catalog'
  AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dc+ =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?",
       d.description AS "Description"
FROM pg_catalog.pg_conversion c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
LEFT JOIN pg_catalog.pg_description d ON d.classoid = c.tableoid
          AND d.objoid = c.oid AND d.objsubid = 0
WHERE true
  AND n.nspname <> 'pg_catalog'
  AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dcS =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
WHERE true
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dconfig =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
WHERE true
  AND n.nspname <> 'pg_catalog'
  AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dconfig+ =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?",
       d.description AS "Description"
FROM pg_catalog.pg_conversion c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
LEFT JOIN pg_catalog.pg_description d ON d.classoid = c.tableoid
          AND d.objoid = c.oid AND d.objsubid = 0
WHERE true
  AND n.nspname <> 'pg_catalog'
  AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dconfig work_mem =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.connamespace
WHERE true
  AND c.conname OPERATOR(pg_catalog.~) '^(work_mem)$' COLLATE pg_catalog.default
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dC =====
SELECT pg_catalog.format_type(castsource, NULL) AS "Source type",
       pg_catalog.format_type(casttarget, NULL) AS "Target type",
       CASE WHEN c.castmethod = 'b' THEN '(binary coercible)'
            WHEN c.castmethod = 'i' THEN '(with inout)'
            ELSE p.proname
       END AS "Function",
       CASE WHEN c.castcontext = 'e' THEN 'no'
            WHEN c.castcontext = 'a' THEN 'in assignment'
            ELSE 'yes'
       END AS "Implicit?"
FROM pg_catalog.pg_cast c LEFT JOIN pg_catalog.pg_proc p
     ON c.castfunc = p.oid
     LEFT JOIN pg_catalog.pg_type ts
     ON c.castsource = ts.oid
     LEFT JOIN pg_catalog.pg_namespace ns
     ON ns.oid = ts.typnamespace
     LEFT JOIN pg_catalog.pg_type tt
     ON c.casttarget = tt.oid
     LEFT JOIN pg_catalog.pg_namespace nt
     ON nt.oid = tt.typnamespace
WHERE ( (true  AND pg_catalog.pg_type_is_visible(ts.oid)
) OR (true  AND pg_catalog.pg_type_is_visible(tt.oid)
) )
ORDER BY 1, 2;

-- ===== \dC+ =====
SELECT pg_catalog.format_type(castsource, NULL) AS "Source type",
       pg_catalog.format_type(casttarget, NULL) AS "Target type",
       CASE WHEN c.castmethod = 'b' THEN '(binary coercible)'
            WHEN c.castmethod = 'i' THEN '(with inout)'
            ELSE p.proname
       END AS "Function",
       CASE WHEN c.castcontext = 'e' THEN 'no'
            WHEN c.castcontext = 'a' THEN 'in assignment'
            ELSE 'yes'
       END AS "Implicit?",
       d.description AS "Description"
FROM pg_catalog.pg_cast c LEFT JOIN pg_catalog.pg_proc p
     ON c.castfunc = p.oid
     LEFT JOIN pg_catalog.pg_type ts
     ON c.castsource = ts.oid
     LEFT JOIN pg_catalog.pg_namespace ns
     ON ns.oid = ts.typnamespace
     LEFT JOIN pg_catalog.pg_type tt
     ON c.casttarget = tt.oid
     LEFT JOIN pg_catalog.pg_namespace nt
     ON nt.oid = tt.typnamespace
     LEFT JOIN pg_catalog.pg_description d
     ON d.classoid = c.tableoid AND d.objoid = c.oid AND d.objsubid = 0
WHERE ( (true  AND pg_catalog.pg_type_is_visible(ts.oid)
) OR (true  AND pg_catalog.pg_type_is_visible(tt.oid)
) )
ORDER BY 1, 2;

-- ===== \dC int4 =====
SELECT pg_catalog.format_type(castsource, NULL) AS "Source type",
       pg_catalog.format_type(casttarget, NULL) AS "Target type",
       CASE WHEN c.castmethod = 'b' THEN '(binary coercible)'
            WHEN c.castmethod = 'i' THEN '(with inout)'
            ELSE p.proname
       END AS "Function",
       CASE WHEN c.castcontext = 'e' THEN 'no'
            WHEN c.castcontext = 'a' THEN 'in assignment'
            ELSE 'yes'
       END AS "Implicit?"
FROM pg_catalog.pg_cast c LEFT JOIN pg_catalog.pg_proc p
     ON c.castfunc = p.oid
     LEFT JOIN pg_catalog.pg_type ts
     ON c.castsource = ts.oid
     LEFT JOIN pg_catalog.pg_namespace ns
     ON ns.oid = ts.typnamespace
     LEFT JOIN pg_catalog.pg_type tt
     ON c.casttarget = tt.oid
     LEFT JOIN pg_catalog.pg_namespace nt
     ON nt.oid = tt.typnamespace
WHERE ( (true  AND (ts.typname OPERATOR(pg_catalog.~) '^(int4)$' COLLATE pg_catalog.default
        OR pg_catalog.format_type(ts.oid, NULL) OPERATOR(pg_catalog.~) '^(int4)$' COLLATE pg_catalog.default)
  AND pg_catalog.pg_type_is_visible(ts.oid)
) OR (true  AND (tt.typname OPERATOR(pg_catalog.~) '^(int4)$' COLLATE pg_catalog.default
        OR pg_catalog.format_type(tt.oid, NULL) OPERATOR(pg_catalog.~) '^(int4)$' COLLATE pg_catalog.default)
  AND pg_catalog.pg_type_is_visible(tt.oid)
) )
ORDER BY 1, 2;

-- ===== \dd =====
SELECT DISTINCT tt.nspname AS "Schema", tt.name AS "Name", tt.object AS "Object", d.description AS "Description"
FROM (
  SELECT pgc.oid as oid, pgc.tableoid AS tableoid,
  n.nspname as nspname,
  CAST(pgc.conname AS pg_catalog.text) as name,  CAST('table constraint' AS pg_catalog.text) as object
  FROM pg_catalog.pg_constraint pgc
    JOIN pg_catalog.pg_class c ON c.oid = pgc.conrelid
    LEFT JOIN pg_catalog.pg_namespace n     ON n.oid = c.relnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_table_is_visible(c.oid)
UNION ALL
  SELECT pgc.oid as oid, pgc.tableoid AS tableoid,
  n.nspname as nspname,
  CAST(pgc.conname AS pg_catalog.text) as name,  CAST('domain constraint' AS pg_catalog.text) as object
  FROM pg_catalog.pg_constraint pgc
    JOIN pg_catalog.pg_type t ON t.oid = pgc.contypid
    LEFT JOIN pg_catalog.pg_namespace n     ON n.oid = t.typnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_type_is_visible(t.oid)
UNION ALL
  SELECT o.oid as oid, o.tableoid as tableoid,
  n.nspname as nspname,
  CAST(o.opcname AS pg_catalog.text) as name,
  CAST('operator class' AS pg_catalog.text) as object
  FROM pg_catalog.pg_opclass o
    JOIN pg_catalog.pg_am am ON o.opcmethod = am.oid
    JOIN pg_catalog.pg_namespace n ON n.oid = o.opcnamespace
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_opclass_is_visible(o.oid)
UNION ALL
  SELECT opf.oid as oid, opf.tableoid as tableoid,
  n.nspname as nspname,
  CAST(opf.opfname AS pg_catalog.text) AS name,
  CAST('operator family' AS pg_catalog.text) as object
  FROM pg_catalog.pg_opfamily opf
    JOIN pg_catalog.pg_am am ON opf.opfmethod = am.oid
    JOIN pg_catalog.pg_namespace n ON opf.opfnamespace = n.oid
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_opfamily_is_visible(opf.oid)
UNION ALL
  SELECT r.oid as oid, r.tableoid as tableoid,
  n.nspname as nspname,
  CAST(r.rulename AS pg_catalog.text) as name,  CAST('rule' AS pg_catalog.text) as object
  FROM pg_catalog.pg_rewrite r
       JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE r.rulename != '_RETURN'
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_table_is_visible(c.oid)
UNION ALL
  SELECT t.oid as oid, t.tableoid as tableoid,
  n.nspname as nspname,
  CAST(t.tgname AS pg_catalog.text) as name,  CAST('trigger' AS pg_catalog.text) as object
  FROM pg_catalog.pg_trigger t
       JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_table_is_visible(c.oid)
) AS tt
  JOIN pg_catalog.pg_description d ON (tt.oid = d.objoid AND tt.tableoid = d.classoid AND d.objsubid = 0)
ORDER BY 1, 2, 3;

-- ===== \dd app.* =====
SELECT DISTINCT tt.nspname AS "Schema", tt.name AS "Name", tt.object AS "Object", d.description AS "Description"
FROM (
  SELECT pgc.oid as oid, pgc.tableoid AS tableoid,
  n.nspname as nspname,
  CAST(pgc.conname AS pg_catalog.text) as name,  CAST('table constraint' AS pg_catalog.text) as object
  FROM pg_catalog.pg_constraint pgc
    JOIN pg_catalog.pg_class c ON c.oid = pgc.conrelid
    LEFT JOIN pg_catalog.pg_namespace n     ON n.oid = c.relnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
UNION ALL
  SELECT pgc.oid as oid, pgc.tableoid AS tableoid,
  n.nspname as nspname,
  CAST(pgc.conname AS pg_catalog.text) as name,  CAST('domain constraint' AS pg_catalog.text) as object
  FROM pg_catalog.pg_constraint pgc
    JOIN pg_catalog.pg_type t ON t.oid = pgc.contypid
    LEFT JOIN pg_catalog.pg_namespace n     ON n.oid = t.typnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
UNION ALL
  SELECT o.oid as oid, o.tableoid as tableoid,
  n.nspname as nspname,
  CAST(o.opcname AS pg_catalog.text) as name,
  CAST('operator class' AS pg_catalog.text) as object
  FROM pg_catalog.pg_opclass o
    JOIN pg_catalog.pg_am am ON o.opcmethod = am.oid
    JOIN pg_catalog.pg_namespace n ON n.oid = o.opcnamespace
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
UNION ALL
  SELECT opf.oid as oid, opf.tableoid as tableoid,
  n.nspname as nspname,
  CAST(opf.opfname AS pg_catalog.text) AS name,
  CAST('operator family' AS pg_catalog.text) as object
  FROM pg_catalog.pg_opfamily opf
    JOIN pg_catalog.pg_am am ON opf.opfmethod = am.oid
    JOIN pg_catalog.pg_namespace n ON opf.opfnamespace = n.oid
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
UNION ALL
  SELECT r.oid as oid, r.tableoid as tableoid,
  n.nspname as nspname,
  CAST(r.rulename AS pg_catalog.text) as name,  CAST('rule' AS pg_catalog.text) as object
  FROM pg_catalog.pg_rewrite r
       JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE r.rulename != '_RETURN'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
UNION ALL
  SELECT t.oid as oid, t.tableoid as tableoid,
  n.nspname as nspname,
  CAST(t.tgname AS pg_catalog.text) as name,  CAST('trigger' AS pg_catalog.text) as object
  FROM pg_catalog.pg_trigger t
       JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
) AS tt
  JOIN pg_catalog.pg_description d ON (tt.oid = d.objoid AND tt.tableoid = d.classoid AND d.objsubid = 0)
ORDER BY 1, 2, 3;

-- ===== \dD =====
SELECT n.nspname as "Schema",
       t.typname as "Name",
       pg_catalog.format_type(t.typbasetype, t.typtypmod) as "Type",
       (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
        WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation) as "Collation",
       CASE WHEN t.typnotnull THEN 'not null' END as "Nullable",
       t.typdefault as "Default",
       pg_catalog.array_to_string(ARRAY(
         SELECT pg_catalog.pg_get_constraintdef(r.oid, true) FROM pg_catalog.pg_constraint r WHERE t.oid = r.contypid
       ), ' ') as "Check"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'd'
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_type_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dD+ =====
SELECT n.nspname as "Schema",
       t.typname as "Name",
       pg_catalog.format_type(t.typbasetype, t.typtypmod) as "Type",
       (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
        WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation) as "Collation",
       CASE WHEN t.typnotnull THEN 'not null' END as "Nullable",
       t.typdefault as "Default",
       pg_catalog.array_to_string(ARRAY(
         SELECT pg_catalog.pg_get_constraintdef(r.oid, true) FROM pg_catalog.pg_constraint r WHERE t.oid = r.contypid
       ), ' ') as "Check",
  pg_catalog.array_to_string(t.typacl, E'\n') AS "Access privileges",
       d.description as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
     LEFT JOIN pg_catalog.pg_description d ON d.classoid = t.tableoid AND d.objoid = t.oid AND d.objsubid = 0
WHERE t.typtype = 'd'
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_type_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dDS =====
SELECT n.nspname as "Schema",
       t.typname as "Name",
       pg_catalog.format_type(t.typbasetype, t.typtypmod) as "Type",
       (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
        WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation) as "Collation",
       CASE WHEN t.typnotnull THEN 'not null' END as "Nullable",
       t.typdefault as "Default",
       pg_catalog.array_to_string(ARRAY(
         SELECT pg_catalog.pg_get_constraintdef(r.oid, true) FROM pg_catalog.pg_constraint r WHERE t.oid = r.contypid
       ), ' ') as "Check"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'd'
  AND pg_catalog.pg_type_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dD app.* =====
SELECT n.nspname as "Schema",
       t.typname as "Name",
       pg_catalog.format_type(t.typbasetype, t.typtypmod) as "Type",
       (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
        WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation) as "Collation",
       CASE WHEN t.typnotnull THEN 'not null' END as "Nullable",
       t.typdefault as "Default",
       pg_catalog.array_to_string(ARRAY(
         SELECT pg_catalog.pg_get_constraintdef(r.oid, true) FROM pg_catalog.pg_constraint r WHERE t.oid = r.contypid
       ), ' ') as "Check"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'd'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2;

-- ===== \ddp =====
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS "Owner",
  n.nspname AS "Schema",
  CASE d.defaclobjtype WHEN 'r' THEN 'table' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'function' WHEN 'T' THEN 'type' WHEN 'n' THEN 'schema' END AS "Type",
  pg_catalog.array_to_string(d.defaclacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_default_acl d
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
ORDER BY 1, 2, 3;

-- ===== \ddp app =====
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS "Owner",
  n.nspname AS "Schema",
  CASE d.defaclobjtype WHEN 'r' THEN 'table' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'function' WHEN 'T' THEN 'type' WHEN 'n' THEN 'schema' END AS "Type",
  pg_catalog.array_to_string(d.defaclacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_default_acl d
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
WHERE (n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
        OR pg_catalog.pg_get_userbyid(d.defaclrole) OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default)
ORDER BY 1, 2, 3;

-- ===== \des =====
SELECT s.srvname AS "Name",
  pg_catalog.pg_get_userbyid(s.srvowner) AS "Owner",
  f.fdwname AS "Foreign-data wrapper"
FROM pg_catalog.pg_foreign_server s
     JOIN pg_catalog.pg_foreign_data_wrapper f ON f.oid=s.srvfdw
ORDER BY 1;

-- ===== \des+ =====
SELECT s.srvname AS "Name",
  pg_catalog.pg_get_userbyid(s.srvowner) AS "Owner",
  f.fdwname AS "Foreign-data wrapper",
  pg_catalog.array_to_string(s.srvacl, E'\n') AS "Access privileges",
  s.srvtype AS "Type",
  s.srvversion AS "Version",
  CASE WHEN srvoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(srvoptions)),  ', ') || ')'   END AS "FDW options",
  d.description AS "Description"
FROM pg_catalog.pg_foreign_server s
     JOIN pg_catalog.pg_foreign_data_wrapper f ON f.oid=s.srvfdw
LEFT JOIN pg_catalog.pg_description d
       ON d.classoid = s.tableoid AND d.objoid = s.oid AND d.objsubid = 0
ORDER BY 1;

-- ===== \det =====
SELECT n.nspname AS "Schema",
  c.relname AS "Table",
  s.srvname AS "Server"
FROM pg_catalog.pg_foreign_table ft
  INNER JOIN pg_catalog.pg_class c ON c.oid = ft.ftrelid
  INNER JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  INNER JOIN pg_catalog.pg_foreign_server s ON s.oid = ft.ftserver
WHERE pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \det+ =====
SELECT n.nspname AS "Schema",
  c.relname AS "Table",
  s.srvname AS "Server",
 CASE WHEN ftoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(ftoptions)),  ', ') || ')'   END AS "FDW options",
  d.description AS "Description"
FROM pg_catalog.pg_foreign_table ft
  INNER JOIN pg_catalog.pg_class c ON c.oid = ft.ftrelid
  INNER JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  INNER JOIN pg_catalog.pg_foreign_server s ON s.oid = ft.ftserver
   LEFT JOIN pg_catalog.pg_description d
          ON d.classoid = c.tableoid AND d.objoid = c.oid AND d.objsubid = 0
WHERE pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \deu =====
SELECT um.srvname AS "Server",
  um.usename AS "User name"
FROM pg_catalog.pg_user_mappings um
ORDER BY 1, 2;

-- ===== \deu+ =====
SELECT um.srvname AS "Server",
  um.usename AS "User name",
 CASE WHEN umoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(umoptions)),  ', ') || ')'   END AS "FDW options"
FROM pg_catalog.pg_user_mappings um
ORDER BY 1, 2;

-- ===== \dew =====
SELECT fdw.fdwname AS "Name",
  pg_catalog.pg_get_userbyid(fdw.fdwowner) AS "Owner",
  fdw.fdwhandler::pg_catalog.regproc AS "Handler",
  fdw.fdwvalidator::pg_catalog.regproc AS "Validator"
FROM pg_catalog.pg_foreign_data_wrapper fdw
ORDER BY 1;

-- ===== \dew+ =====
SELECT fdw.fdwname AS "Name",
  pg_catalog.pg_get_userbyid(fdw.fdwowner) AS "Owner",
  fdw.fdwhandler::pg_catalog.regproc AS "Handler",
  fdw.fdwvalidator::pg_catalog.regproc AS "Validator",
  pg_catalog.array_to_string(fdwacl, E'\n') AS "Access privileges",
 CASE WHEN fdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(fdwoptions)),  ', ') || ')'   END AS "FDW options",
  d.description AS "Description" 
FROM pg_catalog.pg_foreign_data_wrapper fdw
LEFT JOIN pg_catalog.pg_description d
       ON d.classoid = fdw.tableoid AND d.objoid = fdw.oid AND d.objsubid = 0
ORDER BY 1;

-- ===== \df =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \df+ =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'c' THEN 'coordinator'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
 CASE
  WHEN p.proparallel = 'r' THEN 'restricted'
  WHEN p.proparallel = 's' THEN 'safe'
  WHEN p.proparallel = 'u' THEN 'unsafe'
 END as "Parallel",
 pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 pg_catalog.array_to_string(p.proacl, E'\n') AS "Access privileges",
 l.lanname as "Language",
 p.prosrc as "Source code",
 pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfS =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE pg_catalog.pg_function_is_visible(p.oid)
ORDER BY 1, 2, 4;

-- ===== \df app.* =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2, 4;

-- ===== \df+ app.add =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'c' THEN 'coordinator'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
 CASE
  WHEN p.proparallel = 'r' THEN 'restricted'
  WHEN p.proparallel = 's' THEN 'safe'
  WHEN p.proparallel = 'u' THEN 'unsafe'
 END as "Parallel",
 pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 pg_catalog.array_to_string(p.proacl, E'\n') AS "Access privileges",
 l.lanname as "Language",
 p.prosrc as "Source code",
 pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE p.proname OPERATOR(pg_catalog.~) '^(add)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2, 4;

-- ===== \dfa =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE (
       p.prokind = 'a'
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfa+ =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'c' THEN 'coordinator'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
 CASE
  WHEN p.proparallel = 'r' THEN 'restricted'
  WHEN p.proparallel = 's' THEN 'safe'
  WHEN p.proparallel = 'u' THEN 'unsafe'
 END as "Parallel",
 pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 pg_catalog.array_to_string(p.proacl, E'\n') AS "Access privileges",
 l.lanname as "Language",
 p.prosrc as "Source code",
 pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE (
       p.prokind = 'a'
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfn =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind <> 'a'
      AND p.prokind <> 'p'
      AND p.prorettype <> 'pg_catalog.trigger'::pg_catalog.regtype
      AND p.prokind <> 'w'
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfn+ =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'c' THEN 'coordinator'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
 CASE
  WHEN p.proparallel = 'r' THEN 'restricted'
  WHEN p.proparallel = 's' THEN 'safe'
  WHEN p.proparallel = 'u' THEN 'unsafe'
 END as "Parallel",
 pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 pg_catalog.array_to_string(p.proacl, E'\n') AS "Access privileges",
 l.lanname as "Language",
 p.prosrc as "Source code",
 pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE p.prokind <> 'a'
      AND p.prokind <> 'p'
      AND p.prorettype <> 'pg_catalog.trigger'::pg_catalog.regtype
      AND p.prokind <> 'w'
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfp =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE (
       p.prokind = 'p'
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfp+ =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'c' THEN 'coordinator'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
 CASE
  WHEN p.proparallel = 'r' THEN 'restricted'
  WHEN p.proparallel = 's' THEN 'safe'
  WHEN p.proparallel = 'u' THEN 'unsafe'
 END as "Parallel",
 pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 pg_catalog.array_to_string(p.proacl, E'\n') AS "Access privileges",
 l.lanname as "Language",
 p.prosrc as "Source code",
 pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE (
       p.prokind = 'p'
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dft =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE (
       p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dft+ =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'c' THEN 'coordinator'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
 CASE
  WHEN p.proparallel = 'r' THEN 'restricted'
  WHEN p.proparallel = 's' THEN 'safe'
  WHEN p.proparallel = 'u' THEN 'unsafe'
 END as "Parallel",
 pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 pg_catalog.array_to_string(p.proacl, E'\n') AS "Access privileges",
 l.lanname as "Language",
 p.prosrc as "Source code",
 pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE (
       p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfw =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE (
       p.prokind = 'w'
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfw+ =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE p.prokind
  WHEN 'a' THEN 'agg'
  WHEN 'w' THEN 'window'
  WHEN 'p' THEN 'proc'
  ELSE 'func'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'c' THEN 'coordinator'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
 CASE
  WHEN p.proparallel = 'r' THEN 'restricted'
  WHEN p.proparallel = 's' THEN 'safe'
  WHEN p.proparallel = 'u' THEN 'unsafe'
 END as "Parallel",
 pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 pg_catalog.array_to_string(p.proacl, E'\n') AS "Access privileges",
 l.lanname as "Language",
 p.prosrc as "Source code",
 pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE (
       p.prokind = 'w'
      )
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dF =====
SELECT
   n.nspname as "Schema",
   c.cfgname as "Name",
   pg_catalog.obj_description(c.oid, 'pg_ts_config') as "Description"
FROM pg_catalog.pg_ts_config c
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.cfgnamespace
WHERE pg_catalog.pg_ts_config_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dF+ =====
SELECT c.oid, c.cfgname,
   n.nspname,
   p.prsname,
   np.nspname as pnspname
FROM pg_catalog.pg_ts_config c
   LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.cfgnamespace,
 pg_catalog.pg_ts_parser p
   LEFT JOIN pg_catalog.pg_namespace np ON np.oid = p.prsnamespace
WHERE  p.oid = c.cfgparser
  AND pg_catalog.pg_ts_config_is_visible(c.oid)
ORDER BY 3, 2;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12892' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12894' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12896' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12898' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12900' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12902' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12904' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12906' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12908' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12910' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12912' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12914' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12916' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12918' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12920' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12922' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12924' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '3748' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12926' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12928' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12930' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;
SELECT
  ( SELECT t.alias FROM
    pg_catalog.ts_token_type(c.cfgparser) AS t
    WHERE t.tokid = m.maptokentype ) AS "Token",
  pg_catalog.btrim(
    ARRAY( SELECT mm.mapdict::pg_catalog.regdictionary
           FROM pg_catalog.pg_ts_config_map AS mm
           WHERE mm.mapcfg = m.mapcfg AND mm.maptokentype = m.maptokentype
           ORDER BY mapcfg, maptokentype, mapseqno
    ) :: pg_catalog.text,
  '{}') AS "Dictionaries"
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m
WHERE c.oid = '12932' AND m.mapcfg = c.oid
GROUP BY m.mapcfg, m.maptokentype, c.cfgparser
ORDER BY 1;

-- ===== \dFd =====
SELECT
  n.nspname as "Schema",
  d.dictname as "Name",
  pg_catalog.obj_description(d.oid, 'pg_ts_dict') as "Description"
FROM pg_catalog.pg_ts_dict d
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.dictnamespace
WHERE pg_catalog.pg_ts_dict_is_visible(d.oid)
ORDER BY 1, 2;

-- ===== \dFd+ =====
SELECT
  n.nspname as "Schema",
  d.dictname as "Name",
  ( SELECT COALESCE(nt.nspname, '(null)')::pg_catalog.text || '.' || t.tmplname FROM
    pg_catalog.pg_ts_template t
    LEFT JOIN pg_catalog.pg_namespace nt ON nt.oid = t.tmplnamespace
    WHERE d.dicttemplate = t.oid ) AS  "Template",
  d.dictinitoption as "Init options",
  pg_catalog.obj_description(d.oid, 'pg_ts_dict') as "Description"
FROM pg_catalog.pg_ts_dict d
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.dictnamespace
WHERE pg_catalog.pg_ts_dict_is_visible(d.oid)
ORDER BY 1, 2;

-- ===== \dFp =====
SELECT
  n.nspname as "Schema",
  p.prsname as "Name",
  pg_catalog.obj_description(p.oid, 'pg_ts_parser') as "Description"
FROM pg_catalog.pg_ts_parser p
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.prsnamespace
WHERE pg_catalog.pg_ts_parser_is_visible(p.oid)
ORDER BY 1, 2;

-- ===== \dFp+ =====
SELECT p.oid,
  n.nspname,
  p.prsname
FROM pg_catalog.pg_ts_parser p
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.prsnamespace
WHERE pg_catalog.pg_ts_parser_is_visible(p.oid)
ORDER BY 1, 2;
SELECT 'Start parse' AS "Method",
   p.prsstart::pg_catalog.regproc AS "Function",
   pg_catalog.obj_description(p.prsstart, 'pg_proc') as "Description"
 FROM pg_catalog.pg_ts_parser p
 WHERE p.oid = '3722'
UNION ALL
SELECT 'Get next token',
   p.prstoken::pg_catalog.regproc,
   pg_catalog.obj_description(p.prstoken, 'pg_proc')
 FROM pg_catalog.pg_ts_parser p
 WHERE p.oid = '3722'
UNION ALL
SELECT 'End parse',
   p.prsend::pg_catalog.regproc,
   pg_catalog.obj_description(p.prsend, 'pg_proc')
 FROM pg_catalog.pg_ts_parser p
 WHERE p.oid = '3722'
UNION ALL
SELECT 'Get headline',
   p.prsheadline::pg_catalog.regproc,
   pg_catalog.obj_description(p.prsheadline, 'pg_proc')
 FROM pg_catalog.pg_ts_parser p
 WHERE p.oid = '3722'
UNION ALL
SELECT 'Get token types',
   p.prslextype::pg_catalog.regproc,
   pg_catalog.obj_description(p.prslextype, 'pg_proc')
 FROM pg_catalog.pg_ts_parser p
 WHERE p.oid = '3722';
SELECT t.alias as "Token name",
  t.description as "Description"
FROM pg_catalog.ts_token_type( '3722'::pg_catalog.oid ) as t
ORDER BY 1;

-- ===== \dFt =====
SELECT
  n.nspname AS "Schema",
  t.tmplname AS "Name",
  pg_catalog.obj_description(t.oid, 'pg_ts_template') AS "Description"
FROM pg_catalog.pg_ts_template t
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.tmplnamespace
WHERE pg_catalog.pg_ts_template_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dFt+ =====
SELECT
  n.nspname AS "Schema",
  t.tmplname AS "Name",
  t.tmplinit::pg_catalog.regproc AS "Init",
  t.tmpllexize::pg_catalog.regproc AS "Lexize",
  pg_catalog.obj_description(t.oid, 'pg_ts_template') AS "Description"
FROM pg_catalog.pg_ts_template t
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.tmplnamespace
WHERE pg_catalog.pg_ts_template_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dl =====
SELECT oid as "ID",
  pg_catalog.pg_get_userbyid(lomowner) as "Owner",
  pg_catalog.obj_description(oid, 'pg_largeobject') as "Description"
  FROM pg_catalog.pg_largeobject_metadata   ORDER BY oid;

-- ===== \dl+ =====
SELECT oid as "ID",
  pg_catalog.pg_get_userbyid(lomowner) as "Owner",
  pg_catalog.obj_description(oid, 'pg_largeobject') as "Description"
  FROM pg_catalog.pg_largeobject_metadata   ORDER BY oid;

-- ===== \lo_list =====
SELECT oid as "ID",
  pg_catalog.pg_get_userbyid(lomowner) as "Owner",
  pg_catalog.obj_description(oid, 'pg_largeobject') as "Description"
  FROM pg_catalog.pg_largeobject_metadata   ORDER BY oid;

-- ===== \lo_list+ =====
-- (no queries sent)

-- ===== \dL =====
SELECT l.lanname AS "Name",
       pg_catalog.pg_get_userbyid(l.lanowner) as "Owner",
       l.lanpltrusted AS "Trusted",
       d.description AS "Description"
FROM pg_catalog.pg_language l
LEFT JOIN pg_catalog.pg_description d
  ON d.classoid = l.tableoid AND d.objoid = l.oid
  AND d.objsubid = 0
WHERE l.lanplcallfoid != 0
ORDER BY 1;

-- ===== \dL+ =====
SELECT l.lanname AS "Name",
       pg_catalog.pg_get_userbyid(l.lanowner) as "Owner",
       l.lanpltrusted AS "Trusted",
       NOT l.lanispl AS "Internal language",
       l.lanplcallfoid::pg_catalog.regprocedure AS "Call handler",
       l.lanvalidator::pg_catalog.regprocedure AS "Validator",
       l.laninline::pg_catalog.regprocedure AS "Inline handler",
       pg_catalog.array_to_string(l.lanacl, E'\n') AS "Access privileges",
       d.description AS "Description"
FROM pg_catalog.pg_language l
LEFT JOIN pg_catalog.pg_description d
  ON d.classoid = l.tableoid AND d.objoid = l.oid
  AND d.objsubid = 0
WHERE l.lanplcallfoid != 0
ORDER BY 1;

-- ===== \dLS =====
SELECT l.lanname AS "Name",
       pg_catalog.pg_get_userbyid(l.lanowner) as "Owner",
       l.lanpltrusted AS "Trusted",
       d.description AS "Description"
FROM pg_catalog.pg_language l
LEFT JOIN pg_catalog.pg_description d
  ON d.classoid = l.tableoid AND d.objoid = l.oid
  AND d.objsubid = 0
ORDER BY 1;

-- ===== \do =====
SELECT n.nspname as "Schema",
  o.oprname AS "Name",
  CASE WHEN o.oprkind='l' THEN NULL ELSE pg_catalog.format_type(o.oprleft, NULL) END AS "Left arg type",
  CASE WHEN o.oprkind='r' THEN NULL ELSE pg_catalog.format_type(o.oprright, NULL) END AS "Right arg type",
  pg_catalog.format_type(o.oprresult, NULL) AS "Result type",
  coalesce(pg_catalog.obj_description(o.oid, 'pg_operator'),
           pg_catalog.obj_description(o.oprcode, 'pg_proc')) AS "Description"
FROM pg_catalog.pg_operator o
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_operator_is_visible(o.oid)
ORDER BY 1, 2, 3, 4;

-- ===== \do+ =====
SELECT n.nspname as "Schema",
  o.oprname AS "Name",
  CASE WHEN o.oprkind='l' THEN NULL ELSE pg_catalog.format_type(o.oprleft, NULL) END AS "Left arg type",
  CASE WHEN o.oprkind='r' THEN NULL ELSE pg_catalog.format_type(o.oprright, NULL) END AS "Right arg type",
  pg_catalog.format_type(o.oprresult, NULL) AS "Result type",
  o.oprcode AS "Function",
  coalesce(pg_catalog.obj_description(o.oid, 'pg_operator'),
           pg_catalog.obj_description(o.oprcode, 'pg_proc')) AS "Description"
FROM pg_catalog.pg_operator o
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_operator_is_visible(o.oid)
ORDER BY 1, 2, 3, 4;

-- ===== \doS =====
SELECT n.nspname as "Schema",
  o.oprname AS "Name",
  CASE WHEN o.oprkind='l' THEN NULL ELSE pg_catalog.format_type(o.oprleft, NULL) END AS "Left arg type",
  CASE WHEN o.oprkind='r' THEN NULL ELSE pg_catalog.format_type(o.oprright, NULL) END AS "Right arg type",
  pg_catalog.format_type(o.oprresult, NULL) AS "Result type",
  coalesce(pg_catalog.obj_description(o.oid, 'pg_operator'),
           pg_catalog.obj_description(o.oprcode, 'pg_proc')) AS "Description"
FROM pg_catalog.pg_operator o
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
WHERE pg_catalog.pg_operator_is_visible(o.oid)
ORDER BY 1, 2, 3, 4;

-- ===== \do app.* =====
SELECT n.nspname as "Schema",
  o.oprname AS "Name",
  CASE WHEN o.oprkind='l' THEN NULL ELSE pg_catalog.format_type(o.oprleft, NULL) END AS "Left arg type",
  CASE WHEN o.oprkind='r' THEN NULL ELSE pg_catalog.format_type(o.oprright, NULL) END AS "Right arg type",
  pg_catalog.format_type(o.oprresult, NULL) AS "Result type",
  coalesce(pg_catalog.obj_description(o.oid, 'pg_operator'),
           pg_catalog.obj_description(o.oprcode, 'pg_proc')) AS "Description"
FROM pg_catalog.pg_operator o
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2, 3, 4;

-- ===== \do <+> =====
SELECT n.nspname as "Schema",
  o.oprname AS "Name",
  CASE WHEN o.oprkind='l' THEN NULL ELSE pg_catalog.format_type(o.oprleft, NULL) END AS "Left arg type",
  CASE WHEN o.oprkind='r' THEN NULL ELSE pg_catalog.format_type(o.oprright, NULL) END AS "Right arg type",
  pg_catalog.format_type(o.oprresult, NULL) AS "Result type",
  coalesce(pg_catalog.obj_description(o.oid, 'pg_operator'),
           pg_catalog.obj_description(o.oprcode, 'pg_proc')) AS "Description"
FROM pg_catalog.pg_operator o
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
WHERE o.oprname OPERATOR(pg_catalog.~) E'^(<\\+>)$' COLLATE pg_catalog.default
  AND pg_catalog.pg_operator_is_visible(o.oid)
ORDER BY 1, 2, 3, 4;

-- ===== \dO =====
SELECT n.nspname AS "Schema",
       c.collname AS "Name",
       c.collcollate AS "Collate",
       c.collctype AS "Ctype",
       CASE c.collprovider WHEN 'd' THEN 'default' WHEN 'c' THEN 'libc' WHEN 'i' THEN 'icu' END AS "Provider",
       CASE WHEN c.collisdeterministic THEN 'yes' ELSE 'no' END AS "Deterministic?"
FROM pg_catalog.pg_collation c, pg_catalog.pg_namespace n
WHERE n.oid = c.collnamespace
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND c.collencoding IN (-1, pg_catalog.pg_char_to_encoding(pg_catalog.getdatabaseencoding()))
  AND pg_catalog.pg_collation_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dO+ =====
SELECT n.nspname AS "Schema",
       c.collname AS "Name",
       c.collcollate AS "Collate",
       c.collctype AS "Ctype",
       CASE c.collprovider WHEN 'd' THEN 'default' WHEN 'c' THEN 'libc' WHEN 'i' THEN 'icu' END AS "Provider",
       CASE WHEN c.collisdeterministic THEN 'yes' ELSE 'no' END AS "Deterministic?",
       pg_catalog.obj_description(c.oid, 'pg_collation') AS "Description"
FROM pg_catalog.pg_collation c, pg_catalog.pg_namespace n
WHERE n.oid = c.collnamespace
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND c.collencoding IN (-1, pg_catalog.pg_char_to_encoding(pg_catalog.getdatabaseencoding()))
  AND pg_catalog.pg_collation_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dOS =====
SELECT n.nspname AS "Schema",
       c.collname AS "Name",
       c.collcollate AS "Collate",
       c.collctype AS "Ctype",
       CASE c.collprovider WHEN 'd' THEN 'default' WHEN 'c' THEN 'libc' WHEN 'i' THEN 'icu' END AS "Provider",
       CASE WHEN c.collisdeterministic THEN 'yes' ELSE 'no' END AS "Deterministic?"
FROM pg_catalog.pg_collation c, pg_catalog.pg_namespace n
WHERE n.oid = c.collnamespace
      AND c.collencoding IN (-1, pg_catalog.pg_char_to_encoding(pg_catalog.getdatabaseencoding()))
  AND pg_catalog.pg_collation_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dp =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT polname
    || CASE WHEN NOT polpermissive THEN
       E' (RESTRICTIVE)'
       ELSE '' END
    || CASE WHEN polcmd != '*' THEN
           E' (' || polcmd || E'):'
       ELSE E':'
       END
    || CASE WHEN polqual IS NOT NULL THEN
           E'\n  (u): ' || pg_catalog.pg_get_expr(polqual, polrelid)
       ELSE E''
       END
    || CASE WHEN polwithcheck IS NOT NULL THEN
           E'\n  (c): ' || pg_catalog.pg_get_expr(polwithcheck, polrelid)
       ELSE E''
       END    || CASE WHEN polroles <> '{0}' THEN
           E'\n  to: ' || pg_catalog.array_to_string(
               ARRAY(
                   SELECT rolname
                   FROM pg_catalog.pg_roles
                   WHERE oid = ANY (polroles)
                   ORDER BY 1
               ), E', ')
       ELSE E''
       END
    FROM pg_catalog.pg_policy pol
    WHERE polrelid = c.oid), E'\n')
    AS "Policies"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','p')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dpS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT polname
    || CASE WHEN NOT polpermissive THEN
       E' (RESTRICTIVE)'
       ELSE '' END
    || CASE WHEN polcmd != '*' THEN
           E' (' || polcmd || E'):'
       ELSE E':'
       END
    || CASE WHEN polqual IS NOT NULL THEN
           E'\n  (u): ' || pg_catalog.pg_get_expr(polqual, polrelid)
       ELSE E''
       END
    || CASE WHEN polwithcheck IS NOT NULL THEN
           E'\n  (c): ' || pg_catalog.pg_get_expr(polwithcheck, polrelid)
       ELSE E''
       END    || CASE WHEN polroles <> '{0}' THEN
           E'\n  to: ' || pg_catalog.array_to_string(
               ARRAY(
                   SELECT rolname
                   FROM pg_catalog.pg_roles
                   WHERE oid = ANY (polroles)
                   ORDER BY 1
               ), E', ')
       ELSE E''
       END
    FROM pg_catalog.pg_policy pol
    WHERE polrelid = c.oid), E'\n')
    AS "Policies"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','p')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dp app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT polname
    || CASE WHEN NOT polpermissive THEN
       E' (RESTRICTIVE)'
       ELSE '' END
    || CASE WHEN polcmd != '*' THEN
           E' (' || polcmd || E'):'
       ELSE E':'
       END
    || CASE WHEN polqual IS NOT NULL THEN
           E'\n  (u): ' || pg_catalog.pg_get_expr(polqual, polrelid)
       ELSE E''
       END
    || CASE WHEN polwithcheck IS NOT NULL THEN
           E'\n  (c): ' || pg_catalog.pg_get_expr(polwithcheck, polrelid)
       ELSE E''
       END    || CASE WHEN polroles <> '{0}' THEN
           E'\n  to: ' || pg_catalog.array_to_string(
               ARRAY(
                   SELECT rolname
                   FROM pg_catalog.pg_roles
                   WHERE oid = ANY (polroles)
                   ORDER BY 1
               ), E', ')
       ELSE E''
       END
    FROM pg_catalog.pg_policy pol
    WHERE polrelid = c.oid), E'\n')
    AS "Policies"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','p')
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2;

-- ===== \dP =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  CASE c.relkind WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
 c2.oid::pg_catalog.regclass as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('p','I','')
 AND NOT c.relispartition
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Type" DESC, "Name";

-- ===== \dP+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  CASE c.relkind WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
 c2.oid::pg_catalog.regclass as "Table",
  s.tps as "Total size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid,
     LATERAL (SELECT pg_catalog.pg_size_pretty(sum(
                 CASE WHEN ppt.isleaf AND ppt.level = 1
                      THEN pg_catalog.pg_table_size(ppt.relid) ELSE 0 END)) AS dps,
                     pg_catalog.pg_size_pretty(sum(pg_catalog.pg_table_size(ppt.relid))) AS tps
              FROM pg_catalog.pg_partition_tree(c.oid) ppt) s
WHERE c.relkind IN ('p','I','')
 AND NOT c.relispartition
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Type" DESC, "Name";

-- ===== \dPn =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  CASE c.relkind WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  inh.inhparent::pg_catalog.regclass as "Parent name",
 c2.oid::pg_catalog.regclass as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
     LEFT JOIN pg_catalog.pg_inherits inh ON c.oid = inh.inhrelid
WHERE c.relkind IN ('p','I','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Type" DESC, "Parent name" NULLS FIRST, "Name";

-- ===== \dPn+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  CASE c.relkind WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  inh.inhparent::pg_catalog.regclass as "Parent name",
 c2.oid::pg_catalog.regclass as "Table",
  s.dps as "Leaf partition size",
  s.tps as "Total size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
     LEFT JOIN pg_catalog.pg_inherits inh ON c.oid = inh.inhrelid,
     LATERAL (SELECT pg_catalog.pg_size_pretty(sum(
                 CASE WHEN ppt.isleaf AND ppt.level = 1
                      THEN pg_catalog.pg_table_size(ppt.relid) ELSE 0 END)) AS dps,
                     pg_catalog.pg_size_pretty(sum(pg_catalog.pg_table_size(ppt.relid))) AS tps
              FROM pg_catalog.pg_partition_tree(c.oid) ppt) s
WHERE c.relkind IN ('p','I','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Type" DESC, "Parent name" NULLS FIRST, "Name";

-- ===== \dPi =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.oid::pg_catalog.regclass as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('I','')
 AND NOT c.relispartition
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Name";

-- ===== \dPi+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.oid::pg_catalog.regclass as "Table",
  s.tps as "Total size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid,
     LATERAL (SELECT pg_catalog.pg_size_pretty(sum(
                 CASE WHEN ppt.isleaf AND ppt.level = 1
                      THEN pg_catalog.pg_table_size(ppt.relid) ELSE 0 END)) AS dps,
                     pg_catalog.pg_size_pretty(sum(pg_catalog.pg_table_size(ppt.relid))) AS tps
              FROM pg_catalog.pg_partition_tree(c.oid) ppt) s
WHERE c.relkind IN ('I','')
 AND NOT c.relispartition
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Name";

-- ===== \dPt =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('p','')
 AND NOT c.relispartition
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Name";

-- ===== \dPt+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  s.tps as "Total size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
     LATERAL (SELECT pg_catalog.pg_size_pretty(sum(
                 CASE WHEN ppt.isleaf AND ppt.level = 1
                      THEN pg_catalog.pg_table_size(ppt.relid) ELSE 0 END)) AS dps,
                     pg_catalog.pg_size_pretty(sum(pg_catalog.pg_table_size(ppt.relid))) AS tps
              FROM pg_catalog.pg_partition_tree(c.oid) ppt) s
WHERE c.relkind IN ('p','')
 AND NOT c.relispartition
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Name";

-- ===== \dPtn =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  inh.inhparent::pg_catalog.regclass as "Parent name"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_inherits inh ON c.oid = inh.inhrelid
WHERE c.relkind IN ('p','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY "Schema", "Parent name" NULLS FIRST, "Name";

-- ===== \dP app.part =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  CASE c.relkind WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  inh.inhparent::pg_catalog.regclass as "Parent name",
 c2.oid::pg_catalog.regclass as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
     LEFT JOIN pg_catalog.pg_inherits inh ON c.oid = inh.inhrelid
WHERE c.relkind IN ('p','I','')
      AND n.nspname !~ '^pg_toast'
  AND c.relname OPERATOR(pg_catalog.~) '^(part)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY "Schema", "Type" DESC, "Parent name" NULLS FIRST, "Name";

-- ===== \dP+ app.part =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  CASE c.relkind WHEN 'p' THEN 'partitioned table' WHEN 'I' THEN 'partitioned index' END as "Type",
  inh.inhparent::pg_catalog.regclass as "Parent name",
 c2.oid::pg_catalog.regclass as "Table",
  s.tps as "Total size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
     LEFT JOIN pg_catalog.pg_inherits inh ON c.oid = inh.inhrelid,
     LATERAL (SELECT pg_catalog.pg_size_pretty(sum(
                 CASE WHEN ppt.isleaf AND ppt.level = 1
                      THEN pg_catalog.pg_table_size(ppt.relid) ELSE 0 END)) AS dps,
                     pg_catalog.pg_size_pretty(sum(pg_catalog.pg_table_size(ppt.relid))) AS tps
              FROM pg_catalog.pg_partition_tree(c.oid) ppt) s
WHERE c.relkind IN ('p','I','')
      AND n.nspname !~ '^pg_toast'
  AND c.relname OPERATOR(pg_catalog.~) '^(part)$' COLLATE pg_catalog.default
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY "Schema", "Type" DESC, "Parent name" NULLS FIRST, "Name";

-- ===== \dRp =====
SELECT pubname AS "Name",
  pg_catalog.pg_get_userbyid(pubowner) AS "Owner",
  puballtables AS "All tables",
  pubinsert AS "Inserts",
  pubupdate AS "Updates",
  pubdelete AS "Deletes",
  pubtruncate AS "Truncates"
FROM pg_catalog.pg_publication
ORDER BY 1;

-- ===== \dRp+ =====
SELECT oid, pubname,
  pg_catalog.pg_get_userbyid(pubowner) AS owner,
  puballtables, pubinsert, pubupdate, pubdelete, pubtruncate
FROM pg_catalog.pg_publication
ORDER BY 2;
SELECT n.nspname, c.relname
FROM pg_catalog.pg_class c,
     pg_catalog.pg_namespace n,
     pg_catalog.pg_publication_rel pr
WHERE c.relnamespace = n.oid
  AND c.oid = pr.prrelid
  AND pr.prpubid = '16654'
ORDER BY 1,2;

-- ===== \dRp scrape_pub =====
SELECT pubname AS "Name",
  pg_catalog.pg_get_userbyid(pubowner) AS "Owner",
  puballtables AS "All tables",
  pubinsert AS "Inserts",
  pubupdate AS "Updates",
  pubdelete AS "Deletes",
  pubtruncate AS "Truncates"
FROM pg_catalog.pg_publication
WHERE pubname OPERATOR(pg_catalog.~) '^(scrape_pub)$' COLLATE pg_catalog.default
ORDER BY 1;

-- ===== \dRp+ scrape_pub =====
SELECT oid, pubname,
  pg_catalog.pg_get_userbyid(pubowner) AS owner,
  puballtables, pubinsert, pubupdate, pubdelete, pubtruncate
FROM pg_catalog.pg_publication
WHERE pubname OPERATOR(pg_catalog.~) '^(scrape_pub)$' COLLATE pg_catalog.default
ORDER BY 2;
SELECT n.nspname, c.relname
FROM pg_catalog.pg_class c,
     pg_catalog.pg_namespace n,
     pg_catalog.pg_publication_rel pr
WHERE c.relnamespace = n.oid
  AND c.oid = pr.prrelid
  AND pr.prpubid = '16654'
ORDER BY 1,2;

-- ===== \dRs =====
SELECT subname AS "Name"
,  pg_catalog.pg_get_userbyid(subowner) AS "Owner"
,  subenabled AS "Enabled"
,  subpublications AS "Publication"
FROM pg_catalog.pg_subscription
WHERE subdbid = (SELECT oid
                 FROM pg_catalog.pg_database
                 WHERE datname = pg_catalog.current_database())ORDER BY 1;

-- ===== \dRs+ =====
SELECT subname AS "Name"
,  pg_catalog.pg_get_userbyid(subowner) AS "Owner"
,  subenabled AS "Enabled"
,  subpublications AS "Publication"
,  subsynccommit AS "Synchronous commit"
,  subconninfo AS "Conninfo"
FROM pg_catalog.pg_subscription
WHERE subdbid = (SELECT oid
                 FROM pg_catalog.pg_database
                 WHERE datname = pg_catalog.current_database())ORDER BY 1;

-- ===== \dT =====
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT n.nspname as "Schema",
  pg_catalog.format_type(t.oid, NULL) AS "Name",
  pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_type_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dT+ =====
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT n.nspname as "Schema",
  pg_catalog.format_type(t.oid, NULL) AS "Name",
  t.typname AS "Internal name",
  CASE WHEN t.typrelid != 0
      THEN CAST('tuple' AS pg_catalog.text)
    WHEN t.typlen < 0
      THEN CAST('var' AS pg_catalog.text)
    ELSE CAST(t.typlen AS pg_catalog.text)
  END AS "Size",
  pg_catalog.array_to_string(
      ARRAY(
          SELECT e.enumlabel
          FROM pg_catalog.pg_enum e
          WHERE e.enumtypid = t.oid
          ORDER BY e.enumsortorder
      ),
      E'\n'
  ) AS "Elements",
 pg_catalog.array_to_string(te.typoptions, ',') AS "Type Options",
  pg_catalog.pg_get_userbyid(t.typowner) AS "Owner",
pg_catalog.array_to_string(t.typacl, E'\n') AS "Access privileges",
    pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace

   LEFT OUTER JOIN pg_catalog.pg_type_encoding te ON te.typid = t.oid WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_type_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dTS =====
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT n.nspname as "Schema",
  pg_catalog.format_type(t.oid, NULL) AS "Name",
  pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND pg_catalog.pg_type_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dT app.* =====
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT n.nspname as "Schema",
  pg_catalog.format_type(t.oid, NULL) AS "Name",
  pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2;

-- ===== \dT+ app.mood =====
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT n.nspname as "Schema",
  pg_catalog.format_type(t.oid, NULL) AS "Name",
  t.typname AS "Internal name",
  CASE WHEN t.typrelid != 0
      THEN CAST('tuple' AS pg_catalog.text)
    WHEN t.typlen < 0
      THEN CAST('var' AS pg_catalog.text)
    ELSE CAST(t.typlen AS pg_catalog.text)
  END AS "Size",
  pg_catalog.array_to_string(
      ARRAY(
          SELECT e.enumlabel
          FROM pg_catalog.pg_enum e
          WHERE e.enumtypid = t.oid
          ORDER BY e.enumsortorder
      ),
      E'\n'
  ) AS "Elements",
 pg_catalog.array_to_string(te.typoptions, ',') AS "Type Options",
  pg_catalog.pg_get_userbyid(t.typowner) AS "Owner",
pg_catalog.array_to_string(t.typacl, E'\n') AS "Access privileges",
    pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace

   LEFT OUTER JOIN pg_catalog.pg_type_encoding te ON te.typid = t.oid WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND (t.typname OPERATOR(pg_catalog.~) '^(mood)$' COLLATE pg_catalog.default
        OR pg_catalog.format_type(t.oid, NULL) OPERATOR(pg_catalog.~) '^(mood)$' COLLATE pg_catalog.default)
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2;

-- ===== \dT+ app.pair =====
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT n.nspname as "Schema",
  pg_catalog.format_type(t.oid, NULL) AS "Name",
  t.typname AS "Internal name",
  CASE WHEN t.typrelid != 0
      THEN CAST('tuple' AS pg_catalog.text)
    WHEN t.typlen < 0
      THEN CAST('var' AS pg_catalog.text)
    ELSE CAST(t.typlen AS pg_catalog.text)
  END AS "Size",
  pg_catalog.array_to_string(
      ARRAY(
          SELECT e.enumlabel
          FROM pg_catalog.pg_enum e
          WHERE e.enumtypid = t.oid
          ORDER BY e.enumsortorder
      ),
      E'\n'
  ) AS "Elements",
 pg_catalog.array_to_string(te.typoptions, ',') AS "Type Options",
  pg_catalog.pg_get_userbyid(t.typowner) AS "Owner",
pg_catalog.array_to_string(t.typacl, E'\n') AS "Access privileges",
    pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace

   LEFT OUTER JOIN pg_catalog.pg_type_encoding te ON te.typid = t.oid WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND (t.typname OPERATOR(pg_catalog.~) '^(pair)$' COLLATE pg_catalog.default
        OR pg_catalog.format_type(t.oid, NULL) OPERATOR(pg_catalog.~) '^(pair)$' COLLATE pg_catalog.default)
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2;

-- ===== \dx =====
SELECT e.extname AS "Name", e.extversion AS "Version", n.nspname AS "Schema", c.description AS "Description"
FROM pg_catalog.pg_extension e LEFT JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace LEFT JOIN pg_catalog.pg_description c ON c.objoid = e.oid AND c.classoid = 'pg_catalog.pg_extension'::pg_catalog.regclass
ORDER BY 1;

-- ===== \dx+ =====
SELECT e.extname, e.oid
FROM pg_catalog.pg_extension e
ORDER BY 1;
SELECT pg_catalog.pg_describe_object(classid, objid, 0) AS "Object description"
FROM pg_catalog.pg_depend
WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND refobjid = '13226' AND deptype = 'e'
ORDER BY 1;
SELECT pg_catalog.pg_describe_object(classid, objid, 0) AS "Object description"
FROM pg_catalog.pg_depend
WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND refobjid = '16510' AND deptype = 'e'
ORDER BY 1;
SELECT pg_catalog.pg_describe_object(classid, objid, 0) AS "Object description"
FROM pg_catalog.pg_depend
WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND refobjid = '13186' AND deptype = 'e'
ORDER BY 1;

-- ===== \dx hstore =====
SELECT e.extname AS "Name", e.extversion AS "Version", n.nspname AS "Schema", c.description AS "Description"
FROM pg_catalog.pg_extension e LEFT JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace LEFT JOIN pg_catalog.pg_description c ON c.objoid = e.oid AND c.classoid = 'pg_catalog.pg_extension'::pg_catalog.regclass
WHERE e.extname OPERATOR(pg_catalog.~) '^(hstore)$' COLLATE pg_catalog.default
ORDER BY 1;

-- ===== \dx+ hstore =====
SELECT e.extname, e.oid
FROM pg_catalog.pg_extension e
WHERE e.extname OPERATOR(pg_catalog.~) '^(hstore)$' COLLATE pg_catalog.default
ORDER BY 1;
SELECT pg_catalog.pg_describe_object(classid, objid, 0) AS "Object description"
FROM pg_catalog.pg_depend
WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND refobjid = '16510' AND deptype = 'e'
ORDER BY 1;

-- ===== \dX =====
-- (no queries sent)

-- ===== \dX+ =====
-- (no queries sent)

-- ===== \dX app.* =====
-- (no queries sent)

-- ===== \dy =====
SELECT evtname as "Name", evtevent as "Event", pg_catalog.pg_get_userbyid(e.evtowner) as "Owner",
 case evtenabled when 'O' then 'enabled'  when 'R' then 'replica'  when 'A' then 'always'  when 'D' then 'disabled' end as "Enabled",
 e.evtfoid::pg_catalog.regproc as "Function", pg_catalog.array_to_string(array(select x from pg_catalog.unnest(evttags) as t(x)), ', ') as "Tags"
FROM pg_catalog.pg_event_trigger e ORDER BY 1;

-- ===== \dy+ =====
SELECT evtname as "Name", evtevent as "Event", pg_catalog.pg_get_userbyid(e.evtowner) as "Owner",
 case evtenabled when 'O' then 'enabled'  when 'R' then 'replica'  when 'A' then 'always'  when 'D' then 'disabled' end as "Enabled",
 e.evtfoid::pg_catalog.regproc as "Function", pg_catalog.array_to_string(array(select x from pg_catalog.unnest(evttags) as t(x)), ', ') as "Tags",
pg_catalog.obj_description(e.oid, 'pg_event_trigger') as "Description"
FROM pg_catalog.pg_event_trigger e ORDER BY 1;

-- ===== \z =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT polname
    || CASE WHEN NOT polpermissive THEN
       E' (RESTRICTIVE)'
       ELSE '' END
    || CASE WHEN polcmd != '*' THEN
           E' (' || polcmd || E'):'
       ELSE E':'
       END
    || CASE WHEN polqual IS NOT NULL THEN
           E'\n  (u): ' || pg_catalog.pg_get_expr(polqual, polrelid)
       ELSE E''
       END
    || CASE WHEN polwithcheck IS NOT NULL THEN
           E'\n  (c): ' || pg_catalog.pg_get_expr(polwithcheck, polrelid)
       ELSE E''
       END    || CASE WHEN polroles <> '{0}' THEN
           E'\n  to: ' || pg_catalog.array_to_string(
               ARRAY(
                   SELECT rolname
                   FROM pg_catalog.pg_roles
                   WHERE oid = ANY (polroles)
                   ORDER BY 1
               ), E', ')
       ELSE E''
       END
    FROM pg_catalog.pg_policy pol
    WHERE polrelid = c.oid), E'\n')
    AS "Policies"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','p')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \z app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT polname
    || CASE WHEN NOT polpermissive THEN
       E' (RESTRICTIVE)'
       ELSE '' END
    || CASE WHEN polcmd != '*' THEN
           E' (' || polcmd || E'):'
       ELSE E':'
       END
    || CASE WHEN polqual IS NOT NULL THEN
           E'\n  (u): ' || pg_catalog.pg_get_expr(polqual, polrelid)
       ELSE E''
       END
    || CASE WHEN polwithcheck IS NOT NULL THEN
           E'\n  (c): ' || pg_catalog.pg_get_expr(polwithcheck, polrelid)
       ELSE E''
       END    || CASE WHEN polroles <> '{0}' THEN
           E'\n  to: ' || pg_catalog.array_to_string(
               ARRAY(
                   SELECT rolname
                   FROM pg_catalog.pg_roles
                   WHERE oid = ANY (polroles)
                   ORDER BY 1
               ), E', ')
       ELSE E''
       END
    FROM pg_catalog.pg_policy pol
    WHERE polrelid = c.oid), E'\n')
    AS "Policies"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','p')
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$' COLLATE pg_catalog.default
ORDER BY 1, 2;

-- ===== \zS =====
-- (no queries sent)

-- ===== \sf app.add =====
SELECT 'app.add'::pg_catalog.regproc::pg_catalog.oid;
SELECT pg_catalog.pg_get_functiondef(16498);

-- ===== \sf+ app.add =====
SELECT 'app.add'::pg_catalog.regproc::pg_catalog.oid;
SELECT pg_catalog.pg_get_functiondef(16498);

-- ===== \sf app.touch =====
SELECT 'app.touch'::pg_catalog.regproc::pg_catalog.oid;
SELECT pg_catalog.pg_get_functiondef(16496);

-- ===== \sv app.parent_view =====
SELECT 'app.parent_view'::pg_catalog.regclass::pg_catalog.oid;
SELECT nspname, relname, relkind, pg_catalog.pg_get_viewdef(c.oid, true), pg_catalog.array_remove(pg_catalog.array_remove(c.reloptions,'check_option=local'),'check_option=cascaded') AS reloptions, CASE WHEN 'check_option=local' = ANY (c.reloptions) THEN 'LOCAL'::text WHEN 'check_option=cascaded' = ANY (c.reloptions) THEN 'CASCADED'::text ELSE NULL END AS checkoption FROM pg_catalog.pg_class c LEFT JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid WHERE c.oid = 16491;

-- ===== \sv+ app.parent_view =====
SELECT 'app.parent_view'::pg_catalog.regclass::pg_catalog.oid;
SELECT nspname, relname, relkind, pg_catalog.pg_get_viewdef(c.oid, true), pg_catalog.array_remove(pg_catalog.array_remove(c.reloptions,'check_option=local'),'check_option=cascaded') AS reloptions, CASE WHEN 'check_option=local' = ANY (c.reloptions) THEN 'LOCAL'::text WHEN 'check_option=cascaded' = ANY (c.reloptions) THEN 'CASCADED'::text ELSE NULL END AS checkoption FROM pg_catalog.pg_class c LEFT JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid WHERE c.oid = 16491;

-- ===== \sv app.parent_mv =====
SELECT 'app.parent_mv'::pg_catalog.regclass::pg_catalog.oid;
SELECT nspname, relname, relkind, pg_catalog.pg_get_viewdef(c.oid, true), pg_catalog.array_remove(pg_catalog.array_remove(c.reloptions,'check_option=local'),'check_option=cascaded') AS reloptions, CASE WHEN 'check_option=local' = ANY (c.reloptions) THEN 'LOCAL'::text WHEN 'check_option=cascaded' = ANY (c.reloptions) THEN 'CASCADED'::text ELSE NULL END AS checkoption FROM pg_catalog.pg_class c LEFT JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid WHERE c.oid = 16636;

-- ===== \conninfo =====
-- (no queries sent)
