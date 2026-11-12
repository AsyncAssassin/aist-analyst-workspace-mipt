"""Сохраняем геометрические связи строк отдельно от исходного текста PDF."""
from alembic import op

revision = '0013_pdf_diagrams'
down_revision = '0012_conversation_reply'
branch_labels = depends_on = None


def upgrade():
    op.execute('''ALTER TABLE aist.source_pages ADD COLUMN diagram_regions jsonb NOT NULL DEFAULT '[]'
      CHECK (jsonb_typeof(diagram_regions)='array' AND jsonb_array_length(diagram_regions)<=64
        AND length(diagram_regions::text)<=100000)''')


def downgrade():
    op.execute('ALTER TABLE aist.source_pages DROP COLUMN diagram_regions')
