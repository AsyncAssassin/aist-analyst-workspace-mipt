"""Сохранить номер страницы PDF, даже когда на ней только изображение."""

from alembic import op

revision = '0019_pdf_visual_pages'
down_revision = '0018_note_news'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE aist.source_pages DROP CONSTRAINT source_pages_text_check, '
               'ADD CONSTRAINT source_pages_text_check CHECK (length(text) <= 200000)')


def downgrade():
    # PostgreSQL refuses this downgrade if it would invalidate an existing scan.
    op.execute('ALTER TABLE aist.source_pages DROP CONSTRAINT source_pages_text_check, '
               'ADD CONSTRAINT source_pages_text_check CHECK (length(text) BETWEEN 1 AND 200000)')
