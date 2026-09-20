-- psql: psql (PostgreSQL) 9.0.23
contains support for command-line editing
-- server: PostgreSQL 9.0.23 on x86_64-unknown-linux-gnu, compiled by GCC gcc (GCC) 8.5.0, 64-bit

-- ===== \l =====
SELECT d.datname as "Name",
       pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
       pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
       d.datcollate as "Collation",
       d.datctype as "Ctype",
       pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_database d
ORDER BY 1;

-- ===== \l+ =====
SELECT d.datname as "Name",
       pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
       pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
       d.datcollate as "Collation",
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
       d.datcollate as "Collation",
       d.datctype as "Ctype",
       pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_database d
ORDER BY 1;

-- ===== \du =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
FROM pg_catalog.pg_roles r
ORDER BY 1;

-- ===== \du+ =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
, pg_catalog.shobj_description(r.oid, 'pg_authid') AS description
FROM pg_catalog.pg_roles r
ORDER BY 1;

-- ===== \du scrape_login =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
FROM pg_catalog.pg_roles r
WHERE r.rolname ~ '^(scrape_login)$'
ORDER BY 1;

-- ===== \dg =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
FROM pg_catalog.pg_roles r
ORDER BY 1;

-- ===== \dg+ =====
SELECT r.rolname, r.rolsuper, r.rolinherit,
  r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
  r.rolconnlimit,
  ARRAY(SELECT b.rolname
        FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles b ON (m.roleid = b.oid)
        WHERE m.member = r.oid) as memberof
, pg_catalog.shobj_description(r.oid, 'pg_authid') AS description
FROM pg_catalog.pg_roles r
ORDER BY 1;

-- ===== \drg =====
-- (no queries sent)

-- ===== \drds =====
SELECT rolname AS role, datname AS database,
pg_catalog.array_to_string(setconfig, E'\n') AS settings
FROM pg_db_role_setting AS s
LEFT JOIN pg_database ON pg_database.oid = setdatabase
LEFT JOIN pg_roles ON pg_roles.oid = setrole
ORDER BY role, database;

-- ===== \drds scrape_login =====
SELECT rolname AS role, datname AS database,
pg_catalog.array_to_string(setconfig, E'\n') AS settings
FROM pg_db_role_setting AS s
LEFT JOIN pg_database ON pg_database.oid = setdatabase
LEFT JOIN pg_roles ON pg_roles.oid = setrole
WHERE pg_roles.rolname ~ '^(scrape_login)$'
ORDER BY role, database;

-- ===== \dn =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
FROM pg_catalog.pg_namespace n
WHERE	(n.nspname !~ '^pg_temp_' OR
		 n.nspname = (pg_catalog.current_schemas(true))[1])
ORDER BY 1;

-- ===== \dn+ =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner",
  pg_catalog.array_to_string(n.nspacl, E'\n') AS "Access privileges",
  pg_catalog.obj_description(n.oid, 'pg_namespace') AS "Description"
FROM pg_catalog.pg_namespace n
WHERE	(n.nspname !~ '^pg_temp_' OR
		 n.nspname = (pg_catalog.current_schemas(true))[1])
ORDER BY 1;

-- ===== \dnS =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
FROM pg_catalog.pg_namespace n
WHERE	(n.nspname !~ '^pg_temp_' OR
		 n.nspname = (pg_catalog.current_schemas(true))[1])
ORDER BY 1;

-- ===== \dn app =====
SELECT n.nspname AS "Name",
  pg_catalog.pg_get_userbyid(n.nspowner) AS "Owner"
FROM pg_catalog.pg_namespace n
WHERE	(n.nspname !~ '^pg_temp_' OR
		 n.nspname = (pg_catalog.current_schemas(true))[1])
  AND n.nspname ~ '^(app)$'
ORDER BY 1;

-- ===== \d =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','S','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \d+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','S','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','S','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dS+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','S','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \d app.* =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16419';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16419' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16419' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16419' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16419' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16419' AND NOT t.tgisinternal
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16419' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16419' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16417';
SELECT * FROM app.child_id_seq;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16417' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16426';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16426' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16426' AND c.relam = a.oid
AND i.indrelid = c2.oid;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16433';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16433' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16433' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16433' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16433' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16393';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16393' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16402';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16402' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16402' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16402' AND r.contype = 'c'
ORDER BY 1;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16402' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16402' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16402' AND NOT t.tgisinternal
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16402' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16402' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16416';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16416' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16416' AND c.relam = a.oid
AND i.indrelid = c2.oid;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16400';
SELECT * FROM app.parent_id_seq;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16400' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16414';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16414' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16414' AND c.relam = a.oid
AND i.indrelid = c2.oid;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16412';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16412' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16412' AND c.relam = a.oid
AND i.indrelid = c2.oid;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16443';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16443' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16398';
SELECT * FROM app.seq_manual;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16398' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d app.parent =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16402';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16402' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16402' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16402' AND r.contype = 'c'
ORDER BY 1;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16402' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16402' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16402' AND NOT t.tgisinternal
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16402' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16402' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d+ app.parent =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16402';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16402' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16402' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16402' AND r.contype = 'c'
ORDER BY 1;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16402' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16402' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16402' AND NOT t.tgisinternal
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16402' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16402' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d app.child =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(child)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16419';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16419' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16419' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16419' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16419' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16419' AND NOT t.tgisinternal
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16419' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16419' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d+ app.child =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(child)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16419';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16419' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '16419' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT conname,
  pg_catalog.pg_get_constraintdef(r.oid, true) as condef
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16419' AND r.contype = 'f' ORDER BY 1;
SELECT conname, conrelid::pg_catalog.regclass,
  pg_catalog.pg_get_constraintdef(c.oid, true) as condef
FROM pg_catalog.pg_constraint c
WHERE c.confrelid = '16419' AND c.contype = 'f' ORDER BY 1;
SELECT t.tgname, pg_catalog.pg_get_triggerdef(t.oid, true), t.tgenabled
FROM pg_catalog.pg_trigger t
WHERE t.tgrelid = '16419' AND NOT t.tgisinternal
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16419' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16419' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d app.inherited =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(inherited)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16433';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16433' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16433' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16433' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16433' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d+ app.inherited =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(inherited)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16433';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16433' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT r.conname, pg_catalog.pg_get_constraintdef(r.oid, true)
FROM pg_catalog.pg_constraint r
WHERE r.conrelid = '16433' AND r.contype = 'c'
ORDER BY 1;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '16433' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '16433' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d app.parent_view =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_view)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16443';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16443' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d+ app.parent_view =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_view)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16443';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16443' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT pg_catalog.pg_get_viewdef('16443'::pg_catalog.oid, true);
SELECT r.rulename, trim(trailing ';' from pg_catalog.pg_get_ruledef(r.oid, true))
FROM pg_catalog.pg_rewrite r
WHERE r.ev_class = '16443' AND r.rulename != '_RETURN' ORDER BY 1;

-- ===== \d app.parent_mv =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_mv)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.parent_mv =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_mv)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.seq_manual =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(seq_manual)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16398';
SELECT * FROM app.seq_manual;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16398' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d+ app.seq_manual =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(seq_manual)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16398';
SELECT * FROM app.seq_manual;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16398' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d app.parent_id_seq =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_id_seq)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16400';
SELECT * FROM app.parent_id_seq;
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16400' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d app.parent_pkey =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_pkey)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16412';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16412' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16412' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d+ app.parent_pkey =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_pkey)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16412';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16412' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16412' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d app.parent_created_idx =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(parent_created_idx)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16416';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  pg_catalog.pg_get_indexdef(a.attrelid, a.attnum, TRUE) AS indexdef
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16416' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT i.indisunique, i.indisprimary, i.indisclustered, i.indisvalid,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferrable) AS condeferrable,
  (NOT i.indimmediate) AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x') AND condeferred) AS condeferred,
  a.amname, c2.relname, pg_catalog.pg_get_expr(i.indpred, i.indrelid, true)
FROM pg_catalog.pg_index i, pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_am a
WHERE i.indexrelid = c.oid AND c.oid = '16416' AND c.relam = a.oid
AND i.indrelid = c2.oid;

-- ===== \d app.part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(part)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.part =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(part)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.part_2024 =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(part_2024)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.part_2024 =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(part_2024)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.ident =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(ident)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.ident =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(ident)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.ft =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(ft)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d+ app.ft =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(ft)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;

-- ===== \d app.pair =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(pair)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16393';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16393' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d+ app.pair =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(pair)$'
  AND n.nspname ~ '^(app)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '16393';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '16393' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- ===== \d pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(pg_class)$'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d+ pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(pg_class)$'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, pg_catalog.array_to_string(c.reloptions || array(select 'toast.' || x from pg_catalog.unnest(tc.reloptions) x), ', ')
, c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum,
  a.attstorage, pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \d pg_catalog.pg_class =====
SELECT c.oid,
  n.nspname,
  c.relname
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~ '^(pg_class)$'
  AND n.nspname ~ '^(pg_catalog)$'
ORDER BY 2, 3;
SELECT c.relchecks, c.relkind, c.relhasindex, c.relhasrules, c.relhastriggers, c.relhasoids, '', c.reltablespace, CASE WHEN c.reloftype = 0 THEN '' ELSE c.reloftype::pg_catalog.regtype::pg_catalog.text END
FROM pg_catalog.pg_class c
 LEFT JOIN pg_catalog.pg_class tc ON (c.reltoastrelid = tc.oid)
WHERE c.oid = '1259';
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT substring(pg_catalog.pg_get_expr(d.adbin, d.adrelid) for 128)
   FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull, a.attnum
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '1259' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid, pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '1259' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, i.indisunique DESC, c2.relname;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhparent AND i.inhrelid = '1259' ORDER BY inhseqno;
SELECT c.oid::pg_catalog.regclass FROM pg_catalog.pg_class c, pg_catalog.pg_inherits i WHERE c.oid=i.inhrelid AND i.inhparent = '1259' ORDER BY c.oid::pg_catalog.regclass::pg_catalog.text;

-- ===== \dt =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dt+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dt app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','s','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname ~ '^(app)$'
ORDER BY 1,2;

-- ===== \dt+ app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','s','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname ~ '^(app)$'
ORDER BY 1,2;

-- ===== \di =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \di+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \diS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \di app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('i','s','')
      AND n.nspname !~ '^pg_toast'
  AND n.nspname ~ '^(app)$'
ORDER BY 1,2;

-- ===== \ds =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
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
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
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
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
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
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
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
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
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
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','s','')
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dm =====
-- (no queries sent)

-- ===== \dm+ =====
-- (no queries sent)

-- ===== \dE =====
-- (no queries sent)

-- ===== \dE+ =====
-- (no queries sent)

-- ===== \dtvsimE =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','v','i','S','')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
      AND n.nspname !~ '^pg_toast'
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1,2;

-- ===== \dtvsimE+ =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 's' THEN 'special' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner",
 c2.relname as "Table",
  pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) as "Size",
  pg_catalog.obj_description(c.oid, 'pg_class') as "Description"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
     LEFT JOIN pg_catalog.pg_class c2 ON i.indrelid = c2.oid
WHERE c.relkind IN ('r','v','i','S','')
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
    ELSE
    pg_catalog.array_to_string(ARRAY(
      SELECT
        pg_catalog.format_type(p.proargtypes[s.i], NULL)
      FROM
        pg_catalog.generate_series(0, pg_catalog.array_upper(p.proargtypes, 1)) AS s(i)
    ), ', ')
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
    ELSE
    pg_catalog.array_to_string(ARRAY(
      SELECT
        pg_catalog.format_type(p.proargtypes[s.i], NULL)
      FROM
        pg_catalog.generate_series(0, pg_catalog.array_upper(p.proargtypes, 1)) AS s(i)
    ), ', ')
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
    ELSE
    pg_catalog.array_to_string(ARRAY(
      SELECT
        pg_catalog.format_type(p.proargtypes[s.i], NULL)
      FROM
        pg_catalog.generate_series(0, pg_catalog.array_upper(p.proargtypes, 1)) AS s(i)
    ), ', ')
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
    ELSE
    pg_catalog.array_to_string(ARRAY(
      SELECT
        pg_catalog.format_type(p.proargtypes[s.i], NULL)
      FROM
        pg_catalog.generate_series(0, pg_catalog.array_upper(p.proargtypes, 1)) AS s(i)
    ), ', ')
  END AS "Argument data types",
  pg_catalog.obj_description(p.oid, 'pg_proc') as "Description"
FROM pg_catalog.pg_proc p
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.proisagg
  AND n.nspname ~ '^(app)$'
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
  spclocation AS "Location"
FROM pg_catalog.pg_tablespace
ORDER BY 1;

-- ===== \db+ =====
SELECT spcname AS "Name",
  pg_catalog.pg_get_userbyid(spcowner) AS "Owner",
  spclocation AS "Location",
  pg_catalog.array_to_string(spcacl, E'\n') AS "Access privileges",
  pg_catalog.shobj_description(oid, 'pg_tablespace') AS "Description"
FROM pg_catalog.pg_tablespace
ORDER BY 1;

-- ===== \db scrape_ts =====
SELECT spcname AS "Name",
  pg_catalog.pg_get_userbyid(spcowner) AS "Owner",
  spclocation AS "Location"
FROM pg_catalog.pg_tablespace
WHERE spcname ~ '^(scrape_ts)$'
ORDER BY 1;

-- ===== \dc =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c, pg_catalog.pg_namespace n
WHERE n.oid = c.connamespace
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
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c, pg_catalog.pg_namespace n
WHERE n.oid = c.connamespace
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
FROM pg_catalog.pg_conversion c, pg_catalog.pg_namespace n
WHERE n.oid = c.connamespace
  AND pg_catalog.pg_conversion_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dconfig =====
SELECT n.nspname AS "Schema",
       c.conname AS "Name",
       pg_catalog.pg_encoding_to_char(c.conforencoding) AS "Source",
       pg_catalog.pg_encoding_to_char(c.contoencoding) AS "Destination",
       CASE WHEN c.condefault THEN 'yes'
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c, pg_catalog.pg_namespace n
WHERE n.oid = c.connamespace
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
       ELSE 'no' END AS "Default?"
FROM pg_catalog.pg_conversion c, pg_catalog.pg_namespace n
WHERE n.oid = c.connamespace
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
FROM pg_catalog.pg_conversion c, pg_catalog.pg_namespace n
WHERE n.oid = c.connamespace
  AND c.conname ~ '^(work_mem)$'
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
       END as "Implicit?"
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
WHERE (true  AND pg_catalog.pg_type_is_visible(ts.oid)
) OR (true  AND pg_catalog.pg_type_is_visible(tt.oid)
)
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
       END as "Implicit?"
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
WHERE (true  AND pg_catalog.pg_type_is_visible(ts.oid)
) OR (true  AND pg_catalog.pg_type_is_visible(tt.oid)
)
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
       END as "Implicit?"
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
WHERE (true  AND (ts.typname ~ '^(int4)$'
        OR pg_catalog.format_type(ts.oid, NULL) ~ '^(int4)$')
  AND pg_catalog.pg_type_is_visible(ts.oid)
) OR (true  AND (tt.typname ~ '^(int4)$'
        OR pg_catalog.format_type(tt.oid, NULL) ~ '^(int4)$')
  AND pg_catalog.pg_type_is_visible(tt.oid)
)
ORDER BY 1, 2;

-- ===== \dd =====
SELECT DISTINCT tt.nspname AS "Schema", tt.name AS "Name", tt.object AS "Object", d.description AS "Description"
FROM (
  SELECT p.oid as oid, p.tableoid as tableoid,
  n.nspname as nspname,
  CAST(p.proname AS pg_catalog.text) as name,  CAST('aggregate' AS pg_catalog.text) as object
  FROM pg_catalog.pg_proc p
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
  WHERE p.proisagg
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_function_is_visible(p.oid)
UNION ALL
  SELECT p.oid as oid, p.tableoid as tableoid,
  n.nspname as nspname,
  CAST(p.proname AS pg_catalog.text) as name,  CAST('function' AS pg_catalog.text) as object
  FROM pg_catalog.pg_proc p
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
  WHERE NOT p.proisagg
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_function_is_visible(p.oid)
UNION ALL
  SELECT o.oid as oid, o.tableoid as tableoid,
  n.nspname as nspname,
  CAST(o.oprname AS pg_catalog.text) as name,  CAST('operator' AS pg_catalog.text) as object
  FROM pg_catalog.pg_operator o
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_operator_is_visible(o.oid)
UNION ALL
  SELECT t.oid as oid, t.tableoid as tableoid,
  n.nspname as nspname,
  pg_catalog.format_type(t.oid, NULL) as name,  CAST('data type' AS pg_catalog.text) as object
  FROM pg_catalog.pg_type t
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_type_is_visible(t.oid)
UNION ALL
  SELECT c.oid as oid, c.tableoid as tableoid,
  n.nspname as nspname,
  CAST(c.relname AS pg_catalog.text) as name,
  CAST(
    CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' END  AS pg_catalog.text) as object
  FROM pg_catalog.pg_class c
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'v', 'i', 'S')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_table_is_visible(c.oid)
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
  SELECT p.oid as oid, p.tableoid as tableoid,
  n.nspname as nspname,
  CAST(p.proname AS pg_catalog.text) as name,  CAST('aggregate' AS pg_catalog.text) as object
  FROM pg_catalog.pg_proc p
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
  WHERE p.proisagg
  AND n.nspname ~ '^(app)$'
UNION ALL
  SELECT p.oid as oid, p.tableoid as tableoid,
  n.nspname as nspname,
  CAST(p.proname AS pg_catalog.text) as name,  CAST('function' AS pg_catalog.text) as object
  FROM pg_catalog.pg_proc p
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
  WHERE NOT p.proisagg
  AND n.nspname ~ '^(app)$'
UNION ALL
  SELECT o.oid as oid, o.tableoid as tableoid,
  n.nspname as nspname,
  CAST(o.oprname AS pg_catalog.text) as name,  CAST('operator' AS pg_catalog.text) as object
  FROM pg_catalog.pg_operator o
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
WHERE n.nspname ~ '^(app)$'
UNION ALL
  SELECT t.oid as oid, t.tableoid as tableoid,
  n.nspname as nspname,
  pg_catalog.format_type(t.oid, NULL) as name,  CAST('data type' AS pg_catalog.text) as object
  FROM pg_catalog.pg_type t
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname ~ '^(app)$'
UNION ALL
  SELECT c.oid as oid, c.tableoid as tableoid,
  n.nspname as nspname,
  CAST(c.relname AS pg_catalog.text) as name,
  CAST(
    CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' END  AS pg_catalog.text) as object
  FROM pg_catalog.pg_class c
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'v', 'i', 'S')
  AND n.nspname ~ '^(app)$'
UNION ALL
  SELECT r.oid as oid, r.tableoid as tableoid,
  n.nspname as nspname,
  CAST(r.rulename AS pg_catalog.text) as name,  CAST('rule' AS pg_catalog.text) as object
  FROM pg_catalog.pg_rewrite r
       JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  WHERE r.rulename != '_RETURN'
  AND n.nspname ~ '^(app)$'
UNION ALL
  SELECT t.oid as oid, t.tableoid as tableoid,
  n.nspname as nspname,
  CAST(t.tgname AS pg_catalog.text) as name,  CAST('trigger' AS pg_catalog.text) as object
  FROM pg_catalog.pg_trigger t
       JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
       LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname ~ '^(app)$'
) AS tt
  JOIN pg_catalog.pg_description d ON (tt.oid = d.objoid AND tt.tableoid = d.classoid AND d.objsubid = 0)
ORDER BY 1, 2, 3;

-- ===== \dD =====
SELECT n.nspname as "Schema",
       t.typname as "Name",
       pg_catalog.format_type(t.typbasetype, t.typtypmod) as "Type",
       CASE WHEN t.typnotnull AND t.typdefault IS NOT NULL THEN 'not null default '||t.typdefault
            WHEN t.typnotnull AND t.typdefault IS NULL THEN 'not null'
            WHEN NOT t.typnotnull AND t.typdefault IS NOT NULL THEN 'default '||t.typdefault
            ELSE ''
       END as "Modifier",
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
       CASE WHEN t.typnotnull AND t.typdefault IS NOT NULL THEN 'not null default '||t.typdefault
            WHEN t.typnotnull AND t.typdefault IS NULL THEN 'not null'
            WHEN NOT t.typnotnull AND t.typdefault IS NOT NULL THEN 'default '||t.typdefault
            ELSE ''
       END as "Modifier",
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

-- ===== \dDS =====
SELECT n.nspname as "Schema",
       t.typname as "Name",
       pg_catalog.format_type(t.typbasetype, t.typtypmod) as "Type",
       CASE WHEN t.typnotnull AND t.typdefault IS NOT NULL THEN 'not null default '||t.typdefault
            WHEN t.typnotnull AND t.typdefault IS NULL THEN 'not null'
            WHEN NOT t.typnotnull AND t.typdefault IS NOT NULL THEN 'default '||t.typdefault
            ELSE ''
       END as "Modifier",
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
       CASE WHEN t.typnotnull AND t.typdefault IS NOT NULL THEN 'not null default '||t.typdefault
            WHEN t.typnotnull AND t.typdefault IS NULL THEN 'not null'
            WHEN NOT t.typnotnull AND t.typdefault IS NOT NULL THEN 'default '||t.typdefault
            ELSE ''
       END as "Modifier",
       pg_catalog.array_to_string(ARRAY(
         SELECT pg_catalog.pg_get_constraintdef(r.oid, true) FROM pg_catalog.pg_constraint r WHERE t.oid = r.contypid
       ), ' ') as "Check"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype = 'd'
  AND n.nspname ~ '^(app)$'
ORDER BY 1, 2;

-- ===== \ddp =====
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS "Owner",
  n.nspname AS "Schema",
  CASE d.defaclobjtype WHEN 'r' THEN 'table' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'function' END AS "Type",
  pg_catalog.array_to_string(d.defaclacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_default_acl d
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
ORDER BY 1, 2, 3;

-- ===== \ddp app =====
SELECT pg_catalog.pg_get_userbyid(d.defaclrole) AS "Owner",
  n.nspname AS "Schema",
  CASE d.defaclobjtype WHEN 'r' THEN 'table' WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'function' END AS "Type",
  pg_catalog.array_to_string(d.defaclacl, E'\n') AS "Access privileges"
FROM pg_catalog.pg_default_acl d
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
WHERE (n.nspname ~ '^(app)$'
        OR pg_catalog.pg_get_userbyid(d.defaclrole) ~ '^(app)$')
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
  s.srvoptions AS "Options"
FROM pg_catalog.pg_foreign_server s
     JOIN pg_catalog.pg_foreign_data_wrapper f ON f.oid=s.srvfdw
ORDER BY 1;

-- ===== \det =====
-- (no queries sent)

-- ===== \det+ =====
-- (no queries sent)

-- ===== \deu =====
SELECT um.srvname AS "Server",
  um.usename AS "User name"
FROM pg_catalog.pg_user_mappings um
ORDER BY 1, 2;

-- ===== \deu+ =====
SELECT um.srvname AS "Server",
  um.usename AS "User name",
  um.umoptions AS "Options"
FROM pg_catalog.pg_user_mappings um
ORDER BY 1, 2;

-- ===== \dew =====
SELECT fdwname AS "Name",
  pg_catalog.pg_get_userbyid(fdwowner) AS "Owner",
  fdwvalidator::pg_catalog.regproc AS "Validator"
FROM pg_catalog.pg_foreign_data_wrapper
ORDER BY 1;

-- ===== \dew+ =====
SELECT fdwname AS "Name",
  pg_catalog.pg_get_userbyid(fdwowner) AS "Owner",
  fdwvalidator::pg_catalog.regproc AS "Validator",
  pg_catalog.array_to_string(fdwacl, E'\n') AS "Access privileges",
  fdwoptions AS "Options"
FROM pg_catalog.pg_foreign_data_wrapper
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
WHERE n.nspname ~ '^(app)$'
ORDER BY 1, 2, 4;

-- ===== \df+ app.add =====
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
WHERE p.proname ~ '^(add)$'
  AND n.nspname ~ '^(app)$'
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
WHERE c.oid = '11313' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11315' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11317' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11319' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11321' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11323' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11325' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11327' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11329' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11331' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11333' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11335' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11337' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11339' AND m.mapcfg = c.oid 
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
WHERE c.oid = '11341' AND m.mapcfg = c.oid 
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
-- (no queries sent)

-- ===== \dL+ =====
-- (no queries sent)

-- ===== \dLS =====
-- (no queries sent)

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
WHERE n.nspname ~ '^(app)$'
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
WHERE o.oprname ~ E'^(<\\+>)$'
  AND pg_catalog.pg_operator_is_visible(o.oid)
ORDER BY 1, 2, 3, 4;

-- ===== \dO =====
-- (no queries sent)

-- ===== \dO+ =====
-- (no queries sent)

-- ===== \dOS =====
-- (no queries sent)

-- ===== \dp =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'S' THEN 'sequence' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'S')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dpS =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'S' THEN 'sequence' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'S')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \dp app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'S' THEN 'sequence' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'S')
  AND n.nspname ~ '^(app)$'
ORDER BY 1, 2;

-- ===== \dP =====
-- (no queries sent)

-- ===== \dP+ =====
-- (no queries sent)

-- ===== \dPn =====
-- (no queries sent)

-- ===== \dPn+ =====
-- (no queries sent)

-- ===== \dPi =====
-- (no queries sent)

-- ===== \dPi+ =====
-- (no queries sent)

-- ===== \dPt =====
-- (no queries sent)

-- ===== \dPt+ =====
-- (no queries sent)

-- ===== \dPtn =====
-- (no queries sent)

-- ===== \dP app.part =====
-- (no queries sent)

-- ===== \dP+ app.part =====
-- (no queries sent)

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
          ORDER BY e.oid
      ),
      E'\n'
  ) AS "Elements",
  pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
  AND pg_catalog.pg_type_is_visible(t.oid)
ORDER BY 1, 2;

-- ===== \dTS =====
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
SELECT n.nspname as "Schema",
  pg_catalog.format_type(t.oid, NULL) AS "Name",
  pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND n.nspname ~ '^(app)$'
ORDER BY 1, 2;

-- ===== \dT+ app.mood =====
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
          ORDER BY e.oid
      ),
      E'\n'
  ) AS "Elements",
  pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND (t.typname ~ '^(mood)$'
        OR pg_catalog.format_type(t.oid, NULL) ~ '^(mood)$')
  AND n.nspname ~ '^(app)$'
ORDER BY 1, 2;

-- ===== \dT+ app.pair =====
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
          ORDER BY e.oid
      ),
      E'\n'
  ) AS "Elements",
  pg_catalog.obj_description(t.oid, 'pg_type') as "Description"
FROM pg_catalog.pg_type t
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_type el WHERE el.oid = t.typelem AND el.typarray = t.oid)
  AND (t.typname ~ '^(pair)$'
        OR pg_catalog.format_type(t.oid, NULL) ~ '^(pair)$')
  AND n.nspname ~ '^(app)$'
ORDER BY 1, 2;

-- ===== \dx =====
-- (no queries sent)

-- ===== \dx+ =====
-- (no queries sent)

-- ===== \dx hstore =====
-- (no queries sent)

-- ===== \dx+ hstore =====
-- (no queries sent)

-- ===== \dX =====
-- (no queries sent)

-- ===== \dX+ =====
-- (no queries sent)

-- ===== \dX app.* =====
-- (no queries sent)

-- ===== \dy =====
-- (no queries sent)

-- ===== \dy+ =====
-- (no queries sent)

-- ===== \z =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'S' THEN 'sequence' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'S')
  AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 1, 2;

-- ===== \z app.* =====
SELECT n.nspname as "Schema",
  c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'S' THEN 'sequence' END as "Type",
  pg_catalog.array_to_string(c.relacl, E'\n') AS "Access privileges",
  pg_catalog.array_to_string(ARRAY(
    SELECT attname || E':\n  ' || pg_catalog.array_to_string(attacl, E'\n  ')
    FROM pg_catalog.pg_attribute a
    WHERE attrelid = c.oid AND NOT attisdropped AND attacl IS NOT NULL
  ), E'\n') AS "Column access privileges"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'S')
  AND n.nspname ~ '^(app)$'
ORDER BY 1, 2;

-- ===== \zS =====
-- (no queries sent)

-- ===== \sf app.add =====
-- (no queries sent)

-- ===== \sf+ app.add =====
-- (no queries sent)

-- ===== \sf app.touch =====
-- (no queries sent)

-- ===== \sv app.parent_view =====
-- (no queries sent)

-- ===== \sv+ app.parent_view =====
-- (no queries sent)

-- ===== \sv app.parent_mv =====
-- (no queries sent)

-- ===== \conninfo =====
-- (no queries sent)
