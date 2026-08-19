export type JobStatus =
  | "PENDING"
  | "PROCESSING"
  | "RETRYING"
  | "DONE"
  | "FAILED"
  | "DEAD_LETTER";

export type JobStage = "extract_text" | "ocr" | "embed";

export type JobPriority = "high" | "normal" | "low";

/** One entry in a job's stage timeline. */
export interface StageEntry {
  stage: JobStage;
  status: string;
  at: string;
  duration_ms?: number;
  detail?: Record<string, unknown>;
}

/** Row shape from `GET /jobs` - no result payload, so listings stay small. */
export interface JobSummary {
  id: string;
  file_name: string;
  job_type: string;
  status: JobStatus;
  stage: JobStage;
  priority: JobPriority;
  retry_count: number;
  max_retries: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  next_retry_at: string | null;
}

export interface JobDetail extends JobSummary {
  result: Record<string, unknown> | null;
  error_message: string | null;
  idempotency_key: string | null;
  stages: StageEntry[];
}

export interface JobListResponse {
  items: JobSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface QueueDepth {
  name: string;
  depth: number | null;
  is_priority_queue: boolean;
}

export interface Stats {
  queues: QueueDepth[];
  jobs_by_status: Record<JobStatus, number>;
  active_by_stage: Record<JobStage, number>;
  total_jobs: number;
  total_chunks: number;
  broker_reachable: boolean;
}

export interface UploadResult {
  job_id: string;
  status: JobStatus;
  job_type: string;
  status_url: string;
  deduplicated: boolean;
}

/** Statuses from which nothing further happens - polling can stop. */
export const TERMINAL_STATUSES: JobStatus[] = ["DONE", "FAILED", "DEAD_LETTER"];

export const STAGE_ORDER: JobStage[] = ["extract_text", "ocr", "embed"];
