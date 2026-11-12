"""Сессии, проекты, неизменяемые PDF и durable receipts D06."""
from alembic import op

revision = '0002_projects_sources'
down_revision = '0001_foundation'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
    CREATE TABLE aist.users (
      id text PRIMARY KEY, label text NOT NULL CHECK (length(label) BETWEEN 1 AND 300),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp());
    CREATE TABLE aist.sessions (
      id text PRIMARY KEY, user_id text NOT NULL REFERENCES aist.users(id),
      token_hash char(64) NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      expires_at timestamptz NOT NULL, revoked_at timestamptz,
      CHECK (expires_at > created_at));
    CREATE TABLE aist.projects (
      id text PRIMARY KEY, owner_id text NOT NULL REFERENCES aist.users(id),
      name text NOT NULL CHECK (length(name) BETWEEN 1 AND 300),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(id, owner_id));
    CREATE TABLE aist.sources (
      id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL,
      title text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
      latest_version_id text NOT NULL, revision bigint NOT NULL CHECK (revision BETWEEN 1 AND 9007199254740991),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(project_id, owner_id) REFERENCES aist.projects(id, owner_id),
      UNIQUE(id, project_id, owner_id));
    CREATE TABLE aist.source_versions (
      id text PRIMARY KEY, source_id text NOT NULL, project_id text NOT NULL, owner_id text NOT NULL,
      revision bigint NOT NULL CHECK (revision BETWEEN 1 AND 9007199254740991),
      sha256 char(64) NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
      byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 8388608),
      original_filename text NOT NULL, storage_name text NOT NULL UNIQUE CHECK (storage_name ~ '^sv_[a-f0-9]{32}\.pdf$'),
      processor_version text NOT NULL, page_count integer NOT NULL CHECK (page_count BETWEEN 1 AND 40),
      processing_state text NOT NULL CHECK (processing_state IN ('ready','failed')), error jsonb,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CHECK ((processing_state='ready' AND error IS NULL) OR (processing_state='failed' AND error IS NOT NULL)),
      FOREIGN KEY(source_id, project_id, owner_id) REFERENCES aist.sources(id, project_id, owner_id),
      UNIQUE(source_id, revision), UNIQUE(id, source_id, project_id, owner_id), UNIQUE(id, project_id, owner_id),
      UNIQUE(id, sha256));
    ALTER TABLE aist.sources ADD CONSTRAINT source_head_scope
      FOREIGN KEY(latest_version_id, id, project_id, owner_id)
      REFERENCES aist.source_versions(id, source_id, project_id, owner_id) DEFERRABLE INITIALLY DEFERRED;
    CREATE TABLE aist.source_pages (
      version_id text NOT NULL, sha256 char(64) NOT NULL,
      page integer NOT NULL CHECK(page BETWEEN 1 AND 40),
      text text NOT NULL CHECK(length(text) BETWEEN 1 AND 200000), PRIMARY KEY(version_id,page),
      FOREIGN KEY(version_id, sha256) REFERENCES aist.source_versions(id, sha256));
    CREATE TABLE aist.conversations (
      id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL,
      title text NOT NULL CHECK(length(title) BETWEEN 1 AND 300),
      revision bigint NOT NULL DEFAULT 0 CHECK(revision BETWEEN 0 AND 9007199254740991),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(project_id, owner_id) REFERENCES aist.projects(id, owner_id),
      UNIQUE(id, project_id, owner_id));
    CREATE TABLE aist.receipts (
      actor_id text NOT NULL REFERENCES aist.users(id), project_scope text NOT NULL,
      operation text NOT NULL, target text NOT NULL, key text NOT NULL,
      fingerprint char(64) NOT NULL, project_id text NOT NULL,
      source_id text, version_id text, conversation_id text,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(actor_id, project_scope, operation, target, key),
      FOREIGN KEY(project_id, actor_id) REFERENCES aist.projects(id, owner_id),
      FOREIGN KEY(source_id, project_id, actor_id) REFERENCES aist.sources(id, project_id, owner_id),
      FOREIGN KEY(version_id, source_id, project_id, actor_id)
        REFERENCES aist.source_versions(id, source_id, project_id, owner_id),
      FOREIGN KEY(conversation_id, project_id, actor_id) REFERENCES aist.conversations(id, project_id, owner_id),
      CHECK (
        (operation='createProject' AND project_scope='' AND target='' AND source_id IS NULL AND version_id IS NULL AND conversation_id IS NULL)
        OR (operation='createConversation' AND project_scope=project_id AND target='' AND conversation_id IS NOT NULL AND source_id IS NULL AND version_id IS NULL)
        OR (operation='uploadSource' AND project_scope=project_id AND target='' AND source_id IS NOT NULL AND version_id IS NOT NULL AND conversation_id IS NULL)
        OR (operation='uploadSourceVersion' AND project_scope=project_id AND target=source_id AND source_id IS NOT NULL AND version_id IS NOT NULL AND conversation_id IS NULL)));
    CREATE INDEX projects_order ON aist.projects(owner_id,created_at,id);
    CREATE INDEX sources_order ON aist.sources(project_id,owner_id,created_at,id);
    CREATE INDEX conversations_order ON aist.conversations(project_id,owner_id,created_at,id);
    CREATE FUNCTION aist.immutable_pdf() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME='source_pages' OR TG_TABLE_NAME='receipts' OR TG_OP='DELETE' THEN
        RAISE EXCEPTION 'immutable record';
      END IF;
      IF (to_jsonb(NEW)-'processing_state'-'error') IS DISTINCT FROM (to_jsonb(OLD)-'processing_state'-'error') THEN
        RAISE EXCEPTION 'immutable source version';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER source_version_immutable BEFORE UPDATE OR DELETE ON aist.source_versions
      FOR EACH ROW EXECUTE FUNCTION aist.immutable_pdf();
    CREATE TRIGGER source_page_immutable BEFORE UPDATE OR DELETE ON aist.source_pages
      FOR EACH ROW EXECUTE FUNCTION aist.immutable_pdf();
    CREATE TRIGGER receipt_immutable BEFORE UPDATE OR DELETE ON aist.receipts
      FOR EACH ROW EXECUTE FUNCTION aist.immutable_pdf();
    ''')


def downgrade():
    op.execute('''DROP TABLE aist.receipts, aist.source_pages, aist.conversations;
      ALTER TABLE aist.sources DROP CONSTRAINT source_head_scope;
      DROP TABLE aist.source_versions, aist.sources, aist.projects, aist.sessions, aist.users;
      DROP FUNCTION aist.immutable_pdf();''')
