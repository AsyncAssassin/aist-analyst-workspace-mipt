SELECT jsonb_pretty(jsonb_build_object(
 'synthetic', true,
 'migration', (SELECT version_num FROM alembic_version),
 'counts', jsonb_build_object(
  'projects',(SELECT count(*) FROM aist.projects),
  'sources',(SELECT count(*) FROM aist.sources),
  'source_versions',(SELECT count(*) FROM aist.source_versions),
  'conversations',(SELECT count(*) FROM aist.conversations),
  'messages',(SELECT count(*) FROM aist.messages),
  'project_counts',(SELECT jsonb_agg(jsonb_build_object('project_id',p.id,'conversations',(SELECT count(*) FROM aist.conversations c WHERE c.project_id=p.id),'sources',(SELECT count(*) FROM aist.sources s WHERE s.project_id=p.id),'source_versions',(SELECT count(*) FROM aist.source_versions v WHERE v.project_id=p.id),'messages',(SELECT count(*) FROM aist.messages m WHERE m.project_id=p.id),'runs',(SELECT count(*) FROM aist.runs r WHERE r.project_id=p.id),'artifact_versions',(SELECT count(*) FROM aist.artifact_versions v WHERE v.project_id=p.id))) FROM aist.projects p),
 'event_counts',(SELECT jsonb_agg(jsonb_build_object('run_id',e.run_id,'events',e.events,'min_seq',e.min_seq,'max_seq',e.max_seq)) FROM (SELECT run_id,count(*) AS events,min(seq) AS min_seq,max(seq) AS max_seq FROM aist.run_events GROUP BY run_id) e),
 'runs',(SELECT count(*) FROM aist.runs),
  'artifact_versions',(SELECT count(*) FROM aist.artifact_versions),
  'model_calls',(SELECT count(*) FROM aist.model_calls),
  'usage_entries',(SELECT count(*) FROM aist.usage_entries)),
 'project_counts',(SELECT jsonb_agg(jsonb_build_object('project_id',p.id,'conversations',(SELECT count(*) FROM aist.conversations c WHERE c.project_id=p.id),'sources',(SELECT count(*) FROM aist.sources s WHERE s.project_id=p.id),'source_versions',(SELECT count(*) FROM aist.source_versions v WHERE v.project_id=p.id),'messages',(SELECT count(*) FROM aist.messages m WHERE m.project_id=p.id),'runs',(SELECT count(*) FROM aist.runs r WHERE r.project_id=p.id),'artifact_versions',(SELECT count(*) FROM aist.artifact_versions v WHERE v.project_id=p.id))) FROM aist.projects p),
 'event_counts',(SELECT jsonb_agg(jsonb_build_object('run_id',e.run_id,'events',e.events,'min_seq',e.min_seq,'max_seq',e.max_seq)) FROM (SELECT run_id,count(*) AS events,min(seq) AS min_seq,max(seq) AS max_seq FROM aist.run_events GROUP BY run_id) e),
 'runs',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'project_id',project_id,'conversation_id',conversation_id,'state',state,'revision',revision,'pins',input->'source_versions','result_version_id',result_version_id,'last_event_seq',last_event_seq) ORDER BY created_at), '[]'::jsonb) FROM aist.runs),
 'versions',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'source_id',source_id,'revision',revision,'sha256',sha256,'page_count',page_count,'processing_state',processing_state) ORDER BY created_at),'[]'::jsonb) FROM aist.source_versions)
));
