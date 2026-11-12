"""Add proposal decisions to the existing immutable operation receipts."""

from alembic import op

revision = '0024_proposal_receipts'
down_revision = '0023_web_objective'
branch_labels = depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE aist.edit_proposals ADD UNIQUE (id, project_id, owner_id);
        ALTER TABLE aist.receipts ADD COLUMN proposal_id text,
          ADD FOREIGN KEY (proposal_id, project_id, actor_id)
            REFERENCES aist.edit_proposals (id, project_id, owner_id);
    """)
    op.execute("""DO $$ DECLARE old_check text; BEGIN
      SELECT pg_get_constraintdef(oid) INTO STRICT old_check FROM pg_constraint
        WHERE conrelid='aist.receipts'::regclass AND conname='receipts_check';
      ALTER TABLE aist.receipts DROP CONSTRAINT receipts_check;
      EXECUTE 'ALTER TABLE aist.receipts ADD CONSTRAINT receipts_check CHECK (('
        || substring(old_check from 8 for length(old_check)-8)
        || ') AND proposal_id IS NULL OR (operation=''decideEdit'' AND project_scope=project_id'
        || ' AND proposal_id IS NOT NULL AND target=proposal_id AND conversation_id IS NULL'
        || ' AND source_id IS NULL AND version_id IS NULL AND artifact_version_id IS NULL AND export_id IS NULL))';
    END $$;""")


def downgrade():
    raise RuntimeError('Preserve proposal decision receipts before downgrading.')
