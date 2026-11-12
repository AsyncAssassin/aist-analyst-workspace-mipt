"""Source-bound immutable facts; accept original PDFs up to 16 MiB."""
from alembic import op
revision = '0005_domain_facts'
down_revision = '0004_run_invariants'
branch_labels = depends_on = None


def upgrade():
    op.execute('''
    ALTER TABLE aist.source_versions DROP CONSTRAINT source_versions_byte_size_check;
    ALTER TABLE aist.source_versions ADD CONSTRAINT source_versions_byte_size_check CHECK(byte_size BETWEEN 1 AND 16777216);
    CREATE TABLE aist.facts (
      source_version_id text NOT NULL, fact_id text NOT NULL, source_id text NOT NULL,
      sha256 char(64) NOT NULL, project_id text NOT NULL, owner_id text NOT NULL,
      data jsonb NOT NULL, calculation_basis jsonb NOT NULL, provenance jsonb NOT NULL,
      content_sha256 char(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(source_version_id,fact_id),
      FOREIGN KEY(source_version_id,source_id,project_id,owner_id) REFERENCES aist.source_versions(id,source_id,project_id,owner_id),
      FOREIGN KEY(source_version_id,sha256) REFERENCES aist.source_versions(id,sha256),
      CHECK(data->>'fact_id'=fact_id AND data->'source'->>'source_version_id'=source_version_id
        AND data->'source'->>'source_id'=source_id AND data->'source'->>'sha256'=sha256));
    CREATE TRIGGER fact_immutable BEFORE UPDATE OR DELETE ON aist.facts
      FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable();
    ''')


def downgrade():
    # Deliberately refuses a downgrade if a retained source exceeds the older limit.
    op.execute('''ALTER TABLE aist.source_versions DROP CONSTRAINT source_versions_byte_size_check;
      ALTER TABLE aist.source_versions ADD CONSTRAINT source_versions_byte_size_check CHECK(byte_size BETWEEN 1 AND 8388608);
      DROP TABLE aist.facts;''')
