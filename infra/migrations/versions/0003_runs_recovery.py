"""Durable Run, executor leases, call ledger and minimal saved results."""
from alembic import op
revision = '0003_runs_recovery'
down_revision = '0002_projects_sources'
branch_labels = depends_on = None


def upgrade():
    op.execute(r'''
    CREATE TABLE aist.project_policies (
      project_id text NOT NULL, owner_id text NOT NULL, version text NOT NULL, data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(project_id,version),
      FOREIGN KEY(project_id,owner_id) REFERENCES aist.projects(id,owner_id));
    CREATE TABLE aist.artifacts (
      id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL, result_type text NOT NULL,
      latest_version_id text NOT NULL, revision bigint NOT NULL CHECK(revision>0),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(project_id,owner_id) REFERENCES aist.projects(id,owner_id), UNIQUE(id,project_id,owner_id));
    CREATE TABLE aist.artifact_versions (
      id text PRIMARY KEY, artifact_id text NOT NULL, project_id text NOT NULL, owner_id text NOT NULL,
      revision bigint NOT NULL CHECK(revision>0), data jsonb NOT NULL, view jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(artifact_id,revision),
      UNIQUE(id,project_id,owner_id), UNIQUE(id,artifact_id,project_id,owner_id),
      FOREIGN KEY(artifact_id,project_id,owner_id) REFERENCES aist.artifacts(id,project_id,owner_id));
    ALTER TABLE aist.artifacts ADD CONSTRAINT artifact_head_scope FOREIGN KEY(latest_version_id,id,project_id,owner_id)
      REFERENCES aist.artifact_versions(id,artifact_id,project_id,owner_id) DEFERRABLE INITIALLY DEFERRED;
    CREATE TABLE aist.runs (
      id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL, conversation_id text NOT NULL,
      message_id text NOT NULL, session_id text NOT NULL REFERENCES aist.sessions(id),
      revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
      state text NOT NULL CHECK(state IN ('queued','running','waiting_for_user','completed','failed','cancelled','budget_exceeded','recovery_required')),
      input jsonb NOT NULL, configuration jsonb NOT NULL, request_sha256 char(64) NOT NULL,
      result_version_id text, error jsonb, waiting_input jsonb, last_event_seq bigint NOT NULL DEFAULT 0 CHECK(last_event_seq>=0),
      epoch bigint NOT NULL DEFAULT 0 CHECK(epoch>=0), worker_id text, lease_until timestamptz,
      executor_run_id text, executor_session_id text, checkpoint jsonb, cancel_dispatched_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(id,project_id,owner_id), UNIQUE(id,conversation_id,project_id,owner_id),
      FOREIGN KEY(conversation_id,project_id,owner_id) REFERENCES aist.conversations(id,project_id,owner_id),
      FOREIGN KEY(result_version_id,project_id,owner_id) REFERENCES aist.artifact_versions(id,project_id,owner_id),
      CHECK ((state='completed')=(result_version_id IS NOT NULL)),
      CHECK ((state='waiting_for_user')=(waiting_input IS NOT NULL)),
      CHECK (state NOT IN ('failed','budget_exceeded','recovery_required') OR error IS NOT NULL),
      CHECK ((executor_run_id IS NULL)=(executor_session_id IS NULL)));
    CREATE TABLE aist.run_sources (
      run_id text NOT NULL, project_id text NOT NULL, owner_id text NOT NULL, source_version_id text NOT NULL,
      sha256 char(64) NOT NULL, PRIMARY KEY(run_id,source_version_id),
      FOREIGN KEY(run_id,project_id,owner_id) REFERENCES aist.runs(id,project_id,owner_id),
      FOREIGN KEY(source_version_id,project_id,owner_id) REFERENCES aist.source_versions(id,project_id,owner_id),
      FOREIGN KEY(source_version_id,sha256) REFERENCES aist.source_versions(id,sha256));
    CREATE TABLE aist.messages (
      id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL, conversation_id text NOT NULL,
      sequence bigint NOT NULL CHECK(sequence>0), role text NOT NULL CHECK(role IN ('user','assistant')),
      text text NOT NULL CHECK(length(text) BETWEEN 1 AND 20000), run_id text NOT NULL, artifact_version_id text,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(conversation_id,sequence),
      UNIQUE(id,conversation_id,project_id,owner_id),
      FOREIGN KEY(conversation_id,project_id,owner_id) REFERENCES aist.conversations(id,project_id,owner_id),
      FOREIGN KEY(run_id,conversation_id,project_id,owner_id) REFERENCES aist.runs(id,conversation_id,project_id,owner_id) DEFERRABLE INITIALLY DEFERRED,
      FOREIGN KEY(artifact_version_id,project_id,owner_id) REFERENCES aist.artifact_versions(id,project_id,owner_id));
    ALTER TABLE aist.runs ADD CONSTRAINT run_message_scope FOREIGN KEY(message_id,conversation_id,project_id,owner_id)
      REFERENCES aist.messages(id,conversation_id,project_id,owner_id) DEFERRABLE INITIALLY DEFERRED;
    CREATE TABLE aist.run_receipts (
      actor_id text NOT NULL, project_id text NOT NULL, operation text NOT NULL, target text NOT NULL, key text NOT NULL,
      fingerprint char(64) NOT NULL, run_id text NOT NULL, message_id text,
      PRIMARY KEY(actor_id,project_id,operation,target,key),
      FOREIGN KEY(run_id,project_id,actor_id) REFERENCES aist.runs(id,project_id,owner_id),
      FOREIGN KEY(message_id) REFERENCES aist.messages(id));
    CREATE TABLE aist.run_events (
      run_id text NOT NULL REFERENCES aist.runs(id), seq bigint NOT NULL CHECK(seq>0), data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(run_id,seq));
    CREATE TABLE aist.tool_calls (
      id text PRIMARY KEY, run_id text NOT NULL, project_id text NOT NULL, owner_id text NOT NULL,
      executor_tool_call_id text NOT NULL, epoch bigint NOT NULL, data jsonb NOT NULL, reply jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(run_id,executor_tool_call_id), UNIQUE(id,run_id),
      FOREIGN KEY(run_id,project_id,owner_id) REFERENCES aist.runs(id,project_id,owner_id));
    CREATE TABLE aist.model_calls (
      id text PRIMARY KEY, run_id text NOT NULL, project_id text NOT NULL, owner_id text NOT NULL, epoch bigint NOT NULL,
      parent_model_call_id text, parent_tool_call_id text, data jsonb NOT NULL, usage_entry_id text NOT NULL,
      exposure numeric NOT NULL CHECK(exposure>=0), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(id,run_id), FOREIGN KEY(run_id,project_id,owner_id) REFERENCES aist.runs(id,project_id,owner_id),
      FOREIGN KEY(parent_model_call_id,run_id) REFERENCES aist.model_calls(id,run_id),
      FOREIGN KEY(parent_tool_call_id,run_id) REFERENCES aist.tool_calls(id,run_id));
    CREATE TABLE aist.usage_entries (
      id text PRIMARY KEY, run_id text NOT NULL, model_call_id text NOT NULL, supersedes_id text UNIQUE, data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(id,model_call_id,run_id),
      FOREIGN KEY(model_call_id,run_id) REFERENCES aist.model_calls(id,run_id) DEFERRABLE INITIALLY DEFERRED,
      FOREIGN KEY(supersedes_id,model_call_id,run_id) REFERENCES aist.usage_entries(id,model_call_id,run_id));
    ALTER TABLE aist.model_calls ADD CONSTRAINT current_usage_scope FOREIGN KEY(usage_entry_id,id,run_id)
      REFERENCES aist.usage_entries(id,model_call_id,run_id) DEFERRABLE INITIALLY DEFERRED;
    CREATE UNIQUE INDEX usage_root ON aist.usage_entries(model_call_id) WHERE supersedes_id IS NULL;
    CREATE TABLE aist.billing_aggregates (id text PRIMARY KEY, data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp());
    CREATE INDEX run_queue ON aist.runs(created_at,id) WHERE state IN ('queued','running');
    CREATE INDEX run_order ON aist.runs(project_id,owner_id,created_at,id);
    CREATE FUNCTION aist.d07_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'immutable D07 record'; END $$;
    CREATE FUNCTION aist.run_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' OR (NEW.id,NEW.project_id,NEW.owner_id,NEW.conversation_id,NEW.message_id,NEW.session_id,NEW.input,NEW.configuration,NEW.request_sha256,NEW.created_at)
       IS DISTINCT FROM (OLD.id,OLD.project_id,OLD.owner_id,OLD.conversation_id,OLD.message_id,OLD.session_id,OLD.input,OLD.configuration,OLD.request_sha256,OLD.created_at) THEN
        RAISE EXCEPTION 'immutable run input'; END IF;
      IF OLD.state IN ('completed','failed','cancelled','budget_exceeded') AND
        (NEW.state,NEW.result_version_id,NEW.waiting_input,NEW.error,NEW.revision) IS DISTINCT FROM
        (OLD.state,OLD.result_version_id,OLD.waiting_input,OLD.error,OLD.revision) THEN RAISE EXCEPTION 'terminal run'; END IF;
      IF NEW.epoch<OLD.epoch OR NEW.last_event_seq<OLD.last_event_seq OR NEW.revision<OLD.revision THEN RAISE EXCEPTION 'nonmonotonic run'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER run_immutable BEFORE UPDATE OR DELETE ON aist.runs FOR EACH ROW EXECUTE FUNCTION aist.run_immutable();
    CREATE FUNCTION aist.conversation_sequence() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (SELECT revision FROM aist.conversations WHERE id=NEW.conversation_id) <>
       (SELECT coalesce(max(sequence),0) FROM aist.messages WHERE conversation_id=NEW.conversation_id) THEN
        RAISE EXCEPTION 'conversation revision differs from message sequence'; END IF; RETURN NEW;
    END $$;
    CREATE CONSTRAINT TRIGGER message_sequence AFTER INSERT ON aist.messages DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION aist.conversation_sequence();
    ''')
    for table in ('project_policies','artifact_versions','run_sources','messages','run_receipts','run_events','usage_entries','billing_aggregates'):
        op.execute(f'CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON aist.{table} FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable()')


def downgrade():
    raise RuntimeError('D07 downgrade requires an explicit data preservation plan')
