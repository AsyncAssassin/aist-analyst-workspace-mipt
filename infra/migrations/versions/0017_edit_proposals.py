"""Persist chat edit proposals and their atomic decisions."""

from alembic import op

revision = '0017_edit_proposals'
down_revision = '0016_project_materials'
branch_labels = depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE aist.edit_proposals (
            id text PRIMARY KEY, project_id text NOT NULL, owner_id text NOT NULL, conversation_id text NOT NULL,
            base_version_id text NOT NULL, run_id text UNIQUE, anchor bigint NOT NULL,
            request_key text NOT NULL, request_sha text NOT NULL, data jsonb NOT NULL,
            state text NOT NULL CHECK(state IN ('pending','applied','rejected')),
            applied_version_id text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE(conversation_id, request_key), CHECK((state='applied')=(applied_version_id IS NOT NULL)),
            FOREIGN KEY(conversation_id,project_id,owner_id) REFERENCES aist.conversations(id,project_id,owner_id),
            FOREIGN KEY(base_version_id,project_id,owner_id) REFERENCES aist.artifact_versions(id,project_id,owner_id),
            FOREIGN KEY(applied_version_id,project_id,owner_id) REFERENCES aist.artifact_versions(id,project_id,owner_id),
            FOREIGN KEY(run_id,conversation_id,project_id,owner_id) REFERENCES aist.runs(id,conversation_id,project_id,owner_id)
        );
        CREATE INDEX edit_proposal_history ON aist.edit_proposals(conversation_id,created_at,id);
        ALTER TABLE aist.runs DROP CONSTRAINT run_completed_result,
            ADD CONSTRAINT run_completed_result CHECK (
            (result_version_id IS NULL OR state='completed') AND
            (NOT (input ? 'edit_request') OR result_version_id IS NULL) AND
            (state<>'completed' OR result_version_id IS NOT NULL OR
            (input->>'requested_result_type'='evidence_answer' AND
             (jsonb_array_length(input->'source_versions')=0 OR input ? 'edit_request'))));
        CREATE FUNCTION aist.proposal_result_saved() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.state='completed' AND NEW.input ? 'edit_request' AND NOT EXISTS (
                SELECT 1 FROM aist.edit_proposals p JOIN aist.messages m ON m.run_id=p.run_id AND m.sequence=p.anchor
                WHERE p.run_id=NEW.id AND p.base_version_id=NEW.input->'edit_request'->>'version_id'
                AND m.role='assistant') THEN RAISE EXCEPTION 'Missing saved edit proposal'; END IF;
            RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER proposal_result_saved AFTER INSERT OR UPDATE OF state ON aist.runs
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aist.proposal_result_saved();
    """
    )


def downgrade():
    raise RuntimeError('Preserve chat proposal decisions before downgrading.')
