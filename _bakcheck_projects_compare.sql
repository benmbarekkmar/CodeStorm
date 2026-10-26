\pset footer off
select 'tables' as metric, count(*)::text as value
from information_schema.tables
where table_schema='public' and table_type='BASE TABLE';

select 'columns_fp' as metric,
  md5(string_agg(table_name||'.'||column_name||':'||data_type||':'
      ||coalesce(character_maximum_length::text,'0')||':'||is_nullable, ',' order by table_name, ordinal_position)) as value
from information_schema.columns
where table_schema='public';

select 'constraints_fp' as metric,
  md5(string_agg(conname, ',' order by conname)) as value
from pg_constraint c join pg_namespace n on n.oid=c.connamespace
where n.nspname='public';

select 'indexes_fp' as metric,
  md5(string_agg(indexname, ',' order by indexname)) as value
from pg_indexes where schemaname='public';

create temp table _rowcounts (tbl text, n bigint);
do $$
declare r record;
begin
  for r in select tablename from pg_tables where schemaname='public' order by 1 loop
    execute format('insert into _rowcounts select %L, count(*) from public.%I', r.tablename, r.tablename);
  end loop;
end $$;
select 'rows_fp' as metric, md5(string_agg(tbl||'='||n, ',' order by tbl)) as value from _rowcounts;
select 'rows_total' as metric, sum(n)::text as value from _rowcounts;
select 'migrations' as metric, count(*)::text as value from django_migrations;
