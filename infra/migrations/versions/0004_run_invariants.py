"""Cross-record invariants and immutable transport evidence."""
from alembic import op
revision='0004_run_invariants'
down_revision='0003_runs_recovery'
branch_labels=depends_on=None


def upgrade():
    op.execute(r'''
    ALTER TABLE aist.messages ADD UNIQUE(id,run_id,project_id,owner_id);
    ALTER TABLE aist.run_receipts ADD FOREIGN KEY(message_id,run_id,project_id,actor_id)
      REFERENCES aist.messages(id,run_id,project_id,owner_id);
    CREATE TABLE aist.model_transport_events (
      call_id text NOT NULL REFERENCES aist.model_calls(id) DEFERRABLE INITIALLY DEFERRED,
      seq bigint NOT NULL, state text NOT NULL CHECK(state IN ('reserved','attempted','sent','observed','unknown','not_sent')),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(call_id,seq));
    CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON aist.model_transport_events FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable();
    CREATE FUNCTION aist.call_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable call identity'; END IF;
      IF (to_jsonb(NEW)-'data'-'usage_entry_id'-'exposure') IS DISTINCT FROM (to_jsonb(OLD)-'data'-'usage_entry_id'-'exposure') THEN
        RAISE EXCEPTION 'immutable call identity'; END IF;
      IF TG_TABLE_NAME='model_calls' AND
        (NEW.data->'id',NEW.data->'run_id',NEW.data->'parent_tool_call_id',NEW.data->'parent_model_call_id',NEW.data->'purpose',NEW.data->'model_profile_version_id')
        IS DISTINCT FROM
        (OLD.data->'id',OLD.data->'run_id',OLD.data->'parent_tool_call_id',OLD.data->'parent_model_call_id',OLD.data->'purpose',OLD.data->'model_profile_version_id') THEN
        RAISE EXCEPTION 'immutable model call projection'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER model_identity BEFORE UPDATE OR DELETE ON aist.model_calls FOR EACH ROW EXECUTE FUNCTION aist.call_identity();
    CREATE TRIGGER tool_immutable BEFORE UPDATE OR DELETE ON aist.tool_calls FOR EACH ROW EXECUTE FUNCTION aist.d07_immutable();
    CREATE OR REPLACE FUNCTION aist.conversation_sequence() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE cid text;
    BEGIN
      IF TG_TABLE_NAME='conversations' THEN cid=NEW.id; ELSE cid=NEW.conversation_id; END IF;
      IF (SELECT revision FROM aist.conversations WHERE id=cid) <>
        (SELECT coalesce(max(sequence),0) FROM aist.messages WHERE conversation_id=cid) THEN
        RAISE EXCEPTION 'conversation revision differs from message sequence'; END IF; RETURN NEW;
    END $$;
    CREATE CONSTRAINT TRIGGER conversation_sequence AFTER UPDATE ON aist.conversations DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION aist.conversation_sequence();
    CREATE FUNCTION aist.event_sequence() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE rid text; maximum bigint; n bigint;
    BEGIN
      IF TG_TABLE_NAME='runs' THEN rid=NEW.id; ELSE rid=NEW.run_id; END IF;
      SELECT coalesce(max(seq),0),count(*) INTO maximum,n FROM aist.run_events WHERE run_id=rid;
      IF maximum<>n OR maximum<>(SELECT last_event_seq FROM aist.runs WHERE id=rid) THEN
        RAISE EXCEPTION 'run cursor differs from event journal'; END IF; RETURN NEW;
    END $$;
    CREATE CONSTRAINT TRIGGER run_event_cursor AFTER INSERT OR UPDATE ON aist.runs DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION aist.event_sequence();
    CREATE CONSTRAINT TRIGGER event_sequence AFTER INSERT ON aist.run_events DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION aist.event_sequence();
    ''')


def downgrade():raise RuntimeError('D07 downgrade requires an explicit data preservation plan')
