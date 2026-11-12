"""Pin a shared PDF in the receiving project without another upload."""
from alembic import op

revision = '0016_project_materials'
down_revision = '0015_workspace_folders'
branch_labels = depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE aist.source_versions ADD origin_version_id text,
            DROP CONSTRAINT source_versions_storage_name_key,
            ADD UNIQUE(id,owner_id,storage_name,sha256),
            ADD FOREIGN KEY(origin_version_id,owner_id,storage_name,sha256)
                REFERENCES aist.source_versions(id,owner_id,storage_name,sha256),
            ADD CHECK(origin_version_id IS NULL OR (media_type='application/pdf' AND origin_version_id<>id));
        CREATE UNIQUE INDEX original_pdf_file ON aist.source_versions(storage_name)
            WHERE origin_version_id IS NULL AND storage_name IS NOT NULL;
        CREATE UNIQUE INDEX registered_project_pdf ON aist.source_versions(project_id,origin_version_id)
            WHERE origin_version_id IS NOT NULL;
        CREATE VIEW aist.sample_source_versions AS
            SELECT DISTINCT v.id FROM aist.source_versions v
            JOIN aist.source_versions ancestor ON ancestor.source_id=v.source_id AND ancestor.revision<=v.revision
            JOIN aist.source_versions original ON original.id=coalesce(ancestor.origin_version_id,ancestor.id)
            JOIN aist.source_versions classified ON classified.source_id=original.source_id AND classified.revision<=original.revision
            JOIN aist.knowledge_materials m ON m.version_id=classified.id AND m.corpus='samples'
            UNION
            SELECT v.id FROM aist.source_versions v JOIN aist.source_versions prior
              ON prior.source_id=v.source_id AND prior.revision<=v.revision
            JOIN aist.knowledge_materials m ON m.version_id=prior.id AND m.corpus='samples';
    """)


def downgrade():
    raise RuntimeError('Preserve pinned project material versions before downgrading.')
