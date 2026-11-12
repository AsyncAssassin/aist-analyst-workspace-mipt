"""MOEX snapshots and JSON section locators, preserving existing PDF indices."""
from alembic import op

revision = '0011_moex_snapshots'
down_revision = '0010_embedding_pricing'
branch_labels = depends_on = None


def upgrade():
    op.execute('''
    ALTER TABLE aist.source_versions ADD COLUMN media_type text NOT NULL DEFAULT 'application/pdf';
    ALTER TABLE aist.source_versions ALTER COLUMN storage_name DROP NOT NULL;
    ALTER TABLE aist.source_versions ALTER COLUMN page_count DROP NOT NULL;
    ALTER TABLE aist.source_versions ADD CONSTRAINT source_media_storage CHECK (
      (media_type='application/pdf' AND storage_name IS NOT NULL AND page_count IS NOT NULL) OR
      (media_type='application/json' AND storage_name IS NULL AND page_count IS NULL));
    CREATE TABLE aist.moex_sources (
      project_id text PRIMARY KEY, owner_id text NOT NULL, source_id text NOT NULL UNIQUE,
      FOREIGN KEY(source_id,project_id,owner_id) REFERENCES aist.sources(id,project_id,owner_id));
    CREATE TABLE aist.moex_snapshots (
      version_id text PRIMARY KEY REFERENCES aist.source_versions(id),
      content_sha256 char(64) NOT NULL, payload text NOT NULL,
      normalized jsonb NOT NULL, fetched_at timestamptz NOT NULL);
    CREATE TRIGGER moex_snapshot_immutable BEFORE UPDATE OR DELETE ON aist.moex_snapshots
      FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable();
    CREATE TABLE aist.source_sections (
      version_id text NOT NULL, sha256 char(64) NOT NULL, section text NOT NULL,
      text text NOT NULL CHECK(length(text) BETWEEN 1 AND 200000),
      PRIMARY KEY(version_id,section),
      FOREIGN KEY(version_id,sha256) REFERENCES aist.source_versions(id,sha256));
    CREATE TRIGGER source_section_immutable BEFORE UPDATE OR DELETE ON aist.source_sections
      FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable();
    ALTER TABLE aist.knowledge_chunks ALTER COLUMN page DROP NOT NULL;
    ALTER TABLE aist.knowledge_chunks ADD COLUMN section text;
    ALTER TABLE aist.knowledge_chunks ADD CONSTRAINT knowledge_section
      FOREIGN KEY(version_id,section) REFERENCES aist.source_sections(version_id,section);
    ALTER TABLE aist.knowledge_chunks ADD CONSTRAINT knowledge_location CHECK (
      (page IS NOT NULL AND section IS NULL) OR (page IS NULL AND section IS NOT NULL));
    CREATE TABLE aist.moex_checks (
      id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL,
      key text NOT NULL, mode text NOT NULL CHECK(mode IN ('get','refresh')),
      state text NOT NULL CHECK(state IN ('started','succeeded','failed','cached')),
      started_at timestamptz NOT NULL DEFAULT clock_timestamp(), completed_at timestamptz,
      version_id text, response jsonb, observation text,
      UNIQUE(project_id,owner_id,key),
      FOREIGN KEY(project_id,owner_id) REFERENCES aist.projects(id,owner_id),
      FOREIGN KEY(version_id,project_id,owner_id) REFERENCES aist.source_versions(id,project_id,owner_id));
    ''')


def downgrade():
    # Snapshot deletion is intentionally not an automatic rollback operation.
    raise RuntimeError('Preserve external snapshots; restore a database backup to roll back D16.')
