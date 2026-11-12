"""Резервы веб-запросов; завершенные ToolCall остаются неизменяемыми."""
from alembic import op

revision = '0022_web_requests'
down_revision = '0021_external_context'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE aist.web_requests (
        id text PRIMARY KEY,
        run_id text NOT NULL REFERENCES aist.runs(id),
        epoch bigint NOT NULL,
        executor_tool_call_id text NOT NULL,
        arguments_sha256 text NOT NULL,
        url text,
        state text NOT NULL CHECK (state IN ('reserved', 'completed')),
        UNIQUE (run_id, executor_tool_call_id),
        UNIQUE (run_id, arguments_sha256)
    )''')


def downgrade():
    raise RuntimeError('Preserve web request reservations before downgrading.')
