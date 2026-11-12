"""Отдельное принятие неизменяемой версии и расширение существующих receipts."""
from alembic import op

revision = '0006_editor_versions'
down_revision = '0005_domain_facts'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
    CREATE TABLE aist.version_acceptances (
      id text PRIMARY KEY, version_id text NOT NULL UNIQUE, project_id text NOT NULL,
      owner_id text NOT NULL, data jsonb NOT NULL,
      FOREIGN KEY(version_id,project_id,owner_id)
        REFERENCES aist.artifact_versions(id,project_id,owner_id));
    CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON aist.version_acceptances
      FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable();
    ALTER TABLE aist.receipts ADD COLUMN artifact_version_id text;
    ALTER TABLE aist.receipts ADD FOREIGN KEY(artifact_version_id,project_id,actor_id)
      REFERENCES aist.artifact_versions(id,project_id,owner_id);
    ''')
    # Сохраняем прежние ограничения операций и добавляем только две новые.
    op.execute("""DO $$ DECLARE old_check text; BEGIN
      SELECT pg_get_constraintdef(oid) INTO old_check FROM pg_constraint
        WHERE conrelid='aist.receipts'::regclass AND conname='receipts_check';
      ALTER TABLE aist.receipts DROP CONSTRAINT receipts_check;
      EXECUTE 'ALTER TABLE aist.receipts ADD CONSTRAINT receipts_check CHECK (('
        || substring(old_check from 8 for length(old_check)-8)
        || ') AND artifact_version_id IS NULL OR (operation IN (''saveVersion'',''acceptVersion'')'
        || ' AND project_scope=project_id AND target<>'''' AND artifact_version_id IS NOT NULL'
        || ' AND source_id IS NULL AND version_id IS NULL AND conversation_id IS NULL))';
    END $$;""")


def downgrade():
    raise RuntimeError('Сначала требуется сохранить версии и решения о принятии')
