import type {
  JobDetail,
  JobListResponse,
  JobPriority,
  Stats,
  UploadResult,
} from "./types";

/**
 * Base URL for the API.
 *
 * These calls run in the browser, so this must be an address the *browser* can
 * reach - not the Docker-internal `http://api:8000`. NEXT_PUBLIC_ values are
 * inlined at build time, which is why the Dockerfile passes it as a build arg.
 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8001";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    // Always hit the network: this dashboard exists to show current state.
    cache: "no-store",
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail ?? body.error ?? detail;
    } catch {
      // Non-JSON error body; the status line is all we have.
    }
    throw new ApiError(String(detail), response.status);
  }

  return (await response.json()) as T;
}

export function fetchStats(): Promise<Stats> {
  return request<Stats>("/stats");
}

export function fetchJobs(limit = 25, status?: string): Promise<JobListResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set("status", status);
  return request<JobListResponse>(`/jobs?${params}`);
}

export function fetchJob(id: string): Promise<JobDetail> {
  return request<JobDetail>(`/jobs/${id}`);
}

export async function uploadDocument(
  file: File,
  priority: JobPriority | "",
): Promise<UploadResult> {
  const body = new FormData();
  body.append("file", file);
  // An empty priority means "let the API decide by size", so omit it entirely
  // rather than sending a blank value the enum would reject.
  if (priority) body.append("priority", priority);

  return request<UploadResult>("/jobs/upload", { method: "POST", body });
}
