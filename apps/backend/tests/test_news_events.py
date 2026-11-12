"""Явные события и сохранность optional аргументов реального Boundary без внешней модели."""
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import note_external, note_news, openclaw_runtime as runtime
from app.errors import ApiError
from app.external_sources import snapshot
from app.live_profile import FABLE_VISUAL_ID
from app.model_save import content_args, failed_command_digest, save_operation
from app.normalization import checked
from test_perf02 import content, run


def evidence_context():
    context = snapshot('Орбита', '2026-11-09')
    context['news_search'] = dict(succeeded=1, failed=0)
    context['items'] = [dict(evidence_id='ext_event', url='https://company.example/report', publisher='Компания',
        title='Об отчете', published='2026-02-24', retrieved_at='2026-11-08T00:00:00Z',
        page_sha256='a'*64, quote='1 августа 2025. Руководитель назначен 25 июля 2025 года. Отчет раскрывает также открытие нового предприятия 12 июня 2025 года.',
        topics=['news'], search_target='Орбита', search_role='company', source_kind='company')]
    return context


def event(**changes):
    return dict(dict(external_evidence_id='ext_event', description='Назначен новый руководитель компании.',
        published='2025-08-01', event_date='2025-07-25',
        date_quote='1 августа 2025. Руководитель назначен 25 июля 2025 года.'), **changes)


def through_boundary(current, command, tmp_path):
    """Подменен только транспорт и учетное окружение, проверяемый Boundary исполняется целиком."""
    native = current['input']['model_profile_version_id'] == FABLE_VISUAL_ID
    payload = {'content': [dict(type='tool_use', id='issued-save', name='aist_save_artifact', input=command)]} if native else {
        'choices': [{'message': {'tool_calls': [dict(id='issued-save', function=dict(name='aist_save_artifact', arguments=json.dumps(command)))]}}]}
    original = deepcopy(payload)
    service = SimpleNamespace(engine=SimpleNamespace(begin=lambda: nullcontext(None)), locked=lambda *_: current, fence=lambda *_: None)
    worker = SimpleNamespace(runs=service, transport=SimpleNamespace(perform=lambda *a, **kw: ('model-stub', payload)))
    boundary = runtime.Boundary(worker, current, tmp_path, 'synthetic-capability')
    with patch.object(runtime, 'attempt_counts', return_value=(0, 0)), patch.object(runtime, 'service_context', return_value='synthetic'), patch.object(runtime.collection, 'context', return_value='synthetic'):
        returned = boundary.model(dict(model=boundary.profile['model'], messages=[]))
    assert payload == original
    if native:
        assert returned == original  # Signed transcript is untouched.
    issued = boundary.issued['issued-save']['args']
    return save_operation(current, issued)


@pytest.mark.parametrize('profile', ['model_novita-mimo-v1', FABLE_VISUAL_ID])
@pytest.mark.parametrize('shape', ['minimal', 'legacy', 'absent'])
def test_optional_save_fields_roundtrip_real_boundary(profile, shape, tmp_path):
    current = dict(run(), epoch=1, executor_session_id='synthetic-session', configuration={})
    current['input'].update(model_profile_version_id=profile, budget={'max_model_calls': 16})
    command = dict(content=content())
    if shape != 'absent':
        command.update(questions=['Какие ограничения финансирования не раскрыты?'], news_events=[event()],
                       ownership_review=dict(status='not_established', reason='Владельцы не раскрыты.', excluded_sources=[]))
    if shape == 'legacy':
        command = save_operation(current, command)
    saved = through_boundary(current, command, tmp_path)
    for key in ('questions', 'news_events', 'ownership_review'):
        assert (key in saved) == (shape != 'absent')
        if shape != 'absent':
            assert saved[key] == command[key]


def test_events_affect_digest_and_unknown_fields_are_refused():
    base = dict(content=content())
    empty = dict(base, news_events=[])
    selected = dict(base, news_events=[event()])
    assert len({failed_command_digest(x) for x in (base, empty, selected)}) == 3
    with pytest.raises(ApiError):
        content_args(dict(base, arbitrary_basis={}))
    for value in (dict(event(), published='2025-08'), dict(event(), date_quote='')):
        with pytest.raises(ApiError):
            checked('NewsEvent', value)


@pytest.mark.parametrize('metadata', ['2025-08-01', '2026-02-24', None])
def test_selected_event_preserves_metadata_and_text_dates(metadata):
    context = evidence_context()
    context['items'][0]['published'] = metadata
    original = deepcopy(context)
    selected = note_news.select_events(dict(external_context=context), None, [event()])
    item, = selected['items']
    assert item['published'] == '2025-08-01' and item['event_date'] == '2025-07-25'
    assert item['metadata_published'] == metadata
    assert item['retrieved_at'] == context['items'][0]['retrieved_at']
    assert item['external_evidence_id'] == 'ext_event'
    text = note_news.news_text(item)
    assert '01.08.2025' in text and '25.07.2025' in text and 'Об отчете' not in text
    assert '24.02.2026' not in text and 'метаданных' not in text
    assert context == original


def test_unknown_dates_and_two_events_from_one_document_have_distinct_blocks():
    context = evidence_context()
    events = [event(), event(description='Открыто новое предприятие.', event_date='2025-06-12',
                             date_quote='открытие нового предприятия 12 июня 2025 года.')]
    selected = note_news.select_events(dict(external_context=context), None, events)
    assert len({note_news.block_id(item) for item in selected['items']}) == 2
    undated = note_news.select_events(dict(external_context=context), None,
        [event(published=None, event_date=None, date_quote='')])['items'][0]
    assert note_news.news_text(undated) == undated['quote']
    assert 'дата события' not in note_news.news_text(undated)
    for invalid in (event(external_evidence_id='ext_foreign'), event(date_quote='Несуществующая выдержка')):
        with pytest.raises(ApiError):
            note_news.select_events(dict(external_context=context), None, [invalid])
    old = {key: value for key, value in selected['items'][0].items() if key not in ('date_quote', 'metadata_published', 'retrieved_at', 'external_evidence_id')}
    old_id = note_news.block_id(old)
    assert old_id == note_news.block_id(dict(old, quote='Редактор изменил прежний текст'))


def test_document_search_does_not_invent_news_from_title():
    context = evidence_context()
    original = deepcopy(context)
    news = note_external.news_from_context(context)
    assert news['items'] == [] and news['status'] == 'empty'
    assert context == original


@pytest.mark.parametrize('description,published,event_date,expected', [
    ('Склад открыт в июле 2025 года.', None, None, 'Склад открыт в июле 2025 года.'),
    ('25 июля 2025 года открыт склад.', '2025-07-25', '2025-07-25', '25 июля 2025 года открыт склад.'),
    ('Открыт склад.', '2025-08-01', '2025-07-25', '25.07.2025: Открыт склад.\n01.08.2025'),
])
def test_modern_news_dates_do_not_invent_day_or_repeat_known_date(description, published, event_date, expected):
    item = dict(quote=description, published=published, event_date=event_date, publisher='company.example', date_quote='')
    assert note_news.news_text(item) == expected
    assert note_news.matches_news_text(note_news.legacy_news_text(item), item)
    assert not note_news.matches_news_text('Человек отредактировал событие.', item)


def test_modern_news_deduplication_keeps_legacy_text_and_human_edit():
    item = dict(quote='Компания открыла склад.', published='2025-08-01', event_date=None,
                publisher='company.example', url='https://company.example/news', page_sha256='a' * 64)
    snapshot = dict(items=[item])
    for text in (note_news.news_text(item), note_news.legacy_news_text(item), 'Редактор уточнил событие.'):
        block = dict(kind='paragraph', block_id=note_news.block_id(item),
                     claim=dict(text=text, origin='public_news', fact_refs=[], calculation_ids=[]))
        payload = dict(result_type='analytical_note', content=dict(sections=[dict(section_id='news_section', title='News Run', blocks=[block])]))
        before = deepcopy(payload)
        note_news.include(payload, {}, dict(news_run=snapshot), None)
        assert payload == before


@pytest.mark.parametrize('created,day', [
    ('2026-01-31T20:59:59Z', '2026-01-31'),
    ('2026-01-31T21:00:00Z', '2026-02-01'),
    ('2026-12-31T20:59:59Z', '2026-12-31'),
    ('2026-12-31T21:00:00Z', '2027-01-01'),
])
def test_news_uses_moscow_cutoff_for_entire_run(created, day, monkeypatch):
    from datetime import date, datetime, timedelta
    import time

    # Run and direct editorial save happen on different Moscow days, in a non-Moscow process.
    with monkeypatch.context() as patch:
        patch.setenv('TZ', 'Pacific/Honolulu')
        time.tzset()
        try:
            moment = datetime.fromisoformat(created)
            current_day = date.fromisoformat(day) + timedelta(days=1)

            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return (moment + timedelta(days=1)).astimezone(tz)

            patch.setattr(note_news, 'datetime', Clock)
            context = dict(evidence_context(), as_of=day)
            current = dict(external_context=context, created_at=moment)
            assert note_news.select_events(current, None, [event(published=day)])['as_of'] == day
            with pytest.raises(ApiError, match='validation_failed'):
                note_news.select_events(current, None, [event(published=current_day.isoformat())])
            current.pop('created_at')
            assert note_news.select_events(current, None, [event(published=current_day.isoformat())])['as_of'] == current_day.isoformat()
        finally:
            patch.undo()
            time.tzset()


def test_legacy_research_cutoff_survives_continuation():
    from datetime import datetime

    context = dict(evidence_context(), as_of='2026-12-31')
    resumed = dict(external_context=context, created_at=datetime.fromisoformat('2026-12-31T21:00:00Z'))
    assert note_news.select_events(resumed, None, [event(published='2026-12-31')])['as_of'] == '2026-12-31'
    with pytest.raises(ApiError, match='validation_failed'):
        note_news.select_events(resumed, None, [event(published='2027-01-01')])
