"""Guard export retries and publish worker availability in the application database."""

from alembic import op

revision = '0025_delivery_recovery'
down_revision = '0024_proposal_receipts'
branch_labels = depends_on = None


def upgrade():
    op.execute('''
    ALTER TABLE aist.exports ADD COLUMN retry_history jsonb NOT NULL DEFAULT '[]'::jsonb;
    CREATE TABLE aist.worker_availability (
      worker_id text PRIMARY KEY,
      compatibility_sha256 text NOT NULL CHECK(compatibility_sha256 ~ '^[0-9a-f]{64}$'),
      expires_at timestamptz NOT NULL
    );
    CREATE OR REPLACE FUNCTION aist.export_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Exports cannot be deleted'; END IF;
      IF (NEW.id,NEW.project_id,NEW.owner_id,NEW.version_id,NEW.identity_sha256) IS DISTINCT FROM
         (OLD.id,OLD.project_id,OLD.owner_id,OLD.version_id,OLD.identity_sha256)
         OR (NEW.data - ARRAY['state','revision','file','release_check','error','updated_at']) IS DISTINCT FROM
            (OLD.data - ARRAY['state','revision','file','release_check','error','updated_at']) THEN
        RAISE EXCEPTION 'Export identity is immutable'; END IF;
      IF OLD.data->'file'<>'null'::jsonb AND NEW.data->'file' IS DISTINCT FROM OLD.data->'file' THEN
        RAISE EXCEPTION 'Saved descriptor is immutable'; END IF;
      IF NEW.retry_history IS DISTINCT FROM OLD.retry_history THEN
        RAISE EXCEPTION 'Export retry history is immutable';
      END IF;
      IF OLD.data->>'state'='failed' AND NEW.data->>'state'='queued'
         AND OLD.data->'file'='null'::jsonb AND NEW.data->'file'='null'::jsonb
         AND OLD.data->'release_check'->>'status'='failed'
         AND OLD.data->'release_check'->'issues'='["Renderer: TimeoutExpired"]'::jsonb
         AND OLD.data->'error'->>'code' IN ('temporarily_unavailable','validation_failed')
         AND NEW.data->'release_check'->>'status'='pending'
         AND NEW.data->'error'='null'::jsonb AND NEW.render_count=OLD.render_count
         AND (NEW.data->>'revision')::int=(OLD.data->>'revision')::int+1 THEN
        NEW.retry_history := OLD.retry_history || jsonb_build_array(jsonb_build_object(
          'revision', (OLD.data->>'revision')::int, 'error', OLD.data->'error',
          'release_check', OLD.data->'release_check', 'failed_at', OLD.data->'updated_at',
          'cleanup', OLD.cleanup, 'queued_at', NEW.data->'updated_at'));
        RETURN NEW;
      END IF;
      IF NEW.data->>'state'<>OLD.data->>'state' AND NOT (
        (OLD.data->>'state'='queued' AND NEW.data->>'state'='rendering') OR
        (OLD.data->>'state'='rendering' AND NEW.data->>'state' IN ('failed','recovery_required')) OR
        (OLD.data->>'state'='recovery_required' AND NEW.data->>'state' IN ('ready','failed')) OR
        (OLD.data->>'state'='ready' AND NEW.data->>'state' IN ('missing','corrupt'))) THEN
        RAISE EXCEPTION 'Invalid export transition'; END IF;
      RETURN NEW;
    END $$;
    ''')


def downgrade():
    raise RuntimeError('Preserve export retry history before downgrading.')
