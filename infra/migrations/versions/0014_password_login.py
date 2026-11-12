"""Операторские учетные записи поверх существующих Bearer-сессий."""
from alembic import op

revision = '0014_password_login'
down_revision = '0013_pdf_diagrams'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''ALTER TABLE aist.users
      ADD COLUMN login text UNIQUE CHECK (login ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
      ADD COLUMN password_hash text,
      ADD COLUMN access_expires_at timestamptz,
      ADD COLUMN disabled_at timestamptz,
      ADD CONSTRAINT password_account_complete CHECK
        ((login IS NULL AND password_hash IS NULL) OR
         (login IS NOT NULL AND password_hash IS NOT NULL AND access_expires_at IS NOT NULL));''')


def downgrade():
    op.execute('''ALTER TABLE aist.users DROP CONSTRAINT password_account_complete,
      DROP COLUMN login, DROP COLUMN password_hash, DROP COLUMN access_expires_at,
      DROP COLUMN disabled_at''')
