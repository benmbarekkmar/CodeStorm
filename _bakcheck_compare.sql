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

select 'rows_total' as metric, sum(n)::text as value from (
  select count(*) as n from accounts_user
  union all select count(*) from hackathons_hackathon
  union all select count(*) from teams_team
  union all select count(*) from projects_project
  union all select count(*) from evaluations_evaluation
  union all select count(*) from evaluations_evaluationcriterion
  union all select count(*) from evaluations_evaluationscore
  union all select count(*) from prizes_prize
  union all select count(*) from django_migrations
) r;
