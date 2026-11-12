"""Неизменяемое содержание, общая проверка редактора/инструмента и принятие."""
from copy import deepcopy
from sqlalchemy.exc import SQLAlchemyError
from .contracts import validate_definition
from .normalization import checked, fingerprint
from .service import ident, row, timestamp
from .runs import execute, json_value, now
from .errors import ApiError
from .domain import refuse


def view(c, saved):
    result = deepcopy(saved['view'])
    accepted = row(c, 'SELECT data FROM aist.version_acceptances WHERE version_id=:v', v=saved['id'])
    if accepted:
        result['review']['acceptance'] = accepted['data']
        result['review']['revision'] += 1
    content = result['version']['payload']['content']
    if 'sections' in content:
        from .aso import reading_paragraph
        omitted = [block['block_id'] for section in content['sections'] for block in section['blocks']
                   if block['kind'] == 'paragraph' and not reading_paragraph(block['claim'])[0]]
        if omitted:
            result['omitted_note_blocks'] = omitted
    return validate_definition('VersionView', result)


def parent_version(runs, c, actor, project, artifact, parent, expected):
    head = runs.service.scoped(c, actor, project, 'artifacts', artifact, lock=True)
    saved = runs.service.scoped(c, actor, project, 'artifact_versions', parent)
    if saved['artifact_id'] != artifact: raise ApiError('resource_not_found')
    if head['revision'] != expected: raise ApiError('revision_conflict', expected, head['revision'])
    return head, saved['data'], saved['view']['checks']


def editorial_payload(payload, parent):
    from .review_candidates import inherited_links
    value = deepcopy(payload)
    candidates = inherited_links(parent)
    previous = {b['block_id']: b for s in parent['payload']['content']['sections'] for b in s['blocks']}
    for section in value['content']['sections']:
        for block in section['blocks']:
            if (block['kind']=='paragraph' and block['claim'].get('origin')!='public_news'
                    and block['claim'] != previous.get(block['block_id'], {}).get('claim')):
                block['claim']['origin'] = ('source_candidate' if block['claim'].get('review_candidate_ids')
                                            or block['block_id'] in candidates else 'human_editorial')
                # Измененный пользователем текст уже не является утверждением из старого запроса.
                # Его исходное основание остается в неизменяемой родительской версии.
                block['claim'].pop('request_message_ids', None)
    return value


TBD = '[TBD]'


def note_team(content, old):
    """Отсутствующая команда наследуется или получает [TBD]; содержание проверяет человек."""
    previous = old['payload']['content'].get('team') if old else None
    content.setdefault('team', previous or TBD)


def check_edited_rows(payload, parent):
    previous = {(block['block_id'], item['row_id']): item
                for section in parent['content']['sections'] for block in section['blocks']
                for item in block.get('rows', [])}
    for section in payload['content']['sections']:
        for block in section['blocks']:
            for item in block.get('rows', []):
                original = previous.get((block['block_id'], item['row_id']))
                if original is None or any(item.get(key, []) != original.get(key, []) for key in
                                           ('fact_refs', 'calculation_ids', 'visual_observation_ids')):
                    refuse('Правка ячейки должна сохранить исходную строку и её основания.')


def review(runs, c, context, payload, old=None, human=False, parent_checks=None):
    """Общий путь проверки редактора и модели: основания, все замечания, нормализация подписей.

    errors отклоняют сохранение, findings сохраняются у версии; severity=error блокирует принятие."""
    from .tools import resolve_basis
    if human:
        payload = editorial_payload(payload, old)
    else:
        # Нормализация подписей меняет сохраняемое содержание, не аргументы инструмента и их отпечаток.
        payload = deepcopy(payload)
    for section in payload['content'].get('sections', []):
        for block in section['blocks']:
            if block['kind'] != 'paragraph':
                continue
            end = 0
            for mark in block.get('marks', []):
                if not end <= mark['start'] < mark['end'] <= len(block['claim']['text']):
                    refuse('Выделение должно находиться внутри абзаца и не пересекаться с соседним.')
                end = mark['end']
    findings = []
    errors = []
    basis = resolve_basis(runs,c,context,payload,parent=old,human=human,findings=findings,errors=errors,
                          parent_checks=parent_checks)
    if payload['result_type']=='analytical_note':
        from .note_standard import financial_table, shared_unit_once, translate_row_labels
        previous = {block['block_id']: block for section in old['payload']['content']['sections']
                    for block in section['blocks']} if old else {}
        normalize = not human and context['input'].get('skill_version_id')=='skill_analytical-note-v3'
        # Значение команды по умолчанию задается всем запискам с этим полем; таблицы нормализуются только для v3.
        # Записка другого навыка без этого поля и без команды родителя сохраняет прежнюю форму.
        has_team = 'team' in payload['content'] or (old is not None and 'team' in old['payload']['content'])
        if normalize or has_team:
            note_team(payload['content'], old)
        facts = {(fact['source']['source_version_id'], fact['fact_id']): fact for fact in basis['facts']}
        for section in payload['content']['sections']:
            for block in section['blocks']:
                if block['kind']!='table':
                    continue
                if normalize and block!=previous.get(block['block_id']):
                    translate_row_labels(block)
                    shared_unit_once(block)
                # Каждая версия проверяет свое содержание, включая неизмененные таблицы.
                # Таблица с отсутствующим основанием уже получила замечание; проверка матрицы зависима.
                if all((ref['source_version_id'], ref['fact_id']) in facts for row in block['rows'] for ref in row['fact_refs']):
                    financial_table(block, facts, findings=findings,
                                    calculations={item['calculation_id']: item for item in basis['calculations']},
                                    observations={item['observation_id']: item for item in basis.get('visual_observations', [])})
        findings.extend(edited_calculation_findings(payload, basis))
        if normalize and old is None and not errors:
            from .aso import organize_sections, omit_editorial_boilerplate
            if omit_editorial_boilerplate(payload['content']):
                # Check the final basis again. Omitted boilerplate cannot supply a note's only source.
                return review(runs, c, context, payload)
            organize_sections(payload['content'], context['input'].get('parameters', {}).get('sections', []))
    return payload, basis, findings, errors


def edited_calculation_findings(payload, basis):
    """Ручное значение не меняет Fact. Зависимый расчет остается по старому входу."""
    facts = {(f['source']['source_version_id'], f['fact_id']): f for f in basis['facts']}
    blocks = [b for s in payload['content']['sections'] for b in s['blocks']]
    changed = set()
    for block in blocks:
        for row in block.get('rows', []):
            for ref in row['fact_refs']:
                key = (ref['source_version_id'], ref['fact_id'])
                if key in facts and row['amount'] != facts[key]['amount']:
                    changed.add(key)
    affected = {calc['calculation_id'] for calc in basis['calculations']
                if any((i['fact']['source_version_id'], i['fact']['fact_id']) in changed for i in calc['inputs'])}
    findings = []
    for block in blocks:
        items = block['rows'] if block['kind'] == 'table' else [block['claim']]
        for item in items:
            if affected.intersection(item['calculation_ids']):
                finding = dict(code='calculation_input_edited', severity='error', block_id=block['block_id'],
                    message='Исходное значение в записке изменено. Расчет использует прежнее значение источника; '
                            'согласуйте расчет с правкой.')
                if 'row_id' in item:
                    finding['row_id'] = item['row_id']
                findings.append(finding)
    return findings


def persist(runs, c, context, payload, artifact=None, parent=None, expected=0, human=False, news_events=None,
            editorial_external_context=None, ownership_review=None):
    from .tools import feedback, block_locations, content_finding
    actor, project = context['owner_id'], context['project_id']
    old = None
    parent_checks = None
    if artifact:
        head, old, parent_checks = parent_version(runs,c,actor,project,artifact,parent,expected)
        if payload['result_type'] != head['result_type']: raise ApiError('unsupported_result_type')
    elif expected != 0 or parent is not None: raise ApiError('revision_conflict',expected,0)
    if human and payload['result_type'] != 'analytical_note': raise ApiError('unsupported_result_type')
    if editorial_external_context is not None:
        # Only a trusted editorial operation supplies newly read public material.
        # Neither HTTP saveVersion nor model arguments accept an arbitrary basis.
        if not human or old is None:
            refuse('Обновление внешних оснований доступно только редакторской дочерней версии.')
        checked('ExternalContext', editorial_external_context)
        retained = {item['evidence_id']: item for item in editorial_external_context['items']}
        for item in old['basis'].get('external_context', {}).get('items', []):
            if retained.get(item['evidence_id']) != item:
                refuse('Редакторская подборка не может переписать прежнее внешнее основание.')
        context = dict(context, editorial_external_context=deepcopy(editorial_external_context))
    if news_events is not None:
        if payload['result_type'] != 'analytical_note': raise ApiError('unsupported_result_type')
        from .note_news import replace_selection
        payload, context = replace_selection(payload, context, old, news_events)
    payload, basis, findings, errors = review(runs, c, context, payload, old, human, parent_checks)
    if payload['result_type'] == 'analytical_note':
        if not human and old is None:
            from .collection import require_note_topics
            require_note_topics(context, basis)
        from .coverage import findings as coverage_findings
        # Незакрытые наблюдения сохраняют предупреждение о возможной неполноте материала.
        findings.extend(coverage_findings(c, context, old))
    blocking = [finding for finding in findings if finding['severity']=='error']
    if errors or (blocking and payload['result_type'] != 'analytical_note'):
        # Отказ остается за поврежденной ссылкой, подделкой происхождения и структурой.
        # Структурно допустимая записка с замечаниями содержания сохраняется
        # непринятой версией: замечания входят в ее проверку и блокируют принятие.
        # Короткий ответ по-прежнему не сохраняется с блокирующими замечаниями.
        refuse(feedback(errors, blocking, block_locations(payload)))
    from .facts import source_documents, comparison_basis
    basis['sources']=source_documents(runs.service,c,context)
    if payload['result_type']=='analytical_note':
        # resolve_basis включает факты расчетов. Сравнительные наблюдения,
        # добавляемые ниже, сами по себе не доказывают использование PDF.
        used={f['source']['source_version_id'] for f in [*basis['facts'], *basis.get('visual_observations', [])]}
        used.update(item['review_candidate']['source']['source_version_id']
                    for item in basis.get('review_candidates', []))
        for document in basis['sources']:
            if document['source']['source_version_id'] not in used:
                findings.append(content_finding('unused_selected_source', None, None,
                    f'Не использован выбранный документ «{document["original_filename"]}». '
                    'Добавьте действительное основание из этого документа.'))
    if len(context['input']['source_versions'])>1:
        basis['facts'],basis['comparisons']=comparison_basis(runs.service,c,context,basis['facts'])
    if payload['result_type']=='evidence_answer' and not payload['content']['claims'] and not payload['content']['missing_information']:
        refuse('Ответ не содержит утверждений или объяснения недостающих сведений.')
    from .note_news import include
    include(payload,basis,context,old)
    if payload['result_type'] == 'analytical_note':
        from .note_external import include as include_external
        include_external(basis, context, old)
        if human and old:
            from .note_external import retain_ownership_review
            retain_ownership_review(payload, basis, old)
        elif not human:
            from .note_external import review_ownership
            review_ownership(payload, basis, context, old, ownership_review)
    version, created = ident('av'), now()
    sha = fingerprint({'payload':payload,'basis':basis})
    artifact_id = artifact or ident('artifact')
    provenance = dict(origin='human_edit' if human else 'model_edit' if old else 'generation',
        source_versions=context['input']['source_versions'], run_id=None if human else context['id'],
        skill_version_id=None if human else context['input']['skill_version_id'],
        model_profile_version_id=None if human else context['input']['model_profile_version_id'],
        actor_id=actor, derived_from_version_id=None)
    value = dict(id=version,project_id=project,artifact_id=artifact_id,revision=expected+1,
        parent_version_id=parent,payload=payload,basis=basis,provenance=provenance,content_sha256=sha,created_at=created)
    check = dict(id=ident('check'),artifact_version_id=version,content_sha256=sha,
        profile_version=('note-technical.v29' if payload['result_type'] == 'analytical_note' else
                         'external-materials.v28' if basis.get('external_context', {}).get('collection_version') else
                         'external-context.v27' if 'external_context' in basis else 'template01r2-references-numbers.v26'),kind='programmatic',
        status='failed' if any(f['severity']=='error' for f in findings) else 'passed',findings=findings,
        created_at=created,method='deterministic',model_call_id=None)
    semantic = dict(check,id=ident('check'),kind='semantic',status='not_checked',method='not_performed',findings=[])
    result = validate_definition('VersionView',dict(version=value,checks=[check,semantic],review=dict(
        artifact_version_id=version,revision=0,validation_ids=[check['id'],semantic['id']],acceptance=None)))
    if artifact:
        execute(c,'UPDATE aist.artifacts SET revision=:rev,latest_version_id=:v WHERE id=:id',rev=expected+1,v=version,id=artifact)
    else:
        execute(c,'''INSERT INTO aist.artifacts(id,project_id,owner_id,result_type,latest_version_id,revision)
            VALUES (:id,:p,:a,:kind,:v,1)''',id=artifact_id,p=project,a=actor,kind=payload['result_type'],v=version)
    execute(c,'''INSERT INTO aist.artifact_versions(id,artifact_id,project_id,owner_id,revision,data,view)
        VALUES (:v,:id,:p,:a,:rev,CAST(:d AS jsonb),CAST(:view AS jsonb))''',
        v=version,id=artifact_id,p=project,a=actor,rev=expected+1,d=json_value(value),view=json_value(result))
    return result


def receipt_view(runs,c,receipt):
    saved = runs.service.scoped(c,receipt['actor_id'],receipt['project_id'],'artifact_versions',receipt['artifact_version_id'])
    result = view(c,saved)
    return result['review'] if receipt['operation']=='acceptVersion' else result


def write(runs,actor,session,project,artifact,version,operation,key,body):
    checked('AcceptVersion' if version else 'SaveVersion',body)
    scope=dict(actor=actor,project_scope=project,operation=operation,target=version or artifact,key=key)
    sha=fingerprint(body)
    c=runs.engine.connect();tx=c.begin();committing=False
    try:
        runs.service.scoped(c,actor,project,lock=True)
        head=runs.service.scoped(c,actor,project,'artifacts',artifact,lock=True)
        runs.session_alive(c,dict(session_id=session,owner_id=actor))
        saved=None
        if version:
            saved=runs.service.scoped(c,actor,project,'artifact_versions',version)
            if saved['artifact_id']!=artifact:raise ApiError('resource_not_found')
        old=runs.service.receipt(c,scope)
        if old:
            if old['fingerprint']!=sha:raise ApiError('idempotency_conflict')
            result=receipt_view(runs,c,old);tx.commit();return 200,result
        response_status = 200 if version else 201
        if version:
            result=view(c,saved);review=result['review']
            if body['content_sha256']!=saved['data']['content_sha256']:refuse('SHA не соответствует выбранной версии.')
            if not review['acceptance']:
                if body['expected_revision']!=review['revision']:raise ApiError('revision_conflict',body['expected_revision'],review['revision'])
                checks=result['checks']
                if any(x['status']=='failed' or any(f['severity']=='error' for f in x['findings']) for x in checks):
                    refuse('Ошибки проверок блокируют принятие этой версии.')
                if any(x['kind']=='semantic' and x['status']=='not_checked' for x in checks) and not body['acknowledge_unchecked_semantics']:
                    refuse('Подтвердите принятие без смысловой проверки.')
                acceptance=checked('Acceptance',dict(id=ident('accept'),artifact_version_id=version,
                    content_sha256=body['content_sha256'],actor_id=actor,validation_ids=review['validation_ids'],
                    created_at=now(),acknowledge_unchecked_semantics=body['acknowledge_unchecked_semantics']))
                execute(c,'''INSERT INTO aist.version_acceptances(id,version_id,project_id,owner_id,data)
                    VALUES (:id,:v,:p,:a,CAST(:d AS jsonb))''',id=acceptance['id'],v=version,p=project,a=actor,d=json_value(acceptance))
        else:
            latest = body.get('require_latest', False)
            if latest and head['latest_version_id'] != body['parent_version_id']:
                raise ApiError('revision_conflict',body['expected_revision'],head['revision'])
            _,parent,_=parent_version(runs,c,actor,project,artifact,body['parent_version_id'],body['expected_revision'])
            context=dict(id=None,owner_id=actor,project_id=project,input=dict(source_versions=parent['provenance']['source_versions']))
            if latest and parent['payload']['result_type'] != 'analytical_note':
                raise ApiError('unsupported_result_type')
            if latest and body['payload']['result_type'] != parent['payload']['result_type']:
                raise ApiError('unsupported_result_type')
            if latest:
                check_edited_rows(body['payload'], parent['payload'])
            if latest and body['payload'] == parent['payload']:
                version = parent['id']
                response_status = 200
            else:
                result=persist(runs,c,context,body['payload'],artifact,parent['id'],body['expected_revision'],human=True)
                version=result['version']['id']
        execute(c,'''INSERT INTO aist.receipts(actor_id,project_scope,operation,target,key,fingerprint,project_id,artifact_version_id)
            VALUES (:actor,:project_scope,:operation,:target,:key,:sha,:project_scope,:v)''',**scope,sha=sha,v=version)
        result=receipt_view(runs,c,runs.service.receipt(c,scope))
        committing=True;tx.commit()
        return response_status,result
    except BaseException:
        if not committing:tx.rollback();raise
        c.close()
        try:
            with runs.engine.begin() as fresh:
                runs.service.scoped(fresh,actor,project,lock=True)
                old=runs.service.receipt(fresh,scope)
                if old and old['fingerprint']==sha:return 200,receipt_view(runs,fresh,old)
        except SQLAlchemyError:pass
        raise ApiError('outcome_unknown') from None
    finally:c.close()


def artifact_view(runs,actor,project,artifact):
    with runs.engine.connect() as c:
        a=runs.service.scoped(c,actor,project,'artifacts',artifact)
        return checked('Artifact',{k:timestamp(a[k]) if k=='created_at' else a[k] for k in ('id','project_id','result_type','latest_version_id','revision','created_at')})


def listing(runs,actor,project,artifact,q):
    with runs.engine.connect() as c:
        runs.service.scoped(c,actor,project,'artifacts',artifact)
        after=0
        if q.get('after_id'):
            cursor=row(c,'SELECT revision FROM aist.artifact_versions WHERE id=:id AND artifact_id=:a',id=q['after_id'],a=artifact)
            if not cursor:raise ApiError('cursor_invalid')
            after=cursor['revision']
        limit=q.get('limit',50)
        items=[x[0] for x in execute(c,'SELECT data FROM aist.artifact_versions WHERE artifact_id=:a AND revision>:r ORDER BY revision LIMIT :lim',a=artifact,r=after,lim=limit+1)]
        return checked('VersionPage',dict(items=items[:limit],next_after_id=items[limit-1]['id'] if len(items)>limit else None))
