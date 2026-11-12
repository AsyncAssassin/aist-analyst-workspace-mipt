"""Расположение текста для проверки строк и столбцов финансовых таблиц."""
from alembic import op

revision = '0008_page_layout'
down_revision = '0007_exports'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE aist.source_pages ADD COLUMN layout_text text CHECK (length(layout_text)<=200000)')


def downgrade():
    op.execute('ALTER TABLE aist.source_pages DROP COLUMN layout_text')
