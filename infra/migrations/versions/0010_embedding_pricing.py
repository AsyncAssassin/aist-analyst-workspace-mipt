"""Keep call pricing independent of embedding/index compatibility."""
from alembic import op

revision = '0010_embedding_pricing'
down_revision = '0009_knowledge_search'
branch_labels = depends_on = None


def upgrade():
    op.execute('''
    ALTER TABLE aist.embedding_calls ADD COLUMN pricing jsonb;
    UPDATE aist.embedding_calls AS calls
    SET pricing = jsonb_build_object(
        'price_usd_per_million_input', indexes.configuration->'price_usd_per_million_input',
        'pricing_checked_on', indexes.configuration->'pricing_checked_on')
    FROM aist.embedding_indexes AS indexes
    WHERE calls.index_id = indexes.id
      AND indexes.configuration ? 'price_usd_per_million_input'
      AND indexes.configuration ? 'pricing_checked_on';
    ''')


def downgrade():
    op.execute('ALTER TABLE aist.embedding_calls DROP COLUMN pricing')
