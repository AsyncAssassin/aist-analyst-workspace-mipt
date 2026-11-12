"""Forecast context from the full page layout, top-down order inside one logical column, and the declared overload backoff."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from types import SimpleNamespace

import pytest

from app.accounting import overload_refusal, retry_pause
from app.extraction import candidate
from app.live_profile import PROFILE
from app.openclaw_runtime import client_timeout, prepare
from app.source_cards import numeric_cards
from app.transport import retry_after_seconds
from test_d10 import PIN
from test_perf02_r1 import HEADER, qualitative


def observed(lines, layout=None, line_start=1):
    """Facts made from the numeric cards of the cited range; the qualitative citation itself always stays valid."""
    text = '\n'.join([HEADER, *lines])
    pages, layouts = {1: text}, {1: text if layout is None else layout}
    item = qualitative(len(lines) + 1)
    item['evidence'][0]['line_start'] = line_start
    quote, basis = candidate(PIN, pages, item, layouts)
    assert quote['amount'] is None and quote['nature'] == 'source_statement' and basis == {}
    facts = [candidate(PIN, pages, card, layouts)[0] for card in numeric_cards(pages, layouts, item)]
    return {(f['metric'], f['amount']['value'], f['period']['kind'], f['period']['end']) for f in facts}


@pytest.mark.parametrize('lines,line_start', [
    (['Прогноз на 2025 год', 'Собственный капитал', '100 млн ₽', '2025 г.'], 3),
    (['Прогноз', '', 'Собственный капитал', '100 млн ₽', '2025 г.'], 1),
    (['Прогноз', '', 'Собственный капитал', '100 млн ₽', '2025 г.'], 4),
    (['Прогноз на 2025 год', 'Основные показатели', 'Собственный капитал', '100 млн ₽', '2025 г.'], 4),
    (['Прогноз', '', 'Выручка', '100 млн ₽'], 1),
    (['Ожидаемые показатели', 'Выручка', '100 млн ₽'], 3),
    (['Прогноз', '120млн ₽ / мес.', 'Текущий объем', '2025'], 1),
], ids=['heading-outside-quote', 'bare-word-gap-full-quote', 'bare-word-gap-outside-quote', 'heading-over-caption-block',
        'card-with-page-date', 'card-heading-outside-quote', 'monthly-card'])
def test_forecast_context_is_read_from_the_page_not_from_the_citation(lines, line_start):
    assert observed(lines, line_start=line_start) == set()


CAPITAL = {('Собственный капитал', '100', 'instant', '2024-12-31'), ('Собственный капитал', '80', 'instant', '2023-12-31')}


def test_forecast_heading_does_not_reach_another_block_or_cross_a_wide_gap():
    # A forecast heading below a factual series qualifies only the series under it.
    lines = ['Собственный капитал', '100 млн ₽', '2024 г.', '80 млн ₽', '2023 г.', 'Прогноз на 2025 год', 'Выручка', '120 млн ₽', 'за 2025 год']
    assert observed(lines) == CAPITAL
    assert observed(lines, line_start=8) == set()
    # More than the short gap allowed before a caption separates the heading from the series.
    assert observed(['Прогноз на 2025 год', '', '', '', 'Собственный капитал', '100 млн ₽', '2025 г.']) == {
        ('Собственный капитал', '100', 'instant', '2025-12-31')}
    # A caption of another card above the gap is not a forecast heading.
    assert observed(['Ожидаемые показатели', 'Собственный капитал', '', 'Выручка', '100 млн ₽', 'за 2025 год'], line_start=5) == {
        ('Выручка', '100', 'interval', '2025-12-31')}
    assert observed(['120млн ₽ / мес.', 'Текущий объем', '2025']) == {('Текущий объем', '120', 'interval', '2025-12-31')}


@pytest.mark.parametrize('layout', [
    ' Собственный капитал\n 100 млн ₽\n 2024 г.\n80 млн ₽\n2023 г.',
    'Собственный капитал\n100 млн ₽\n2024 г.\n   80 млн ₽\n   2023 г.',
    '   Собственный капитал\n   100 млн ₽\n   2024 г.\n80 млн ₽\n2023 г.',
], ids=['top-shifted-right', 'bottom-shifted-right', 'three-space-shift'])
def test_series_keeps_top_down_order_inside_one_logical_column(layout):
    assert observed(['Собственный капитал', '100 млн ₽', '2024 г.', '80 млн ₽', '2023 г.'], layout) == CAPITAL


def test_series_in_another_column_does_not_share_the_caption():
    layout = 'Собственный капитал\n100 млн ₽\n2024 г.\n    80 млн ₽\n    2023 г.'
    assert observed(['Собственный капитал', '100 млн ₽', '2024 г.', '80 млн ₽', '2023 г.'], layout) == {
        ('Собственный капитал', '100', 'instant', '2024-12-31')}
    layout = ' Прогноз на 2025 год\n Собственный капитал\n 100 млн ₽\n 2025 г.\n90 млн ₽\n2024 г.'
    assert observed(['Прогноз на 2025 год', 'Собственный капитал', '100 млн ₽', '2025 г.', '90 млн ₽', '2024 г.'], layout, line_start=3) == set()


def test_profile_declares_only_the_overload_retry():
    assert PROFILE['automatic_retries'] == dict(http_status=429, error_type='server_overload', max_per_run=3, base_seconds=[10, 20, 40],
                                                jitter_ratio=0.2, max_total_wait_seconds=180, honor_retry_after=True)


@pytest.mark.parametrize('status,payload,expected', [
    (429, {'type': 'server_overload', 'message': 'server overload, please try again later'}, True),
    (429, {'error': {'type': 'server_overload', 'message': 'overload'}}, True),
    (429, {'type': 'rate_limit_exceeded'}, False),
    (429, {'error': {'code': 429, 'message': 'too many requests'}}, False),
    (429, {'type': 'server_overload', 'choices': []}, False),
    (429, None, False),
    (429, 'server_overload', False),
    (503, {'type': 'server_overload'}, False),
    (200, {'type': 'server_overload'}, False),
])
def test_only_a_complete_typed_overload_refusal_qualifies(status, payload, expected):
    assert overload_refusal(status, payload) is expected


def test_backoff_schedule_with_jitter_and_retry_after():
    assert [retry_pause(used, None, 0) for used in range(3)] == [10, 20, 40]
    assert retry_pause(0, None, 1) == pytest.approx(12) and retry_pause(2, None, 0.5) == pytest.approx(44)
    assert retry_pause(0, 30, 0.5) == 30 and retry_pause(2, 30, 0) == 40
    assert retry_pause(3, None, 0) is None


def test_retry_after_header_forms():
    assert retry_after_seconds('30') == 30.0 and retry_after_seconds(' 7 ') == 7.0
    assert retry_after_seconds(None) is None and retry_after_seconds('soon') is None and retry_after_seconds('-5') is None
    later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    assert 85 <= retry_after_seconds(later) <= 90
    assert retry_after_seconds(format_datetime(datetime.now(timezone.utc) - timedelta(seconds=90), usegmt=True)) == 0.0


def test_openclaw_outwaits_granted_retries_within_the_run_deadline(tmp_path):
    settings = SimpleNamespace(provider_port=30314, gateway_port=30313, transport_seconds=1200)
    assert client_timeout(settings, 3600) == 3600
    assert client_timeout(SimpleNamespace(transport_seconds=20), 3600) == 20 * 4 + 180
    prepare(settings, tmp_path, 'synthetic-token', 'skill_analytical-note-v3', 'analytical_note', timeout_seconds=3600)
    config = json.loads((tmp_path / 'state/openclaw.json').read_text())
    assert config['models']['providers']['novita-aist']['timeoutSeconds'] == 3600
    assert config['agents']['defaults']['timeoutSeconds'] == 3600
