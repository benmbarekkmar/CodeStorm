SELECT 'cols' AS kind, md5(string_agg(table_name||'|'||column_name||'|'||data_type||'|'||is_nullable, ',' ORDER BY table_name, ordinal_position)) AS fp
FROM information_schema.columns WHERE table_schema='public'
UNION ALL
SELECT 'fk', md5(string_agg(conname::text, ',' ORDER BY conname)) FROM pg_constraint WHERE contype='f'
UNION ALL
SELECT 'idx', md5(string_agg(indexname, ',' ORDER BY indexname)) FROM pg_indexes WHERE schemaname='public'
UNION ALL
SELECT 'ncols', count(*)::text FROM information_schema.columns WHERE table_schema='public'
UNION ALL
SELECT 'ntables', count(*)::text FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'
UNION ALL
SELECT 'fixed_rows', (
  (SELECT count(*) FROM accounts_user)
  + (SELECT count(*) FROM hackathons_hackathon)
  + (SELECT count(*) FROM teams_team)
  + (SELECT count(*) FROM projects_project)
  + (SELECT count(*) FROM evaluations_evaluation)
  + (SELECT count(*) FROM evaluations_evaluationcriterion)
  + (SELECT count(*) FROM analyses_aireport)
  + (SELECT count(*) FROM resources_resource)
  + (SELECT count(*) FROM prizes_prize)
)::text
UNION ALL
SELECT 'per_table', 'users='||(SELECT count(*) FROM accounts_user)
  ||' hackathons='||(SELECT count(*) FROM hackathons_hackathon)
  ||' teams='||(SELECT count(*) FROM teams_team)
  ||' projects='||(SELECT count(*) FROM projects_project)
  ||' aireports='||(SELECT count(*) FROM analyses_aireport)
UNION ALL
SELECT 'migrations', md5(string_agg(app||'>'||name, ',' ORDER BY app, name)) FROM django_migrations
ORDER BY 1;
