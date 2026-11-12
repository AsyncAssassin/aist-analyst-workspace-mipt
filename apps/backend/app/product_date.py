"""Календарный день AIST для часового момента."""

from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')


def moscow_date(moment):
    return moment.astimezone(MOSCOW).date()


def run_as_of(run):
    # An opened research keeps its saved cutoff, including after an upgrade.
    saved = run.get('external_context') or run.get('news_run')
    return saved['as_of'] if saved else moscow_date(run['created_at']).isoformat()
