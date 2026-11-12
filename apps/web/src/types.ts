export type ExternalContext = {research_version?:'agent.v1'|'agent.v2';research_closed?:boolean;news_search?:{succeeded:number;failed:number;evidence_ids?:string[]};collection_version?:'materials.v1';company:string;as_of:string;status:'collected'|'found'|'partial'|'empty'|'unmatched'|'unavailable';topics:Record<string,string>;items:{evidence_id:string;subject?:string;search_target?:string;search_role?:'company'|'competitor'|'market';topics:string[];url:string;publisher:string;title:string;published:string|null;retrieved_at:string;page_sha256:string|null;response_sha256?:string;retrieval_kind?:'provider_extract';provider?:'parallel';provider_acquired_at?:string|null;quote:string;truncated?:boolean;source_kind:'company'|'publication'}[]};
// Narrow views of the accepted D04.v1 contract. Server schemas remain authoritative.
export type Project = { id: string; name: string; created_at: string };
export type Conversation = { id: string; title: string; revision: number };
export type Source = { id: string; title: string; latest_version_id: string; revision: number };
export type SourceVersion = { media_type?: 'application/pdf' | 'application/json'; corpus?: 'factual' | 'samples'; id: string; source_id: string; revision: number; sha256: string; original_filename: string; page_count: number | null; processing_state: string };
export type Pin = { source_id: string; source_version_id: string; sha256: string };
export type Message = { id: string; sequence: number; role: 'user' | 'assistant'; text: string; run_id: string | null; artifact_version_id: string | null; created_at: string };
export type RunState = 'queued' | 'running' | 'waiting_for_user' | 'completed' | 'failed' | 'cancelled' | 'budget_exceeded' | 'recovery_required';
export type Run = { id: string; conversation_id: string; message_id: string; revision: number; state: RunState; input: { requested_result_type?: string; project_context?:{folder_id:string;pdfs:{id:string;local_version_id:string;original_filename:string;revision:number}[];chats:{id:string;title:string;through_sequence:number}[]}; source_versions: Pin[]; skill_version_id: string; context_version_id?: string | null; budget?: { max_duration_seconds: number } }; last_event_seq: number; result_version_id: string | null; error: { code: string; message: string } | null; waiting_input: { id: string; prompt: string } | null; created_at?: string; updated_at?: string };
export type ModelCall = { id: string; purpose: string; transport_state: string; parent_model_call_id: string | null; http_status: number | null };
export type RunEvent = { run_id: string; seq: number; type: string; timestamp: string; data: Record<string, unknown> };
export type PageContent = { text: string; page: number; source: Pin };
export type VersionView = { omitted_note_blocks?:string[]; version: { id: string; artifact_id:string; parent_version_id:string|null; content_sha256:string; revision: number; created_at: string; payload: { result_type: string; content: { claims: Claim[]; missing_information: string[]; title?: string; audience?: string; team?: string; sections?: {section_id:string;title:string;blocks:NoteBlock[]}[] } }; provenance: { origin:string; actor_id:string|null; source_versions: Pin[]; skill_version_id: string | null }; basis: { external_context?:ExternalContext; request_messages?:{message_id:string;text:string}[]; news_run?:{scope?:'market';status:'found'|'partial'|'empty'|'unmatched'|'unavailable'|'not_searched';company:string;as_of:string;items:{quote:string;url:string;publisher:string;published:string|null;event_date?:string|null;external_evidence_id?:string;date_quote?:string;metadata_published?:string|null;retrieved_at?:string}[]};review_candidates?: ReviewCandidateBasis[]; visual_observations?: VisualObservation[]; facts: Fact[]; calculations: Calculation[]; comparisons?: {fact_refs: Claim['fact_refs']; value_relation:string; comparability:string; message:string}[] } }; review: {revision:number;validation_ids:string[];acceptance:{id:string;actor_id:string;created_at:string;content_sha256:string}|null}; checks: {id:string;kind:string;status:string;findings:{code:string;severity:string;block_id:string|null;row_id?:string;message:string}[]}[] };
export type UsageValue = { value: string | null; reason: string | null };
export type Usage = { id: string; supersedes_id: string | null; model_call_id: string; tokens: Record<string, { value: number | string | null; reason: string | null }>; estimated_usd: UsageValue; billed_usd: UsageValue };
export type Registry = { model_execution?: { state: 'available'|'disabled'|'owner_denied'|'temporarily_unavailable'|'busy'|'budget_exceeded'; message: string }; skills: { id: string; status: string; skill_name: string; version: string; max_sources_execution: number; result_type: string; bundle_sha256: string | null }[]; model_profiles: { id: string; status: string; model: string; configuration_sha256: string | null }[] };
export type Claim = {external_evidence_ids?:string[];text:string; origin:string; fact_refs:{source_version_id:string;fact_id:string}[]; calculation_ids:string[];visual_observation_ids?:string[];review_candidate_ids?:string[]};
export type TextMark = {start:number;end:number;style:'bold'|'italic'|'both'};
export type ParagraphBlock = {block_id:string;kind:'paragraph';claim:Claim;marks?:TextMark[];list_level?:1|2};
export type NoteBlock = ParagraphBlock | {block_id:string;kind:'table';caption:string;rows:{row_id:string;label:string;period_label:string;amount:{value:string;unit:string;scale:string;currency:string|null};fact_refs:Claim['fact_refs'];calculation_ids:string[];visual_observation_ids?:string[]}[]};
export const states: Record<RunState, string> = { queued: 'В очереди', running: 'В работе', waiting_for_user: 'Нужно уточнение', completed: 'Завершено', failed: 'Не выполнено', cancelled: 'Отменено', budget_exceeded: 'Достигнут лимит', recovery_required: 'Требуется проверка' };
export const isActive = (r: Run) => ['queued', 'running', 'waiting_for_user'].includes(r.state);

export type Fact = { fact_id: string; source: Pin; extraction_version: string; metric: string; subject: string; reporting_basis: string; nature: string; period: {label: string}; raw_text: string; amount: {value: string; unit: string; scale: string; currency: string | null} | null; evidence: {evidence_id: string; locator: {pdf_page?: number; json_pointer?: string; area: string}; quote: string}[]; limitations: string[] };
export type Calculation = { calculation_id: string; inputs: {fact: Claim['fact_refs'][number]}[]; formula_display: string; exact: {numerator: string; denominator: string}; rounded: {value: string}; limitations: string[] };

export const factAnchor = (ref: {source_version_id:string;fact_id:string}) => `basis-${encodeURIComponent(ref.source_version_id)}:${encodeURIComponent(ref.fact_id)}`;

export type VisualObservation = {observation_id:string;source:Pin;page:number;area:string;text:string;verification_status:"unchecked";numeric:{metric:string;subject:string;amount:NonNullable<Fact["amount"]>;period:Fact["period"];nature:"reported_actual"|"forecast"}|null};

export type ReviewCandidateBasis = {code:string;reason:string;message:string;review_candidate:{
  candidate_id:string;source:Pin;status:'not_admitted';input:{
    metric:string|null;subject:string;reporting_basis:string;nature:string;
    value_text:string|null;amount:{value:string;unit:string;currency:string|null}|null;
    period:{kind:string;label:string;start:string|null;end:string|null};evidence:{page:number;line_start:number;line_end:number;area:string}[];
    basis:{page:number;reporting_page?:number;metric_label:string;unit_label:string;reporting_label:string|null}|null;
    table?:{page:number}|null;
  };
}};

export type NewsRun = NonNullable<VersionView['version']['basis']['news_run']>;
