"""Durable embedding progress and explicit sample provenance."""
from alembic import op
revision = '0009_knowledge_search'
down_revision = '0008_page_layout'
branch_labels = depends_on = None


def upgrade():
    op.execute('''
    CREATE TABLE aist.knowledge_materials (
      version_id text PRIMARY KEY REFERENCES aist.source_versions(id),
      corpus text NOT NULL CHECK(corpus IN ('factual','samples')),
      company text, period text, provenance text NOT NULL,
      CHECK(company IS NULL OR length(company) BETWEEN 1 AND 300),
      CHECK(period IS NULL OR length(period) BETWEEN 1 AND 300));
    CREATE TRIGGER knowledge_material_immutable BEFORE UPDATE OR DELETE ON aist.knowledge_materials
      FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable();
    CREATE TABLE aist.embedding_indexes (
      id text PRIMARY KEY, configuration jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp());
    CREATE TABLE aist.embedding_calls (
      id text PRIMARY KEY, index_id text NOT NULL REFERENCES aist.embedding_indexes(id),
      owner_id text NOT NULL REFERENCES aist.users(id), purpose text NOT NULL,
      input_hashes jsonb NOT NULL, state text NOT NULL, http_status integer,
      usage jsonb, estimated_usd numeric, billed_usd numeric,
      exposure numeric NOT NULL, error_code text,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), observed_at timestamptz);
    CREATE TABLE aist.embedding_cache (
      index_id text NOT NULL REFERENCES aist.embedding_indexes(id),
      owner_id text NOT NULL REFERENCES aist.users(id), input_sha256 text NOT NULL,
      vector jsonb NOT NULL, call_id text NOT NULL REFERENCES aist.embedding_calls(id),
      PRIMARY KEY(index_id,owner_id,input_sha256));
    CREATE TABLE aist.knowledge_chunks (
      index_id text NOT NULL REFERENCES aist.embedding_indexes(id),
      id uuid NOT NULL, version_id text NOT NULL REFERENCES aist.source_versions(id),
      page integer NOT NULL, char_start integer NOT NULL, char_end integer NOT NULL,
      input_sha256 text NOT NULL, indexed boolean NOT NULL DEFAULT false,
      PRIMARY KEY(index_id,id), FOREIGN KEY(version_id,page) REFERENCES aist.source_pages(version_id,page),
      CHECK(char_start>=0 AND char_end>char_start));
    CREATE INDEX knowledge_chunks_version ON aist.knowledge_chunks(index_id,version_id);
    ''')


def downgrade():
    op.execute('DROP TABLE aist.knowledge_chunks, aist.embedding_cache, aist.embedding_calls, aist.embedding_indexes, aist.knowledge_materials')
