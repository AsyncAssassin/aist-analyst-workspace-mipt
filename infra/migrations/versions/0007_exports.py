"""Export identity, durable publication and receipts for saved versions."""
from alembic import op
revision='0007_exports'
down_revision='0006_editor_versions'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''
    CREATE TABLE aist.exports (
      id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL,
      version_id text NOT NULL, identity_sha256 text NOT NULL, data jsonb NOT NULL,
      render_count integer NOT NULL DEFAULT 0, cleanup jsonb,
      UNIQUE(project_id,identity_sha256), UNIQUE(id,project_id,owner_id),
      FOREIGN KEY(version_id,project_id,owner_id) REFERENCES aist.artifact_versions(id,project_id,owner_id),
      CHECK(data->>'id'=id AND data->>'project_id'=project_id AND data->>'artifact_version_id'=version_id
        AND data->>'identity_sha256'=identity_sha256),
      CHECK(data->>'state' IN ('queued','rendering','ready','failed','recovery_required','missing','corrupt')),
      CHECK(data->>'state'<>'ready' OR (data->'release_check'->>'status'='passed'
        AND jsonb_typeof(data->'file')='object' AND (data->'file'->>'byte_size')::bigint>0
        AND data->'file'->>'sha256' ~ '^[a-f0-9]{64}$')));
    CREATE FUNCTION aist.export_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Exports cannot be deleted'; END IF;
      IF (NEW.id,NEW.project_id,NEW.owner_id,NEW.version_id,NEW.identity_sha256) IS DISTINCT FROM
         (OLD.id,OLD.project_id,OLD.owner_id,OLD.version_id,OLD.identity_sha256)
         OR (NEW.data - ARRAY['state','revision','file','release_check','error','updated_at']) IS DISTINCT FROM
            (OLD.data - ARRAY['state','revision','file','release_check','error','updated_at']) THEN
        RAISE EXCEPTION 'Export identity is immutable'; END IF;
      IF OLD.data->'file'<>'null'::jsonb AND NEW.data->'file' IS DISTINCT FROM OLD.data->'file' THEN
        RAISE EXCEPTION 'Saved descriptor is immutable'; END IF;
      IF NEW.data->>'state'<>OLD.data->>'state' AND NOT (
        (OLD.data->>'state'='queued' AND NEW.data->>'state'='rendering') OR
        (OLD.data->>'state'='rendering' AND NEW.data->>'state' IN ('failed','recovery_required')) OR
        (OLD.data->>'state'='recovery_required' AND NEW.data->>'state' IN ('ready','failed')) OR
        (OLD.data->>'state'='ready' AND NEW.data->>'state' IN ('missing','corrupt'))) THEN
        RAISE EXCEPTION 'Invalid export transition'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER export_guard BEFORE UPDATE OR DELETE ON aist.exports FOR EACH ROW EXECUTE FUNCTION aist.export_guard();
    ALTER TABLE aist.receipts ADD COLUMN export_id text;
    ALTER TABLE aist.receipts ADD FOREIGN KEY(export_id,project_id,actor_id) REFERENCES aist.exports(id,project_id,owner_id);
    ''')
    op.execute("""DO $$ DECLARE old_check text; BEGIN
      SELECT pg_get_constraintdef(oid) INTO old_check FROM pg_constraint WHERE conrelid='aist.receipts'::regclass AND conname='receipts_check';
      ALTER TABLE aist.receipts DROP CONSTRAINT receipts_check;
      EXECUTE 'ALTER TABLE aist.receipts ADD CONSTRAINT receipts_check CHECK (('
        || substring(old_check from 8 for length(old_check)-8)
        || ') AND export_id IS NULL OR (operation=''requestExport'' AND project_scope=project_id'
        || ' AND target<>'''' AND export_id IS NOT NULL AND artifact_version_id IS NULL'
        || ' AND source_id IS NULL AND version_id IS NULL AND conversation_id IS NULL))';
    END $$;""")


def downgrade():
    raise RuntimeError('Сначала требуется сохранить выпуски и файлы')
