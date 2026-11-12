"""Explicit, one-message selections from a visible workspace folder."""
from sqlalchemy import text

from .errors import ApiError
from .service import ident, row
from .search import require_factual


# A moved chat exposes only the versions it actually used, never its whole storage project.
FOLDER_PDFS = """WITH member_versions AS (
    SELECT v.id FROM aist.source_versions v
    WHERE v.owner_id=:actor AND v.project_id=:folder AND v.origin_version_id IS NULL
    UNION
    SELECT coalesce(v.origin_version_id,v.id)
    FROM aist.conversations chat JOIN aist.runs r ON r.conversation_id=chat.id
    JOIN aist.run_sources rs ON rs.run_id=r.id JOIN aist.source_versions v ON v.id=rs.source_version_id
    WHERE chat.folder_project_id=:folder AND chat.owner_id=:actor AND chat.hidden_at IS NULL
      AND v.processing_state='ready'
      AND NOT EXISTS (SELECT 1 FROM aist.sample_source_versions restricted WHERE restricted.id=v.id)
) SELECT v.* FROM member_versions member JOIN aist.source_versions v ON v.id=member.id
WHERE v.owner_id=:actor AND v.media_type='application/pdf' AND v.processing_state='ready'
AND NOT EXISTS (SELECT 1 FROM aist.sample_source_versions sample WHERE sample.id=v.id)"""


def options(c, actor, folder, search):
    params = dict(actor=actor, folder=folder, search=search)
    pdfs = c.execute(text(FOLDER_PDFS + """
        AND strpos(lower(v.original_filename),lower(:search))>0
        ORDER BY v.created_at DESC,v.id LIMIT 31"""), params).mappings().all()
    chats = c.execute(text("""SELECT id,coalesce(title_override,title) AS title,revision AS through_sequence FROM aist.conversations
        WHERE folder_project_id=:folder AND owner_id=:actor AND hidden_at IS NULL AND revision>0
          AND strpos(lower(coalesce(title_override,title)),lower(:search))>0 ORDER BY last_sent_at DESC,id LIMIT 31"""), params).mappings().all()
    fields = ('id', 'original_filename', 'revision', 'page_count')
    return dict(pdfs=[{k: v[k] for k in fields} for v in pdfs[:30]],
                chats=[dict(chat) for chat in chats[:30]], more=len(pdfs)>30 or len(chats)>30)


def local_pdf(c, actor, project, original):
    """The caller holds the destination project lock and the Run receipt transaction."""
    if original['project_id'] == project:
        return original['id']
    saved = row(c, 'SELECT id FROM aist.source_versions WHERE project_id=:p AND origin_version_id=:v',
                p=project, v=original['id'])
    if saved:
        return saved['id']
    # The bytes and page locators belong to this immutable original, even after moving either chat.
    source, version = ident('src'), ident('sv')
    args = dict(actor=actor, project=project, source=source, version=version, original=original['id'],
                filename=original['original_filename'])
    c.execute(text("""INSERT INTO aist.sources(id,project_id,owner_id,title,latest_version_id,revision)
        VALUES (:source,:project,:actor,:filename,:version,1)"""), args)
    c.execute(text("""INSERT INTO aist.source_versions(id,source_id,project_id,owner_id,revision,sha256,byte_size,
        original_filename,storage_name,processor_version,page_count,processing_state,origin_version_id)
        SELECT :version,:source,:project,:actor,1,sha256,byte_size,original_filename,storage_name,
               processor_version,page_count,processing_state,id FROM aist.source_versions WHERE id=:original"""),args)
    c.execute(text("""INSERT INTO aist.source_pages(version_id,sha256,page,text,layout_text,diagram_regions)
        SELECT :version,sha256,page,text,layout_text,diagram_regions FROM aist.source_pages WHERE version_id=:original"""),args)
    return version


def select_materials(c, actor, current, selection):
    from .navigation import enabled
    folder = selection['folder_id']
    if not enabled(actor) or current['folder_project_id'] != folder or current['hidden_at'] is not None:
        raise ApiError('resource_not_found')
    pdfs, chats = [], []
    for vid in selection['pdf_version_ids']:
        original = row(c, FOLDER_PDFS + ' AND v.id=:v', actor=actor, folder=folder, v=vid)
        if not original:
            raise ApiError('resource_not_found')
        require_factual(c, vid)
        local = local_pdf(c, actor, current['project_id'], original)
        pdfs.append(dict(id=vid, local_version_id=local, original_filename=original['original_filename'],
                         revision=original['revision']))
    seen = {current['id']}
    for item in selection['chats']:
        chat = row(c, """SELECT id,project_id,coalesce(title_override,title) AS title,revision FROM aist.conversations WHERE id=:id
            AND folder_project_id=:folder AND owner_id=:actor AND hidden_at IS NULL""",
            id=item['id'], folder=folder, actor=actor)
        if not chat or chat['id'] in seen:
            raise ApiError('resource_not_found')
        if item['through_sequence'] > chat['revision']:
            raise ApiError('revision_conflict')
        seen.add(chat['id'])
        chats.append(dict(chat, through_sequence=item['through_sequence']))
    return pdfs, chats
