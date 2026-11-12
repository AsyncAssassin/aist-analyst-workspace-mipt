"""Сохранить название, выбранное пользователем, независимо от результата генерации."""
from alembic import op

revision = '0020_chat_title'
down_revision = '0019_pdf_visual_pages'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE aist.conversations ADD title_override text '
               'CHECK (length(title_override) BETWEEN 1 AND 300)')


def downgrade():
    raise RuntimeError('Preserve manually named chats before downgrading.')
