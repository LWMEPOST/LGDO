export type Metadata = Record<string, any>;

export interface SourceRecord {
  id: string;
  domain: string;
  owner?: string | null;
  title: string;
  source_type: string;
  original_path: string;
  raw_path: string;
  content_hash: string;
  size_bytes: number;
  status: string;
  metadata?: Metadata;
  created_at?: string;
  updated_at?: string;
  last_compiled_at?: string | null;
}

export interface IngestReport {
  id: string;
  source_id: string;
  job_id: string;
  parser: string;
  normalized_path: string;
  jsonl_path: string;
  chunk_count: number;
  char_count: number;
  warnings?: string[];
  metadata?: Metadata;
  created_at?: string;
}

export interface WikiPage {
  path: string;
  page_id?: string | null;
  domain: string;
  page_type: string;
  title: string;
  source_ids?: string[];
  review_status: string;
  owner?: string | null;
  current_revision_id?: string | null;
  generated_revision_id?: string | null;
  accepted_generated_revision_id?: string | null;
  lifecycle_status?: string | null;
  projection_epoch?: number | null;
  pending_write_intent_id?: string | null;
  write_in_progress?: boolean;
  write_intent_id?: string | null;
  sync_error?: string | null;
}

export interface VaultReconcileJob {
  job_id: string;
  status: string;
  result?: Record<string, number> | null;
  error_summary?: string | null;
}

export interface VaultStatus {
  configured: boolean;
  running: boolean;
  clean: boolean;
  degraded: boolean;
  last_event_at?: string | null;
  last_error?: string | null;
  pending_occurrences: number;
  failed_occurrences: number;
  pending_deletes: number;
  open_issues: number;
  invalid_pages: number;
  projection_backlog: number;
  projection: Record<string, unknown>;
  obsidian: Record<string, unknown>;
  reconcile?: VaultReconcileJob | null;
}

export interface ReviewItem {
  id: string;
  page_path: string;
  page_id?: string | null;
  issue_type: string;
  status: string;
  owner?: string | null;
  source_ids?: string[];
  base_revision_id?: string | null;
  candidate_revision_id?: string | null;
  resolution_revision_id?: string | null;
  expected_state?: Record<string, unknown>;
  resolved_at?: string | null;
}

export interface WikiPageContentResponse {
  path: string;
  page_id: string;
  content: string;
  current_revision_id: string;
  generated_revision_id: string | null;
  accepted_generated_revision_id: string | null;
  lifecycle_status: string;
  projection_epoch: number;
  write_in_progress: boolean;
  write_intent_id: string | null;
  metadata: Metadata;
}

export interface WikiMutationResponse {
  path: string;
  page_path: string;
  page_id: string;
  status: string;
  revision_id: string | null;
  current_revision_id: string | null;
  generated_revision_id: string | null;
  candidate_revision_id: string | null;
  write_intent_id: string | null;
  conflict_review_id: string | null;
  observation_id: string | null;
  audit_revision_id: string | null;
  projection_job_ids: string[];
  replayed: boolean;
  review_status: string | null;
}

export interface KnowledgeGap {
  id: string;
  query_id: string;
  question: string;
  answer: string;
  comment?: string | null;
  status: string;
  priority: string;
  owner?: string | null;
  linked_page_path?: string | null;
}

export interface RagStatus {
  database_backend: string;
  rag_store_backend: string;
  chunk_count: number;
  source_count: number;
  embedding_count?: number;
  embedding_model?: string;
  vector_count?: number;
  vector_backend?: string;
  pgvector_enabled?: boolean;
  domains: Array<{ domain: string; chunk_count: number }>;
  external_system_apis: Record<string, string>;
  postgres: {
    host: string;
    port: number;
    database: string;
    user: string;
  };
}

export interface SourcePreview {
  id: string;
  title: string;
  source_type: string;
  parser?: string | null;
  preview_path: string;
  content: string;
  truncated: boolean;
  char_count: number;
  warnings: string[];
}

export interface AskResponse {
  query_id: string;
  answer: string;
  confidence: string;
  missing_info: string[];
  retrieval_strategy?: {
    answer_mode?: string;
    mode_label?: string;
    chunk_hits?: number;
    authorized_chunk_hits?: number;
    context_limit?: number;
    memory_hits?: number;
    alias_expanded?: boolean;
    matched_aliases?: Array<{ canonical_name?: string; alias?: string; domain?: string }>;
    keyword_weight?: number;
    vector_weight?: number;
  };
  user_context?: {
    user_id?: string;
    username?: string;
    role?: string;
    acl_tags?: string[];
    auth_provider?: string;
  };
  memory_hits?: Array<{
    query_id: string;
    question: string;
    answer_snippet: string;
    score: number;
    confidence: string;
    created_at: string;
  }>;
  citations: Array<{
    source_id: string;
    wiki_page?: string | null;
    snippet: string;
  }>;
}

export interface AuthUser {
  user_id: string;
  username?: string | null;
  role: string;
  acl_tags: string[];
  auth_provider?: string;
}

export interface AuthSession {
  token: string;
  user: AccountRecord;
}

export interface AccountRecord {
  user_id: string;
  username?: string | null;
  role: string;
  acl_tags: string[];
  status: "active" | "disabled" | string;
  auth_provider: string;
  password_configured: boolean;
  created_at: string;
  updated_at: string;
  last_login_at?: string | null;
}

export interface EditorState {
  path: string;
  page_id: string;
  content: string;
  current_revision_id: string;
  generated_revision_id: string | null;
  accepted_generated_revision_id: string | null;
  lifecycle_status: string;
  projection_epoch: number;
  write_in_progress: boolean;
  write_intent_id: string | null;
  review_status: string;
  owner: string;
}

export type SpaceFilterKind = "all" | "domain" | "page_type" | "review_status" | "gaps";

export interface SpaceFilter {
  id: string;
  label: string;
  desc: string;
  kind: SpaceFilterKind;
  value?: string;
  targetSection?: "overview" | "sources" | "wiki" | "qa" | "gaps" | "reviews" | "accounts";
  count: number;
}

export interface SpaceDirectoryGroup {
  id: string;
  label: string;
  items: SpaceFilter[];
}
