export type JobStatus =
  | "queued"
  | "discovering"
  | "processing"
  | "packaging"
  | "completed"
  | "completed_with_errors"
  | "failed"
  | "cancelling"
  | "cancelled"
  | "interrupted";

export type ChapterStatus =
  | "success"
  | "failed"
  | "partial"
  | "processing"
  | "pending"
  | "skipped";

export interface CreateJobRequest {
  url: string;
  output_name?: string | null;
}

export interface JobAccepted {
  job_id: string;
  status: JobStatus;
  message: string;
}

export interface Progress {
  total: number;
  completed: number;
  failed: number;
  partial: number;
  skipped: number;
  processing: number;
  pending: number;
  processed: number;
  percentage: number;
}

export interface JobDetail {
  job_id: string;
  output_name: string;
  url: string;
  status: JobStatus;
  progress: Progress;
  current_chapter: number | null;
  download_ready: boolean;
  download_url: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobSummary {
  job_id: string;
  output_name: string;
  url: string;
  status: JobStatus;
  created_at: string;
  updated_at: string;
  total_chapters: number;
  completed_chapters: number;
  failed_chapters: number;
  partial_chapters: number;
  download_ready: boolean;
  download_url: string | null;
}

export interface ChapterInfo {
  number: number;
  status: ChapterStatus;
  attempts: number | null;
  error: string | null;
  format: string | null;
  filename: string | null;
  size_bytes: number | null;
  /** Backend-verified: job finished AND this chapter extracted and intact. */
  copyable: boolean;
}

export interface ApiErrorBody {
  error: { code: string; message: string; [extra: string]: unknown };
}

/** States after which the backend will not change the job on its own. */
export const TERMINAL_STATUSES: readonly JobStatus[] = [
  "completed",
  "completed_with_errors",
  "failed",
  "cancelled",
  "interrupted",
];

export const isTerminal = (status: JobStatus): boolean =>
  TERMINAL_STATUSES.includes(status);

export const isRunning = (status: JobStatus): boolean => !isTerminal(status);

export const canCancel = (status: JobStatus): boolean =>
  status === "queued" || status === "discovering" || status === "processing";

export const canResume = (status: JobStatus): boolean =>
  status === "failed" ||
  status === "completed_with_errors" ||
  status === "interrupted" ||
  status === "cancelled";

/** Chapters can only be copied once the whole job has completed. */
export const isFinishedForCopy = (status: JobStatus): boolean =>
  status === "completed" || status === "completed_with_errors";
