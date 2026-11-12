"""Заморозить результат поиска для запуска до выпуска первой записки."""

from alembic import op

revision = '0018_note_news'
down_revision = '0017_edit_proposals'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE aist.runs ADD COLUMN news_run jsonb')


def downgrade():
    raise RuntimeError('Сохранённые основания News Run принадлежат версии записки.')
