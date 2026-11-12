"""Вопрос извлечения отделяет выборки одного URL, прежние записи не переименовываются."""
from alembic import op

revision = '0023_web_objective'
down_revision = '0022_web_requests'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE aist.web_requests ADD COLUMN objective text')


def downgrade():
    raise RuntimeError('Preserve extraction objectives before downgrading.')
