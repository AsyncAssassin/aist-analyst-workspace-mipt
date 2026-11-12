"""Organize conversations without changing the project that owns their evidence."""
from alembic import op

revision = '0015_workspace_folders'
down_revision = '0014_password_login'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE aist.projects ADD pinned boolean NOT NULL DEFAULT false,
            ADD personal boolean NOT NULL DEFAULT false;
        CREATE UNIQUE INDEX one_personal_project ON aist.projects(owner_id) WHERE personal;
        ALTER TABLE aist.conversations ADD folder_project_id text,
            ADD hidden_at timestamptz, ADD last_sent_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            ADD FOREIGN KEY(folder_project_id,owner_id) REFERENCES aist.projects(id,owner_id);
        CREATE INDEX recent_visible_chats ON aist.conversations(owner_id,last_sent_at DESC,id DESC)
            WHERE hidden_at IS NULL;
        CREATE INDEX folder_visible_chats ON aist.conversations(owner_id,folder_project_id,last_sent_at DESC,id DESC)
            WHERE hidden_at IS NULL;
        CREATE TABLE aist.navigation_receipts (
            owner_id text NOT NULL REFERENCES aist.users(id), key text NOT NULL,
            fingerprint text NOT NULL, result jsonb NOT NULL, PRIMARY KEY (owner_id,key));
        CREATE FUNCTION aist.chat_activity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE aist.conversations SET last_sent_at=NEW.created_at WHERE id=NEW.conversation_id;
            RETURN NEW;
        END $$;
        CREATE TRIGGER user_chat_activity AFTER INSERT ON aist.messages FOR EACH ROW
            WHEN (NEW.role='user') EXECUTE FUNCTION aist.chat_activity();
        UPDATE aist.conversations c SET folder_project_id=project_id,
            last_sent_at=coalesce((SELECT max(m.created_at) FROM aist.messages m
                                  WHERE m.conversation_id=c.id AND m.role='user'),c.created_at);
    """)


def downgrade():
    raise RuntimeError('Preserve chat folders and user actions before downgrading.')
