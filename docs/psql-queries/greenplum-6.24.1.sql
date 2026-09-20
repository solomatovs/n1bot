-- psql: psql (PostgreSQL) 9.4.26
-- server: PostgreSQL 9.4.26 (Greenplum Database 6.24.1 build dev) on x86_64-unknown-linux-gnu, compiled by gcc (Ubuntu 11.3.0-1ubuntu1~22.04) 11.3.0, 64-bit compiled on Apr 20 2023 09:53:16

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
WHERE d.datname OPERATOR(pg_catalog.~) '^(postgres)$'
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
FROM pg_catalog.pg_roles r
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
FROM pg_catalog.pg_roles r
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
FROM pg_catalog.pg_roles r
WHERE r.rolname OPERATOR(pg_catalog.~) '^(scrape_login)$'
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
FROM pg_catalog.pg_roles r
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
FROM pg_catalog.pg_roles r
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
WHERE r.rolname OPERATOR(pg_catalog.~) '^(scrape_login)$'
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
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1;

-- ===== \d =====
select pg_catalog.version();
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \d+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','s','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dS+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','s','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \d app.* =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16475';
SELECT a.compresstype, a.compresslevel, a.blocksize, a.checksum
FROM pg_catalog.pg_appendonly a, pg_catalog.pg_class c
WHERE c.oid = a.relid AND c.oid = '16475';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16475' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16475' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16475' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16475');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16465';
SELECT a.compresstype, a.compresslevel, a.blocksize, a.checksum
FROM pg_catalog.pg_appendonly a, pg_catalog.pg_class c
WHERE c.oid = a.relid AND c.oid = '16465';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16465' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16465' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16465' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16465');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16429';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16429' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16429' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16429' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16429' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16429' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D'))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16429' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16429' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16429');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16427';
SELECT * FROM app.child_id_seq;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16427' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname)
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16427'
 AND d.deptype='a';
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16436';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16436' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16436' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16500';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16500' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select attnum from pg_catalog.pg_attribute where attrelid = 1255 and attname = 'provariadic';
SELECT x.urilocation, x.execlocation, x.fmttype, x.fmtopts, x.command, x.logerrors, x.rejectlimit, x.rejectlimittype, x.writable, pg_catalog.pg_encoding_to_char(x.encoding) , x.options FROM pg_catalog.pg_exttable x, pg_catalog.pg_class c WHERE x.reloid = c.oid AND c.oid = '16500';
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16500' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16500' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16500');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16503';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16503' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select attnum from pg_catalog.pg_attribute where attrelid = 1255 and attname = 'provariadic';
SELECT x.urilocation, x.execlocation, x.fmttype, x.fmtopts, x.command, x.logerrors, x.rejectlimit, x.rejectlimittype, x.writable, pg_catalog.pg_encoding_to_char(x.encoding) , x.options FROM pg_catalog.pg_exttable x, pg_catalog.pg_class c WHERE x.reloid = c.oid AND c.oid = '16503';
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16503' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16503' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16503');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16522';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) ||  ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16522' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16522' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16522' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16522' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16522');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16485';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16485' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16485' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16485' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16485');
SELECT parrelid FROM pg_catalog.pg_partition WHERE parrelid = '16485';
WITH att_arr AS (SELECT unnest(paratts) 
	FROM pg_catalog.pg_partition p 
	WHERE p.parrelid = '16485' AND p.parlevel = 0 AND p.paristemplate = false), 
idx_att AS (SELECT row_number() OVER() AS idx, unnest AS att_num FROM att_arr) 
SELECT attname FROM pg_catalog.pg_attribute, idx_att 
	WHERE attrelid='16485' AND attnum = att_num ORDER BY idx;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16492';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16492' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16492' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16492' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16492' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16492');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16496';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16496' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16496' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16496' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16496' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16496');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16488';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16488' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16488' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16488' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16488');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16443';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16443' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16443' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16443' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16443' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16443');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16405';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16405' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16414';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16414' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16414' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16414' AND r.contype = 'c'
ORDER BY 1;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16414' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16414' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16414' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D'))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16414' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16414' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16414');
SELECT parrelid FROM pg_catalog.pg_partition WHERE parrelid = '16414';
WITH att_arr AS (SELECT unnest(paratts) FROM pg_catalog.pg_partition p, 
	(SELECT parrelid, parlevel 
		FROM pg_catalog.pg_partition p, pg_catalog.pg_partition_rule pr 
		WHERE pr.parchildrelid='16414' AND p.oid = pr.paroid) AS v 
	WHERE p.parrelid = v.parrelid AND p.parlevel = v.parlevel+1 AND p.paristemplate = false), 
idx_att AS (SELECT row_number() OVER() AS idx, unnest AS att_num FROM att_arr) 
SELECT attname FROM pg_catalog.pg_attribute, idx_att 
	WHERE attrelid='16414' AND attnum = att_num ORDER BY idx;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16426';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16426' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16426' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16412';
SELECT * FROM app.parent_id_seq;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16412' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname)
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16412'
 AND d.deptype='a';
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16649';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16649' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16649' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16649' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16649' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16649');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16656';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16656' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16656' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16424';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16424' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16424' AND c.relam = a.oid
AND i.indrelid = c2.oid;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16506';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16506' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16453';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16453' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16453' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16453' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16453');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16459';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16459' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16459' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16459' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16459');
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16410';
SELECT * FROM app.seq_manual;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16410' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname)
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16410'
 AND d.deptype='a';

-- ===== \d app.parent =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16414';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16414' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16414' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16414' AND r.contype = 'c'
ORDER BY 1;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16414' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16414' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16414' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D'))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16414' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16414' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16414');
SELECT parrelid FROM pg_catalog.pg_partition WHERE parrelid = '16414';
WITH att_arr AS (SELECT unnest(paratts) FROM pg_catalog.pg_partition p, 
	(SELECT parrelid, parlevel 
		FROM pg_catalog.pg_partition p, pg_catalog.pg_partition_rule pr 
		WHERE pr.parchildrelid='16414' AND p.oid = pr.paroid) AS v 
	WHERE p.parrelid = v.parrelid AND p.parlevel = v.parlevel+1 AND p.paristemplate = false), 
idx_att AS (SELECT row_number() OVER() AS idx, unnest AS att_num FROM att_arr) 
SELECT attname FROM pg_catalog.pg_attribute, idx_att 
	WHERE attrelid='16414' AND attnum = att_num ORDER BY idx;

-- ===== \d+ app.parent =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16414';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16414' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16414' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16414' AND r.contype = 'c'
ORDER BY 1;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16414' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16414' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16414' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D'))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16414' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16414' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16414');
SELECT parrelid FROM pg_catalog.pg_partition WHERE parrelid = '16414';
WITH att_arr AS (SELECT unnest(paratts) FROM pg_catalog.pg_partition p, 
	(SELECT parrelid, parlevel 
		FROM pg_catalog.pg_partition p, pg_catalog.pg_partition_rule pr 
		WHERE pr.parchildrelid='16414' AND p.oid = pr.paroid) AS v 
	WHERE p.parrelid = v.parrelid AND p.parlevel = v.parlevel+1 AND p.paristemplate = false), 
idx_att AS (SELECT row_number() OVER() AS idx, unnest AS att_num FROM att_arr) 
SELECT attname FROM pg_catalog.pg_attribute, idx_att 
	WHERE attrelid='16414' AND attnum = att_num ORDER BY idx;

-- ===== \d app.child =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(child)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16429';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16429' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16429' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16429' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16429' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16429' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D'))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16429' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16429' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16429');

-- ===== \d+ app.child =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(child)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16429';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16429' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16429' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16429' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16429' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled, t.tgisinternal
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16429' AND (NOT t.tgisinternal OR (t.tgisinternal AND t.tgenabled = 'D'))
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16429' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16429' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16429');

-- ===== \d app.inherited =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(inherited)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16443';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16443' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16443' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16443' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16443' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16443');

-- ===== \d+ app.inherited =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(inherited)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16443';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16443' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16443' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16443' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16443' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16443');

-- ===== \d app.random_dist =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(random_dist)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16453';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16453' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16453' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16453' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16453');

-- ===== \d+ app.random_dist =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(random_dist)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16453';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16453' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16453' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16453' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16453');

-- ===== \d app.replicated =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(replicated)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16459';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16459' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16459' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16459' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16459');

-- ===== \d+ app.replicated =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(replicated)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16459';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16459' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16459' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16459' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16459');

-- ===== \d app.ao_row =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_row)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16465';
SELECT a.compresstype, a.compresslevel, a.blocksize, a.checksum
FROM pg_catalog.pg_appendonly a, pg_catalog.pg_class c
WHERE c.oid = a.relid AND c.oid = '16465';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16465' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16465' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16465' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16465');

-- ===== \d+ app.ao_row =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_row)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16465';
SELECT a.compresstype, a.compresslevel, a.blocksize, a.checksum
FROM pg_catalog.pg_appendonly a, pg_catalog.pg_class c
WHERE c.oid = a.relid AND c.oid = '16465';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16465' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16465' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16465' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16465');

-- ===== \d app.ao_col =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_col)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16475';
SELECT a.compresstype, a.compresslevel, a.blocksize, a.checksum
FROM pg_catalog.pg_appendonly a, pg_catalog.pg_class c
WHERE c.oid = a.relid AND c.oid = '16475';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16475' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16475' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16475' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16475');

-- ===== \d+ app.ao_col =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ao_col)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16475';
SELECT a.compresstype, a.compresslevel, a.blocksize, a.checksum
FROM pg_catalog.pg_appendonly a, pg_catalog.pg_class c
WHERE c.oid = a.relid AND c.oid = '16475';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
 pg_catalog.array_to_string(e.attoptions, ','),
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16475' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16475' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16475' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16475');

-- ===== \d app.gp_part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16485';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16485' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16485' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16485' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16485');
SELECT parrelid FROM pg_catalog.pg_partition WHERE parrelid = '16485';
WITH att_arr AS (SELECT unnest(paratts) 
	FROM pg_catalog.pg_partition p 
	WHERE p.parrelid = '16485' AND p.parlevel = 0 AND p.paristemplate = false), 
idx_att AS (SELECT row_number() OVER() AS idx, unnest AS att_num FROM att_arr) 
SELECT attname FROM pg_catalog.pg_attribute, idx_att 
	WHERE attrelid='16485' AND attnum = att_num ORDER BY idx;

-- ===== \d+ app.gp_part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16485';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16485' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16485' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16485' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16485');
SELECT parrelid FROM pg_catalog.pg_partition WHERE parrelid = '16485';
WITH att_arr AS (SELECT unnest(paratts) 
	FROM pg_catalog.pg_partition p 
	WHERE p.parrelid = '16485' AND p.parlevel = 0 AND p.paristemplate = false), 
idx_att AS (SELECT row_number() OVER() AS idx, unnest AS att_num FROM att_arr) 
SELECT attname FROM pg_catalog.pg_attribute, idx_att 
	WHERE attrelid='16485' AND attnum = att_num ORDER BY idx;

-- ===== \d app.gp_part_1_prt_other =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part_1_prt_other)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16488';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16488' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16488' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16488' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16488');

-- ===== \d+ app.gp_part_1_prt_other =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_part_1_prt_other)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16488';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16488' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16488' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16488' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16488');

-- ===== \d app.ext =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16500';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16500' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select attnum from pg_catalog.pg_attribute where attrelid = 1255 and attname = 'provariadic';
SELECT x.urilocation, x.execlocation, x.fmttype, x.fmtopts, x.command, x.logerrors, x.rejectlimit, x.rejectlimittype, x.writable, pg_catalog.pg_encoding_to_char(x.encoding) , x.options FROM pg_catalog.pg_exttable x, pg_catalog.pg_class c WHERE x.reloid = c.oid AND c.oid = '16500';
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16500' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16500' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16500');

-- ===== \d+ app.ext =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16500';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16500' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select attnum from pg_catalog.pg_attribute where attrelid = 1255 and attname = 'provariadic';
SELECT x.urilocation, x.execlocation, x.fmttype, x.fmtopts, x.command, x.logerrors, x.rejectlimit, x.rejectlimittype, x.writable, pg_catalog.pg_encoding_to_char(x.encoding) , x.options FROM pg_catalog.pg_exttable x, pg_catalog.pg_class c WHERE x.reloid = c.oid AND c.oid = '16500';
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16500' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16500' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16500');

-- ===== \d app.ext_w =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext_w)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16503';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16503' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select attnum from pg_catalog.pg_attribute where attrelid = 1255 and attname = 'provariadic';
SELECT x.urilocation, x.execlocation, x.fmttype, x.fmtopts, x.command, x.logerrors, x.rejectlimit, x.rejectlimittype, x.writable, pg_catalog.pg_encoding_to_char(x.encoding) , x.options FROM pg_catalog.pg_exttable x, pg_catalog.pg_class c WHERE x.reloid = c.oid AND c.oid = '16503';
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16503' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16503' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16503');

-- ===== \d+ app.ext_w =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ext_w)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16503';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16503' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
select attnum from pg_catalog.pg_attribute where attrelid = 1255 and attname = 'provariadic';
SELECT x.urilocation, x.execlocation, x.fmttype, x.fmtopts, x.command, x.logerrors, x.rejectlimit, x.rejectlimittype, x.writable, pg_catalog.pg_encoding_to_char(x.encoding) , x.options FROM pg_catalog.pg_exttable x, pg_catalog.pg_class c WHERE x.reloid = c.oid AND c.oid = '16503';
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16503' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16503' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16503');

-- ===== \d app.parent_view =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_view)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16506';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16506' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d+ app.parent_view =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_view)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16506';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16506' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.pg_get_viewdef('16506'::pg_catalog.oid, true);
SELECT r.rulename, trim(trailing ';' from pg_catalog.pg_get_ruledef(r.oid, true))
FROM pg_catalog.pg_rewrite r
WHERE r.ev_class = '16506' AND r.rulename != '_RETURN' ORDER BY 1;

-- ===== \d app.parent_mv =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_mv)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16649';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16649' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16649' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16649' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16649' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16649');

-- ===== \d+ app.parent_mv =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_mv)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16649';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16649' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.pg_get_viewdef('16649'::pg_catalog.oid, true);
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16649' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.rulename, trim(trailing ';' from pg_catalog.pg_get_ruledef(r.oid, true))
FROM pg_catalog.pg_rewrite r
WHERE r.ev_class = '16649' AND r.rulename != '_RETURN' ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16649' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16649' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16649');

-- ===== \d app.seq_manual =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(seq_manual)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16410';
SELECT * FROM app.seq_manual;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16410' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname)
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16410'
 AND d.deptype='a';

-- ===== \d+ app.seq_manual =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(seq_manual)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16410';
SELECT * FROM app.seq_manual;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16410' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname)
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16410'
 AND d.deptype='a';

-- ===== \d app.parent_id_seq =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_id_seq)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16412';
SELECT * FROM app.parent_id_seq;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16412' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.quote_ident(nspname) || '.' ||
   pg_catalog.quote_ident(relname) || '.' ||
   pg_catalog.quote_ident(attname)
FROM pg_catalog.pg_class c
INNER JOIN pg_catalog.pg_depend d ON c.oid=d.refobjid
INNER JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
INNER JOIN pg_catalog.pg_attribute a ON (
 a.attrelid=c.oid AND
 a.attnum=d.refobjsubid)
WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.refclassid='pg_catalog.pg_class'::pg_catalog.regclass
 AND d.objid='16412'
 AND d.deptype='a';

-- ===== \d app.parent_pkey =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_pkey)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16424';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16424' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16424' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d+ app.parent_pkey =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_pkey)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16424';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16424' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16424' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d app.parent_created_idx =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(parent_created_idx)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16426';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16426' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
i.indisreplident,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16426' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d app.part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.part_2024 =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part_2024)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.part_2024 =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(part_2024)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.ident =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ident)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.ident =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ident)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.ft =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ft)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16522';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) ||  ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16522' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16522' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16522' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16522' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16522');

-- ===== \d+ app.ft =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(ft)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16522';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  CASE WHEN attfdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT pg_catalog.quote_ident(option_name) ||  ' ' || pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(attfdwoptions)), ', ') || ')' END AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16522' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT s.srvname,
  pg_catalog.array_to_string(ARRAY(
    SELECT pg_catalog.quote_ident(option_name) || ' ' || pg_catalog.quote_literal(option_value)
    FROM pg_catalog.pg_options_to_table(ftoptions)),  ', ')
FROM pg_catalog.pg_foreign_table f,
     pg_catalog.pg_foreign_server s
WHERE f.ftrelid = '16522' AND s.oid = f.ftserver;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16522' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16522' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('16522');

-- ===== \d app.pair =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pair)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16405';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16405' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d+ app.pair =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pair)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16405';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '16405' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pg_class)$'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('1259');

-- ===== \d+ pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pg_class)$'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions,
  a.attstorage ,
  CASE WHEN a.attstattarget=-1 THEN NULL ELSE a.attstattarget END AS attstattarget,
  pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('1259');

-- ===== \d pg_catalog.pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(pg_class)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(pg_catalog)$'
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('1259');

-- ===== \d gp_segment_configuration =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(gp_segment_configuration)$'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
select oid from pg_catalog.pg_class where relnamespace = 11 and relname  = 'pg_attribute_encoding';
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END, c.relpersistence, c.relreplident
, c.relstorage as relstorage FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '5036';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  NULL AS indexdef,
  NULL AS attfdwoptions
FROM pg_catalog.pg_attribute a 
LEFT OUTER JOIN pg_catalog.pg_attribute_encoding e
ON   e.attrelid = a .attrelid AND e.attnum = a.attnum
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
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '5036' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '5036' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT pg_catalog.pg_get_table_distributedby('5036');
SELECT spcname FROM pg_catalog.pg_tablespace
WHERE oid = '1664';

-- ===== \dt =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dt+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','s','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dt app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','s','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1,2;

-- ===== \dt+ app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','s','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1,2;

-- ===== \di =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \di+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \diS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','s','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \di app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','s','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1,2;

-- ===== \ds =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('S','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \ds+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('S','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dsS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('S','s','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dv =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','')
AND c.relstorage IN ('v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dv+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','')
AND c.relstorage IN ('v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dvS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','s','')
AND c.relstorage IN ('v','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dm =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('m','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dm+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('m','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dE =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','f','')
AND c.relstorage IN ('x','f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dE+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','f','')
AND c.relstorage IN ('x','f','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtvsimE =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','v','m','i','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtvsimE+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
 c2.relname as "Table",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','v','m','i','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
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
WHERE p.proisagg
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
WHERE p.proisagg
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
WHERE p.proisagg
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
WHERE p.proisagg
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1, 2, 4;

-- ===== \dA =====
-- (no queries sent)

-- ===== \dA+ =====
-- (no queries sent)

-- ===== \dAc =====
-- (no queries sent)

-- ===== \dAc+ =====
-- (no queries sent)

-- ===== \dAf =====
-- (no queries sent)

-- ===== \dAf+ =====
-- (no queries sent)

-- ===== \dAo =====
-- (no queries sent)

-- ===== \dAo+ =====
-- (no queries sent)

-- ===== \dAp =====
-- (no queries sent)

-- ===== \dAp+ =====
-- (no queries sent)

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
  AND c.conname OPERATOR(pg_catalog.~) '^(work_mem)$'
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dC =====
SELECT pg_catalog.format_type(castsource, NULL) AS "Source type",
       pg_catalog.format_type(casttarget, NULL) AS "Target type",
       CASE WHEN castfunc = 0 THEN '(binary coercible)'
            ELSE p.proname
       END as "Function",
       CASE WHEN c.castcontext = 'e' THEN 'no'
            WHEN c.castcontext = 'a' THEN 'in assignment'
            ELSE 'yes'
       END as "Implicit?"FROM pg_catalog.pg_cast c LEFT JOIN pg_catalog.pg_proc p
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
       CASE WHEN castfunc = 0 THEN '(binary coercible)'
            ELSE p.proname
       END as "Function",
       CASE WHEN c.castcontext = 'e' THEN 'no'
            WHEN c.castcontext = 'a' THEN 'in assignment'
            ELSE 'yes'
       END as "Implicit?",
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
       CASE WHEN castfunc = 0 THEN '(binary coercible)'
            ELSE p.proname
       END as "Function",
       CASE WHEN c.castcontext = 'e' THEN 'no'
            WHEN c.castcontext = 'a' THEN 'in assignment'
            ELSE 'yes'
       END as "Implicit?"FROM pg_catalog.pg_cast c LEFT JOIN pg_catalog.pg_proc p
     ON c.castfunc = p.oid
     LEFT JOIN pg_catalog.pg_type ts
     ON c.castsource = ts.oid
     LEFT JOIN pg_catalog.pg_namespace ns
     ON ns.oid = ts.typnamespace
     LEFT JOIN pg_catalog.pg_type tt
     ON c.casttarget = tt.oid
     LEFT JOIN pg_catalog.pg_namespace nt
     ON nt.oid = tt.typnamespace
WHERE ( (true  AND (ts.typname OPERATOR(pg_catalog.~) '^(int4)$'
        OR pg_catalog.format_type(ts.oid, NULL) OPERATOR(pg_catalog.~) '^(int4)$')
  AND pg_catalog.pg_type_is_visible(ts.oid)
) OR (true  AND (tt.typname OPERATOR(pg_catalog.~) '^(int4)$'
        OR pg_catalog.format_type(tt.oid, NULL) OPERATOR(pg_catalog.~) '^(int4)$')
  AND pg_catalog.pg_type_is_visible(tt.oid)
) )
ORDER BY 1, 2;

-- ===== \dd =====
SELECT DISTINCT tt.nspname AS "Schema", tt.name AS "Name", tt.object AS "Object", d.description AS "Description"
FROM (
  SELECT pgc.oid as oid, pgc.tableoid AS tableoid,
  n.nspname as nspname,
  CAST(pgc.conname AS pg_catalog.text) as name,  CAST('constraint' AS pg_catalog.text) as object
  FROM pg_catalog.pg_constraint pgc
    JOIN pg_catalog.pg_class c ON c.oid = pgc.conrelid
    LEFT JOIN pg_catalog.pg_namespace n     ON n.oid = c.relnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_table_is_visible(c.oid)
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
  CAST(pgc.conname AS pg_catalog.text) as name,  CAST('constraint' AS pg_catalog.text) as object
  FROM pg_catalog.pg_constraint pgc
    JOIN pg_catalog.pg_class c ON c.oid = pgc.conrelid
    LEFT JOIN pg_catalog.pg_namespace n     ON n.oid = c.relnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$'
UNION ALL
  SELECT o.oid as oid, o.tableoid as tableoid,
  n.nspname as nspname,
  CAST(o.opcname AS pg_catalog.text) as name,
  CAST('operator class' AS pg_catalog.text) as object
  FROM pg_catalog.pg_opclass o
    JOIN pg_catalog.pg_am am ON o.opcmethod = am.oid
    JOIN pg_catalog.pg_namespace n ON n.oid = o.opcnamespace
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
UNION ALL
  SELECT opf.oid as oid, opf.tableoid as tableoid,
  n.nspname as nspname,
  CAST(opf.opfname AS pg_catalog.text) AS name,
  CAST('operator family' AS pg_catalog.text) as object
  FROM pg_catalog.pg_opfamily opf
    JOIN pg_catalog.pg_am am ON opf.opfmethod = am.oid
    JOIN pg_catalog.pg_namespace n ON opf.opfnamespace = n.oid
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
UNION ALL
  SELECT r.oid as oid, r.tableoid as tableoid,
  n.nspname as nspname,
  CAST(r.rulename AS pg_catalog.text) as name,  CAST('rule' AS pg_catalog.text) as object
  FROM pg_catalog.pg_rewrite r
       JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE r.rulename != '_RETURN'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
UNION ALL
  SELECT t.oid as oid, t.tableoid as tableoid,
  n.nspname as nspname,
  CAST(t.tgname AS pg_catalog.text) as name,  CAST('trigger' AS pg_catalog.text) as object
  FROM pg_catalog.pg_trigger t
       JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$'
) AS tt
  JOIN pg_catalog.pg_description d ON (tt.oid = d.objoid AND tt.tableoid = d.classoid AND d.objsubid = 0)
ORDER BY 1, 2, 3;

-- ===== \dD =====
SELECT n.nspname as "Schema",
       t.typname as "Name",
       pg_catalog.format_type(t.typbasetype, t.typtypmod) as "Type",
       TRIM(LEADING
            COALESCE((SELECT ' collate ' || c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
                      WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation), '') ||
            CASE WHEN t.typnotnull THEN ' not null' ELSE '' END ||
            CASE WHEN t.typdefault IS NOT NULL THEN ' default ' || t.typdefault ELSE '' END
       ) as "Modifier",
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
       TRIM(LEADING
            COALESCE((SELECT ' collate ' || c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
                      WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation), '') ||
            CASE WHEN t.typnotnull THEN ' not null' ELSE '' END ||
            CASE WHEN t.typdefault IS NOT NULL THEN ' default ' || t.typdefault ELSE '' END
       ) as "Modifier",
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
       TRIM(LEADING
            COALESCE((SELECT ' collate ' || c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
                      WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation), '') ||
            CASE WHEN t.typnotnull THEN ' not null' ELSE '' END ||
            CASE WHEN t.typdefault IS NOT NULL THEN ' default ' || t.typdefault ELSE '' END
       ) as "Modifier",
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
       TRIM(LEADING
            COALESCE((SELECT ' collate ' || c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type bt
                      WHERE c.oid = t.typcollation AND bt.oid = t.typbasetype AND t.typcollation <> bt.typcollation), '') ||
            CASE WHEN t.typnotnull THEN ' not null' ELSE '' END ||
            CASE WHEN t.typdefault IS NOT NULL THEN ' default ' || t.typdefault ELSE '' END
       ) as "Modifier",
       pg_catalog.array_to_string(ARRAY(
         SELECT pg_catalog.pg_get_constraintdef(r.oid, true) FROM pg_catalog.pg_constraint r WHERE t.oid = r.contypid
       ), ' ') as "Check"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'd'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1, 2;

-- ===== \ddp =====
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS "Owner",
  n.nspname AS "Schema",
  CASE d.defaclobjtype WHEN 'r' THEN 'table' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'function' WHEN 'T' THEN 'type' END AS "Type",
  pg_catalog.array_to_string(d.defaclacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_default_acl d
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
ORDER BY 1, 2, 3;

-- ===== \ddp app =====
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS "Owner",
  n.nspname AS "Schema",
  CASE d.defaclobjtype WHEN 'r' THEN 'table' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'function' WHEN 'T' THEN 'type' END AS "Type",
  pg_catalog.array_to_string(d.defaclacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_default_acl d
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
WHERE (n.nspname OPERATOR(pg_catalog.~) '^(app)$'
        OR pg_catalog.pg_get_userbyid(d.defaclrole) OPERATOR(pg_catalog.~) '^(app)$')
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
  CASE WHEN srvoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(srvoptions)),  ', ') || ')'   END AS "FDW Options",
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
 CASE WHEN ftoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(ftoptions)),  ', ') || ')'   END AS "FDW Options",
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
 CASE WHEN umoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(umoptions)),  ', ') || ')'   END AS "FDW Options"
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
 CASE WHEN fdwoptions IS NULL THEN '' ELSE   '(' || pg_catalog.array_to_string(ARRAY(SELECT   pg_catalog.quote_ident(option_name) ||  ' ' ||   pg_catalog.quote_literal(option_value)  FROM   pg_catalog.pg_options_to_table(fdwoptions)),  ', ') || ')'   END AS "FDW Options",
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'm' THEN 'master'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
  pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1, 2, 4;

-- ===== \df+ app.add =====
select attnum from pg_catalog.pg_attribute where attrelid = 'pg_catalog.pg_proc'::regclass and attname = 'prodataaccess';
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'm' THEN 'master'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
  pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
  l.lanname as "Language",
  p.prosrc as "Source code",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE p.proname OPERATOR(pg_catalog.~) '^(add)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1, 2, 4;

-- ===== \dfa =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE (
       p.proisagg
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'm' THEN 'master'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
  pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
  l.lanname as "Language",
  p.prosrc as "Source code",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE (
       p.proisagg
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE NOT p.proisagg
      AND p.prorettype <> 'pg_catalog.trigger'::pg_catalog.regtype
      AND NOT p.proiswindow
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'm' THEN 'master'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
  pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
  l.lanname as "Language",
  p.prosrc as "Source code",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE NOT p.proisagg
      AND p.prorettype <> 'pg_catalog.trigger'::pg_catalog.regtype
      AND NOT p.proiswindow
  AND pg_catalog.pg_function_is_visible(p.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 4;

-- ===== \dfp =====
-- (no queries sent)

-- ===== \dfp+ =====
-- (no queries sent)

-- ===== \dft =====
SELECT n.nspname as "Schema",
  p.proname as "Name",
  pg_catalog.pg_get_function_result(p.oid) as "Result data type",
  pg_catalog.pg_get_function_arguments(p.oid) as "Argument data types",
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'm' THEN 'master'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
  pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE (
       p.proiswindow
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
 CASE
  WHEN p.proisagg THEN 'agg'
  WHEN p.proiswindow THEN 'window'
  WHEN p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype THEN 'trigger'
  ELSE 'normal'
 END as "Type",
 CASE
  WHEN p.prodataaccess = 'n' THEN 'no sql'
  WHEN p.prodataaccess = 'c' THEN 'contains sql'
  WHEN p.prodataaccess = 'r' THEN 'reads sql data'
  WHEN p.prodataaccess = 'm' THEN 'modifies sql data'
END as "Data access",
 CASE
  WHEN p.proexeclocation = 'a' THEN 'any'
  WHEN p.proexeclocation = 'm' THEN 'master'
  WHEN p.proexeclocation = 's' THEN 'all segments'
  WHEN p.proexeclocation = 'i' THEN 'initplan'
END as "Execute on",
 CASE WHEN prosecdef THEN 'definer' ELSE 'invoker' END AS "Security",
 CASE
  WHEN p.provolatile = 'i' THEN 'immutable'
  WHEN p.provolatile = 's' THEN 'stable'
  WHEN p.provolatile = 'v' THEN 'volatile'
 END as "Volatility",
  pg_catalog.pg_get_userbyid(p.proowner) as "Owner",
  l.lanname as "Language",
  p.prosrc as "Source code",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     LEFT JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE (
       p.proiswindow
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12061' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12063' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12065' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12067' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12069' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12071' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12073' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12075' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12077' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12079' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12081' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12083' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12085' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12087' AND m.mapcfg = c.oid 
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
    ) :: pg_catalog.text , 
  '{}') AS "Dictionaries" 
FROM pg_catalog.pg_ts_config AS c, pg_catalog.pg_ts_config_map AS m 
WHERE c.oid = '12089' AND m.mapcfg = c.oid 
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
       NOT l.lanispl AS "Internal Language",
       l.lanplcallfoid::pg_catalog.regprocedure AS "Call Handler",
       l.lanvalidator::pg_catalog.regprocedure AS "Validator",
       l.laninline::pg_catalog.regprocedure AS "Inline Handler",
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
WHERE n.nspname OPERATOR(pg_catalog.~) '^(app)$'
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
WHERE o.oprname OPERATOR(pg_catalog.~) E'^(<\\+>)$'
  AND pg_catalog.pg_operator_is_visible(o.oid)
ORDER BY 1, 2, 3, 4;

-- ===== \dO =====
SELECT n.nspname AS "Schema",
       c.collname AS "Name",
       c.collcollate AS "Collate",
       c.collctype AS "Ctype"
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
       c.collctype AS "Ctype"
FROM pg_catalog.pg_collation c, pg_catalog.pg_namespace n
WHERE n.oid = c.collnamespace
      AND c.collencoding IN (-1, pg_catalog.pg_char_to_encoding(pg_catalog.getdatabaseencoding()))
  AND pg_catalog.pg_collation_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dp =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dpS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dp app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1, 2;

-- ===== \dP =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dP+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dPn =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dPn+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dPi =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','i','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dPi+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
 c2.relname as "Table",
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','i','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dPt =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dPt+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dPtn =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','')
AND c.relstorage IN ('h', 'a', 'c','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dP app.part =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"

FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','s','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND c.relname OPERATOR(pg_catalog.~) '^(part)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1,2;

-- ===== \dP+ app.part =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner", CASE c.relstorage WHEN 'h' THEN 'heap' WHEN 'x' THEN 'external' WHEN 'a' THEN 'append only' WHEN 'v' THEN 'none' WHEN 'c' THEN 'append only columnar' WHEN 'p' THEN 'Apache Parquet' WHEN 'f' THEN 'foreign' END as "Storage"
,
  pg_catalog.pg_size_pretty(pg_catalog.pg_table_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','S','s','f','')
AND c.relstorage IN ('h', 'a', 'c','x','f','v','')
      AND n.nspname !~ '^pg_toast'
      AND c.oid NOT IN (select inhrelid from pg_catalog.pg_inherits)
  AND c.relname OPERATOR(pg_catalog.~) '^(part)$'
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1,2;

-- ===== \dRp =====
-- (no queries sent)

-- ===== \dRp+ =====
-- (no queries sent)

-- ===== \dRp scrape_pub =====
-- (no queries sent)

-- ===== \dRp+ scrape_pub =====
-- (no queries sent)

-- ===== \dRs =====
-- (no queries sent)

-- ===== \dRs+ =====
-- (no queries sent)

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
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
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
pg_catalog.array_to_string(t.typacl, E'\n') AS "Access privileges",
    pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace

   LEFT OUTER JOIN pg_catalog.pg_type_encoding te ON te.typid = t.oid WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND (t.typname OPERATOR(pg_catalog.~) '^(mood)$'
        OR pg_catalog.format_type(t.oid, NULL) OPERATOR(pg_catalog.~) '^(mood)$')
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
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
pg_catalog.array_to_string(t.typacl, E'\n') AS "Access privileges",
    pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace

   LEFT OUTER JOIN pg_catalog.pg_type_encoding te ON te.typid = t.oid WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND (t.typname OPERATOR(pg_catalog.~) '^(pair)$'
        OR pg_catalog.format_type(t.oid, NULL) OPERATOR(pg_catalog.~) '^(pair)$')
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1, 2;

-- ===== \dx =====
SELECT e.extname AS "Name", e.extversion AS "Version", n.nspname AS "Schema", c.description AS "Description"
FROM pg_catalog.pg_extension e LEFT JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace LEFT JOIN pg_catalog.pg_description c ON c.objoid = e.oid AND c.classoid = 'pg_catalog.pg_extension'::pg_catalog.regclass
ORDER BY 1;

-- ===== \dx+ =====
SELECT e.extname, e.oid
FROM pg_catalog.pg_extension e
ORDER BY 1;
SELECT pg_catalog.pg_describe_object(classid, objid, 0) AS "Object Description"
FROM pg_catalog.pg_depend
WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND refobjid = '16525' AND deptype = 'e'
ORDER BY 1;
SELECT pg_catalog.pg_describe_object(classid, objid, 0) AS "Object Description"
FROM pg_catalog.pg_depend
WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND refobjid = '12331' AND deptype = 'e'
ORDER BY 1;

-- ===== \dx hstore =====
SELECT e.extname AS "Name", e.extversion AS "Version", n.nspname AS "Schema", c.description AS "Description"
FROM pg_catalog.pg_extension e LEFT JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace LEFT JOIN pg_catalog.pg_description c ON c.objoid = e.oid AND c.classoid = 'pg_catalog.pg_extension'::pg_catalog.regclass
WHERE e.extname OPERATOR(pg_catalog.~) '^(hstore)$'
ORDER BY 1;

-- ===== \dx+ hstore =====
SELECT e.extname, e.oid
FROM pg_catalog.pg_extension e
WHERE e.extname OPERATOR(pg_catalog.~) '^(hstore)$'
ORDER BY 1;
SELECT pg_catalog.pg_describe_object(classid, objid, 0) AS "Object Description"
FROM pg_catalog.pg_depend
WHERE refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass AND refobjid = '16525' AND deptype = 'e'
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
 e.evtfoid::pg_catalog.regproc as "Procedure", pg_catalog.array_to_string(array(select x from pg_catalog.unnest(evttags) as t(x)), ', ') as "Tags"
FROM pg_catalog.pg_event_trigger e ORDER BY 1;

-- ===== \dy+ =====
SELECT evtname as "Name", evtevent as "Event", pg_catalog.pg_get_userbyid(e.evtowner) as "Owner",
 case evtenabled when 'O' then 'enabled'  when 'R' then 'replica'  when 'A' then 'always'  when 'D' then 'disabled' end as "Enabled",
 e.evtfoid::pg_catalog.regproc as "Procedure", pg_catalog.array_to_string(array(select x from pg_catalog.unnest(evttags) as t(x)), ', ') as "Tags",
pg_catalog.obj_description(e.oid, 'pg_event_trigger') as "Description"
FROM pg_catalog.pg_event_trigger e ORDER BY 1;

-- ===== \z =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \z app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
  AND n.nspname OPERATOR(pg_catalog.~) '^(app)$'
ORDER BY 1, 2;

-- ===== \zS =====
-- (no queries sent)

-- ===== \sf app.add =====
SELECT 'app.add'::pg_catalog.regproc::pg_catalog.oid;
SELECT pg_catalog.pg_get_functiondef(16513);

-- ===== \sf+ app.add =====
SELECT 'app.add'::pg_catalog.regproc::pg_catalog.oid;
SELECT pg_catalog.pg_get_functiondef(16513);

-- ===== \sf app.touch =====
SELECT 'app.touch'::pg_catalog.regproc::pg_catalog.oid;
SELECT pg_catalog.pg_get_functiondef(16511);

-- ===== \sv app.parent_view =====
-- (no queries sent)

-- ===== \sv+ app.parent_view =====
-- (no queries sent)

-- ===== \sv app.parent_mv =====
-- (no queries sent)

-- ===== \conninfo =====
-- (no queries sent)
