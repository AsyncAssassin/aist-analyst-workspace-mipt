"""Allow a completed source-free conversation with a durable assistant message."""
from alembic import op

revision = '0012_conversation_reply'
down_revision = '0011_moex_snapshots'
branch_labels = depends_on = None


def upgrade():
    op.execute('''
    DO $$ DECLARE rule text; BEGIN
      SELECT conname INTO STRICT rule FROM pg_constraint
        WHERE conrelid='aist.runs'::regclass AND contype='c'
        AND pg_get_constraintdef(oid) LIKE '%completed%result_version_id%';
      EXECUTE format('ALTER TABLE aist.runs DROP CONSTRAINT %I',rule);
    END $$;
    ALTER TABLE aist.runs ADD CONSTRAINT run_completed_result CHECK (
      (result_version_id IS NULL OR state='completed') AND
      (state<>'completed' OR result_version_id IS NOT NULL OR
        (input->>'requested_result_type'='evidence_answer' AND jsonb_array_length(input->'source_versions')=0)));
    CREATE FUNCTION aist.conversation_reply_saved() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.state='completed' AND NEW.result_version_id IS NULL AND NOT EXISTS (
        SELECT 1 FROM aist.messages WHERE run_id=NEW.id AND owner_id=NEW.owner_id
          AND project_id=NEW.project_id AND conversation_id=NEW.conversation_id AND role='assistant'
          AND sequence>(SELECT sequence FROM aist.messages WHERE id=NEW.message_id)) THEN
        RAISE EXCEPTION 'completed conversation requires an assistant message';
      END IF;
      RETURN NEW;
    END $$;
    CREATE CONSTRAINT TRIGGER conversation_reply_saved AFTER INSERT OR UPDATE ON aist.runs
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aist.conversation_reply_saved();
    ''')


def downgrade():
    raise RuntimeError('Conversation replies must be preserved before downgrade')
