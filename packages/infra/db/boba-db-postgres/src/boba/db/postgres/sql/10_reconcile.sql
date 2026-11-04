create or replace function pg_temp.migration_constraint(definition oid, target_schema text)
returns text language plpgsql as $fn$
declare
    item pg_constraint;
    local_columns text;
    remote_columns text;
    remote_table text;
    remote_schema text;
    actions constant jsonb := '{{"a":"NO ACTION","r":"RESTRICT","c":"CASCADE","n":"SET NULL","d":"SET DEFAULT"}}';
begin
    if definition is null then
        return null;
    end if;
    select * into strict item from pg_constraint where oid = definition;
    if item.contype <> 'f' then
        return pg_get_constraintdef(definition, false);
    end if;
    select string_agg(format('%I', a.attname), ', ' order by k.n)
      into local_columns
      from unnest(item.conkey) with ordinality k(attnum, n)
      join pg_attribute a on a.attrelid = item.conrelid and a.attnum = k.attnum;
    select string_agg(format('%I', a.attname), ', ' order by k.n)
      into remote_columns
      from unnest(item.confkey) with ordinality k(attnum, n)
      join pg_attribute a on a.attrelid = item.confrelid and a.attnum = k.attnum;
    select c.relname, coalesce(target_schema, n.nspname) into remote_table, remote_schema
      from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.oid = item.confrelid;
    return format('FOREIGN KEY (%s) REFERENCES %I.%I (%s) MATCH %s ON UPDATE %s ON DELETE %s %s',
        local_columns, remote_schema, remote_table, remote_columns,
        case item.confmatchtype when 'f' then 'FULL' when 'p' then 'PARTIAL' else 'SIMPLE' end,
        actions->>item.confupdtype::text, actions->>item.confdeltype::text,
        case when not item.condeferrable then 'NOT DEFERRABLE'
             when item.condeferred then 'DEFERRABLE INITIALLY DEFERRED'
             else 'DEFERRABLE INITIALLY IMMEDIATE' end);
end
$fn$;

create or replace function pg_temp.migration_same_columns(actual regclass, model regclass)
returns boolean language sql as $fn$
    select not exists (
        select 1
        from (select * from pg_attribute where attrelid = actual and attnum > 0 and not attisdropped) a
        full join (select * from pg_attribute where attrelid = model and attnum > 0 and not attisdropped) m
          using (attname)
        where a.attnum is null or m.attnum is null
           or (a.atttypid, a.atttypmod, a.attcollation, a.attgenerated, a.attidentity)
              is distinct from (m.atttypid, m.atttypmod, m.attcollation, m.attgenerated, m.attidentity)
           or (m.attgenerated <> '' and
               (select pg_get_expr(adbin, adrelid) from pg_attrdef where adrelid = actual and adnum = a.attnum)
               is distinct from
               (select pg_get_expr(adbin, adrelid) from pg_attrdef where adrelid = model and adnum = m.attnum))
    );
$fn$;

create or replace function pg_temp.migration_sequence(actual regclass, model regclass, used_value bigint default null)
returns void language plpgsql as $fn$
declare
    wanted pg_sequence;
    present pg_sequence;
    last_number bigint;
    called boolean;
    next_number bigint;
    needed bigint;
    wanted_comment text;
begin
    select * into strict wanted from pg_sequence where seqrelid = model;
    select * into strict present from pg_sequence where seqrelid = actual;
    if (present.seqtypid, present.seqstart, present.seqincrement, present.seqmax,
        present.seqmin, present.seqcache, present.seqcycle) is distinct from
       (wanted.seqtypid, wanted.seqstart, wanted.seqincrement, wanted.seqmax,
        wanted.seqmin, wanted.seqcache, wanted.seqcycle) then
        execute format('ALTER SEQUENCE %s AS %s INCREMENT BY %s MINVALUE %s MAXVALUE %s START WITH %s CACHE %s %s',
            actual, format_type(wanted.seqtypid, null), wanted.seqincrement,
            wanted.seqmin, wanted.seqmax, wanted.seqstart, wanted.seqcache,
            case when wanted.seqcycle then 'CYCLE' else 'NO CYCLE' end);
    end if;
    execute format('SELECT last_value, is_called FROM %s', actual) into last_number, called;
    next_number := last_number;
    if called then
        next_number := next_number + wanted.seqincrement;
    end if;
    if used_value is not null then
        needed := used_value + wanted.seqincrement;
        if (wanted.seqincrement > 0 and needed > next_number)
           or (wanted.seqincrement < 0 and needed < next_number) then
            execute format('ALTER SEQUENCE %s RESTART WITH %s', actual, needed);
        end if;
    end if;
    wanted_comment := obj_description(model, 'pg_class');
    if obj_description(actual, 'pg_class') is distinct from wanted_comment then
        execute format('COMMENT ON SEQUENCE %s IS %L', actual, wanted_comment);
    end if;
end
$fn$;

create or replace function pg_temp.migration_identity(actual regclass, model regclass, column_name text)
returns void language plpgsql as $fn$
declare
    desired "char";
    present "char";
    previous_sequence regclass;
    actual_sequence regclass;
    model_sequence regclass;
    previous_value bigint;
    was_called boolean;
    used_value bigint;
    direction bigint;
begin
    select attidentity into desired from pg_attribute where attrelid = model and attname = column_name;
    select attidentity into present from pg_attribute where attrelid = actual and attname = column_name;
    previous_sequence := pg_get_serial_sequence(actual::text, column_name)::regclass;
    if desired = '' then
        if present <> '' then
            execute format('ALTER TABLE %s ALTER COLUMN %I DROP IDENTITY', actual, column_name);
        elsif previous_sequence is not null then
            execute format('ALTER TABLE %s ALTER COLUMN %I DROP DEFAULT', actual, column_name);
            execute format('DROP SEQUENCE %s', previous_sequence);
        end if;
        return;
    end if;
    if present = '' then
        if previous_sequence is not null then
            execute format('SELECT last_value, is_called FROM %s', previous_sequence)
                into previous_value, was_called;
            if not was_called then
                previous_value := null;
            end if;
            execute format('ALTER TABLE %s ALTER COLUMN %I DROP DEFAULT', actual, column_name);
            execute format('DROP SEQUENCE %s', previous_sequence);
        end if;
        execute format('ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', actual, column_name);
        execute format('ALTER TABLE %s ALTER COLUMN %I DROP DEFAULT', actual, column_name);
        execute format('ALTER TABLE %s ALTER COLUMN %I ADD GENERATED %s AS IDENTITY', actual, column_name,
            case desired when 'a' then 'ALWAYS' else 'BY DEFAULT' end);
    elsif present <> desired then
        execute format('ALTER TABLE %s ALTER COLUMN %I SET GENERATED %s', actual, column_name,
            case desired when 'a' then 'ALWAYS' else 'BY DEFAULT' end);
    end if;
    actual_sequence := pg_get_serial_sequence(actual::text, column_name)::regclass;
    model_sequence := pg_get_serial_sequence(model::text, column_name)::regclass;
    select seqincrement into direction from pg_sequence where seqrelid = model_sequence;
    execute format('SELECT %s(%I) FROM %s', case when direction > 0 then 'max' else 'min' end,
        column_name, actual) into used_value;
    if direction > 0 then
        used_value := greatest(used_value, previous_value);
    else
        used_value := least(used_value, previous_value);
    end if;
    perform pg_temp.migration_sequence(actual_sequence, model_sequence, used_value);
end
$fn$;

create or replace function pg_temp.migration_columns(actual regclass, model regclass)
returns void language plpgsql as $fn$
declare
    wanted record;
    present record;
    extra record;
    column_type text;
    expression text;
    actual_expression text;
    wanted_comment text;
    definition text;
begin
    for extra in
        select attname from pg_attribute a
        where attrelid = actual and attnum > 0 and not attisdropped
          and not exists (select 1 from pg_attribute m where m.attrelid = model and m.attname = a.attname and not m.attisdropped)
    loop
        execute format('ALTER TABLE %s DROP COLUMN %I', actual, extra.attname);
    end loop;
    for wanted in
        select * from pg_attribute where attrelid = model and attnum > 0 and not attisdropped order by attnum
    loop
        column_type := format_type(wanted.atttypid, wanted.atttypmod);
        if wanted.attcollation <> 0 then
            select column_type || format(' COLLATE %I.%I', n.nspname, c.collname)
              into column_type from pg_collation c join pg_namespace n on n.oid = c.collnamespace
              where c.oid = wanted.attcollation;
        end if;
        select pg_get_expr(adbin, adrelid) into expression
          from pg_attrdef where adrelid = model and adnum = wanted.attnum;
        select * into present from pg_attribute
          where attrelid = actual and attname = wanted.attname and not attisdropped;
        if present.attnum is not null then
            select pg_get_expr(adbin, adrelid) into actual_expression
              from pg_attrdef where adrelid = actual and adnum = present.attnum;
            if wanted.attgenerated <> '' and
               (present.attgenerated = '' or expression is distinct from actual_expression
                or (wanted.atttypid, wanted.atttypmod, wanted.attcollation)
                   is distinct from (present.atttypid, present.atttypmod, present.attcollation)) then
                execute format('ALTER TABLE %s DROP COLUMN %I', actual, wanted.attname);
                present.attnum := null;
            elsif wanted.attgenerated = '' and present.attgenerated <> '' then
                execute format('ALTER TABLE %s ALTER COLUMN %I DROP EXPRESSION', actual, wanted.attname);
                present.attgenerated := '';
            end if;
        end if;
        if present.attnum is null then
            definition := column_type;
            if wanted.attgenerated <> '' then
                definition := definition || format(' GENERATED ALWAYS AS (%s) STORED', expression);
            elsif wanted.attidentity <> '' then
                definition := definition || ' GENERATED ' ||
                    case wanted.attidentity when 'a' then 'ALWAYS' else 'BY DEFAULT' end || ' AS IDENTITY';
            elsif expression is not null then
                definition := definition || ' DEFAULT ' || expression;
            end if;
            if wanted.attnotnull then
                definition := definition || ' NOT NULL';
            end if;
            execute format('ALTER TABLE %s ADD COLUMN %I %s', actual, wanted.attname, definition);
        elsif (wanted.atttypid, wanted.atttypmod, wanted.attcollation)
              is distinct from (present.atttypid, present.atttypmod, present.attcollation) then
            if present.attidentity <> '' and wanted.attidentity = '' then
                execute format('ALTER TABLE %s ALTER COLUMN %I DROP IDENTITY', actual, wanted.attname);
            end if;
            execute format('ALTER TABLE %s ALTER COLUMN %I DROP DEFAULT', actual, wanted.attname);
            execute format('ALTER TABLE %s ALTER COLUMN %I TYPE %s USING %I::%s',
                actual, wanted.attname, column_type, wanted.attname, format_type(wanted.atttypid, wanted.atttypmod));
        end if;
        perform pg_temp.migration_identity(actual, model, wanted.attname);
        if wanted.attgenerated = '' and wanted.attidentity = '' then
            select pg_get_expr(d.adbin, d.adrelid) into actual_expression
              from pg_attrdef d join pg_attribute a on a.attrelid = d.adrelid and a.attnum = d.adnum
              where d.adrelid = actual and a.attname = wanted.attname;
            if expression is distinct from actual_expression then
                if expression is null then
                    execute format('ALTER TABLE %s ALTER COLUMN %I DROP DEFAULT', actual, wanted.attname);
                else
                    execute format('ALTER TABLE %s ALTER COLUMN %I SET DEFAULT %s', actual, wanted.attname, expression);
                end if;
            end if;
        end if;
        select * into present from pg_attribute where attrelid = actual and attname = wanted.attname and not attisdropped;
        if wanted.attnotnull is distinct from present.attnotnull then
            execute format('ALTER TABLE %s ALTER COLUMN %I %s NOT NULL', actual, wanted.attname,
                case when wanted.attnotnull then 'SET' else 'DROP' end);
        end if;
        wanted_comment := col_description(model, wanted.attnum);
        if col_description(actual, present.attnum) is distinct from wanted_comment then
            execute format('COMMENT ON COLUMN %s.%I IS %L', actual, wanted.attname, wanted_comment);
        end if;
    end loop;
end
$fn$;

create or replace function pg_temp.migration_index(index_oid oid)
returns text language sql as $fn$
    select substring(pg_get_indexdef(index_oid) from ' USING .*');
$fn$;

create or replace function pg_temp.migration_indexes(actual regclass, model regclass)
returns void language plpgsql as $fn$
declare
    item record;
    wanted record;
    present record;
    wanted_comment text;
begin
    for item in
        select i.indexrelid, c.relname from pg_index i join pg_class c on c.oid = i.indexrelid
        where i.indrelid = actual and not exists (
            select 1 from pg_constraint where conrelid = actual and conindid = i.indexrelid and contype in ('p','u','x'))
    loop
        select i.*, c.relname into wanted
          from pg_index i join pg_class c on c.oid = i.indexrelid
          where i.indrelid = model and c.relname = item.relname
            and not exists (select 1 from pg_constraint
                where conrelid = model and conindid = i.indexrelid and contype in ('p','u','x'));
        if wanted.indexrelid is null then
            execute format('DROP INDEX %s', item.indexrelid::regclass);
        end if;
    end loop;
    for wanted in
        select i.*, c.relname from pg_index i join pg_class c on c.oid = i.indexrelid
        where i.indrelid = model and not exists (
            select 1 from pg_constraint where conrelid = model and conindid = i.indexrelid and contype in ('p','u','x'))
    loop
        select i.* into present from pg_index i join pg_class c on c.oid = i.indexrelid
          where i.indrelid = actual and c.relname = wanted.relname;
        if present.indexrelid is not null then
            if present.indisunique <> wanted.indisunique or not present.indisvalid
               or pg_temp.migration_index(present.indexrelid) is distinct from pg_temp.migration_index(wanted.indexrelid) then
                execute format('DROP INDEX %s', present.indexrelid::regclass);
                present.indexrelid := null;
            end if;
        end if;
        if present.indexrelid is null then
            execute format('CREATE %s INDEX %I ON %s%s', case when wanted.indisunique then 'UNIQUE' else '' end,
                wanted.relname, actual, pg_temp.migration_index(wanted.indexrelid));
        end if;
        select i.* into present from pg_index i join pg_class c on c.oid = i.indexrelid
          where i.indrelid = actual and c.relname = wanted.relname;
        wanted_comment := obj_description(wanted.indexrelid, 'pg_class');
        if obj_description(present.indexrelid, 'pg_class') is distinct from wanted_comment then
            execute format('COMMENT ON INDEX %s IS %L', present.indexrelid::regclass, wanted_comment);
        end if;
    end loop;
end
$fn$;

create or replace function pg_temp.reconcile_tables(target_schema text, models regclass[], unlogged_tables text[] default array[]::text[])
returns void language plpgsql as $fn$
declare
    model regclass;
    actual regclass;
    table_name text;
    item record;
    wanted record;
    present record;
    changed boolean;
    desired text;
    wanted_comment text;
begin
    foreach model in array models loop
        select relname into table_name from pg_class where oid = model;
        actual := to_regclass(format('%I.%I', target_schema, table_name));
        if actual is not null then
            execute format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE', actual);
        end if;
    end loop;
    foreach model in array models loop
        select relname into table_name from pg_class where oid = model;
        actual := to_regclass(format('%I.%I', target_schema, table_name));
        if actual is null then
            continue;
        end if;
        for item in select * from pg_constraint where conrelid = actual and contype = 'f' loop
            execute format('ALTER TABLE %s DROP CONSTRAINT %I', actual, item.conname);
        end loop;
        for item in select tgname from pg_trigger where tgrelid = actual and not tgisinternal loop
            execute format('DROP TRIGGER %I ON %s', item.tgname, actual);
        end loop;
    end loop;
    foreach model in array models loop
        select relname into table_name from pg_class where oid = model;
        actual := to_regclass(format('%I.%I', target_schema, table_name));
        if actual is null then
            execute format('CREATE %s TABLE %I.%I ()',
                case when table_name = any(unlogged_tables) then 'UNLOGGED' else '' end,
                target_schema, table_name);
            actual := to_regclass(format('%I.%I', target_schema, table_name));
        end if;
        changed := not pg_temp.migration_same_columns(actual, model);
        for item in select * from pg_constraint where conrelid = actual and contype <> 'f' loop
            select * into wanted from pg_constraint where conrelid = model and conname = item.conname;
            if changed or wanted.oid is null or not item.convalidated
               or pg_temp.migration_constraint(item.oid, null)
                  is distinct from pg_temp.migration_constraint(wanted.oid, target_schema) then
                execute format('ALTER TABLE %s DROP CONSTRAINT %I', actual, item.conname);
            end if;
        end loop;
        if changed then
            for item in select indexrelid from pg_index where indrelid = actual loop
                execute format('DROP INDEX %s', item.indexrelid::regclass);
            end loop;
            for item in select attname from pg_attribute
                        where attrelid = actual and attnum > 0 and not attisdropped and attgenerated <> '' loop
                execute format('ALTER TABLE %s DROP COLUMN %I', actual, item.attname);
            end loop;
        end if;
        perform pg_temp.migration_columns(actual, model);
        desired := 'p';
        if table_name = any(unlogged_tables) then
            desired := 'u';
        end if;
        if (select relpersistence::text from pg_class where oid = actual) <> desired then
            execute format('ALTER TABLE %s SET %s', actual,
                case when desired = 'u' then 'UNLOGGED' else 'LOGGED' end);
        end if;
        perform pg_temp.migration_indexes(actual, model);
        for wanted in select * from pg_constraint where conrelid = model and contype <> 'f' loop
            select * into present from pg_constraint where conrelid = actual and conname = wanted.conname;
            if present.oid is null then
                execute format('ALTER TABLE %s ADD CONSTRAINT %I %s', actual, wanted.conname,
                    pg_temp.migration_constraint(wanted.oid, target_schema));
            end if;
        end loop;
        wanted_comment := obj_description(model, 'pg_class');
        if obj_description(actual, 'pg_class') is distinct from wanted_comment then
            execute format('COMMENT ON TABLE %s IS %L', actual, wanted_comment);
        end if;
    end loop;
    foreach model in array models loop
        select relname into table_name from pg_class where oid = model;
        actual := to_regclass(format('%I.%I', target_schema, table_name));
        for wanted in select * from pg_constraint where conrelid = model and contype = 'f' loop
            select * into present from pg_constraint where conrelid = actual and conname = wanted.conname;
            if present.oid is null then
                execute format('ALTER TABLE %s ADD CONSTRAINT %I %s', actual, wanted.conname,
                    pg_temp.migration_constraint(wanted.oid, target_schema));
            end if;
        end loop;
        for wanted in select * from pg_constraint where conrelid = model loop
            select * into present from pg_constraint where conrelid = actual and conname = wanted.conname;
            wanted_comment := obj_description(wanted.oid, 'pg_constraint');
            if obj_description(present.oid, 'pg_constraint') is distinct from wanted_comment then
                execute format('COMMENT ON CONSTRAINT %I ON %s IS %L', wanted.conname, actual, wanted_comment);
            end if;
            if wanted.contype in ('p','u','x') then
                wanted_comment := obj_description(wanted.conindid, 'pg_class');
                if obj_description(present.conindid, 'pg_class') is distinct from wanted_comment then
                    execute format('COMMENT ON INDEX %s IS %L', present.conindid::regclass, wanted_comment);
                end if;
            end if;
        end loop;
    end loop;
end
$fn$;
