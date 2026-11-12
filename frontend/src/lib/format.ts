import type { ChapterStatus, JobStatus } from "../types";

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatBytes(bytes: number | null): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  queued: "Queued",
  discovering: "Finding chapters",
  processing: "Extracting",
  packaging: "Creating ZIP",
  completed: "Completed",
  completed_with_errors: "Completed with errors",
  failed: "Failed",
  cancelling: "Cancelling",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

export const CHAPTER_STATUS_LABEL: Record<ChapterStatus, string> = {
  success: "Extracted",
  failed: "Failed",
  partial: "Partial",
  processing: "Processing",
  pending: "Pending",
  skipped: "Skipped",
};

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export const JOB_STATUS_TONE: Record<JobStatus, Tone> = {
  queued: "neutral",
  discovering: "info",
  processing: "info",
  packaging: "info",
  completed: "success",
  completed_with_errors: "warning",
  failed: "danger",
  cancelling: "warning",
  cancelled: "neutral",
  interrupted: "warning",
};

export const CHAPTER_STATUS_TONE: Record<ChapterStatus, Tone> = {
  success: "success",
  failed: "danger",
  partial: "warning",
  processing: "info",
  pending: "neutral",
  skipped: "neutral",
};

export const TONE_CLASSES: Record<Tone, string> = {
  neutral: "bg-slate-800 text-slate-300 ring-slate-700",
  info: "bg-sky-950 text-sky-300 ring-sky-800",
  success: "bg-emerald-950 text-emerald-300 ring-emerald-800",
  warning: "bg-amber-950 text-amber-300 ring-amber-800",
  danger: "bg-rose-950 text-rose-300 ring-rose-800",
};
