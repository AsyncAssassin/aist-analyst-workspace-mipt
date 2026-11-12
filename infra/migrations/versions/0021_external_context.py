"""Неизменяемый снимок публичного контекста нового Run."""
from alembic import op

revision = '0021_external_context'
down_revision = '0020_chat_title'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE aist.runs ADD COLUMN external_context jsonb')


def downgrade():
    raise RuntimeError('Preserve external evidence before downgrading.')
