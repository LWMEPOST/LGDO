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
  domain: string;
  page_type: string;
  title: string;
  source_ids?: string[];
  review_status: string;
  owner?: string | null;
}

export interface ReviewItem {
  id: string;
  page_path: string;
  issue_type: string;
  status: string;
  owner?: string | null;
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

export interface EditorState {
  path: string;
  content: string;
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
  targetSection?: "overview" | "sources" | "wiki" | "qa" | "gaps" | "reviews";
  count: number;
}

export interface SpaceDirectoryGroup {
  id: string;
  label: string;
  items: SpaceFilter[];
}
