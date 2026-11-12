"""Report the interrupted extraction in the chat and preserve its independently committed evidence."""
from .errors import ApiError
from .extraction import recorded_extractions
from .runs import execute


def recover_extraction(runs, c, run):
    """Called under the Run lock. The failed model call stays unknown, including its reservation."""
    request = run['input']
    if (request['skill_version_id'] != 'skill_analytical-note-v3' or request['base_version_id']
            or request['target_artifact_id'] or len(request['source_versions']) != 1):
        return None
    if not execute(c, """SELECT 1 FROM aist.model_calls WHERE run_id=:r AND data->>'purpose'='extraction'
                         AND data->>'transport_state'='unknown' LIMIT 1""", r=run['id']).scalar():
        return None
    runs.session_alive(c, run)
    runs.require_live_access(run['owner_id'], required=True)
    version = runs.service.scoped(c, run['owner_id'], run['project_id'], 'source_versions',
                                  request['source_versions'][0]['source_version_id'])
    _, problem = runs.service.inspect(c, version)
    if problem or version['processing_state'] != 'ready':
        raise ApiError('source_not_ready')

    received = set(execute(c, """SELECT id FROM aist.model_calls WHERE run_id=:r AND data->>'purpose'='extraction'
        AND data->>'provider_outcome'='success' AND data->>'delivery'='delivered'
        AND data->>'transport_state'='observed'""", r=run['id']).scalars())
    entries = [e for e in recorded_extractions(run) if e['call_id'] in received and e['source'] == request['source_versions'][0]]
    refs = {(ref['source_version_id'], ref['fact_id']) for entry in entries for ref in entry['fact_refs']}
    visual_count = len({v['observation_id'] for e in entries for v in e.get('visual_observations', [])})
    runs.append_message(c, run, 'assistant', 'Извлечение прервалось. Сохранено фактов: ' + str(len(refs))
                        + ', наблюдений по изображениям: ' + str(visual_count)
                        + '. Записка не сформирована. Повторного запроса к модели не было.')
