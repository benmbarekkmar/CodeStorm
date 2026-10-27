\pset format unaligned
\pset tuples_only on
create temp table fp(tbl text, n bigint, h text);
do $$
declare r record;
begin
  for r in select tablename from pg_tables where schemaname='public' order by 1 loop
    execute format(
      'insert into fp select %L, count(*), md5(coalesce(string_agg(row_text,%L order by row_text),%L)) '
      'from (select t::text as row_text from public.%I t) s',
      r.tablename, '|', '', r.tablename);
  end loop;
end $$;
select tbl || '|' || n || '|' || h from fp order by 1;
