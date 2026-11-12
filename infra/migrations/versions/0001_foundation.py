"""Изолированная схема приложения, без предметных таблиц D06/D07."""
from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA aist")


def downgrade():
    op.execute("DROP SCHEMA aist")
