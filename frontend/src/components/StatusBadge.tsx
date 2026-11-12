import {
  CHAPTER_STATUS_LABEL,
  CHAPTER_STATUS_TONE,
  JOB_STATUS_LABEL,
  JOB_STATUS_TONE,
  TONE_CLASSES,
} from "../lib/format";
import type { ChapterStatus, JobStatus } from "../types";

const base =
  "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset";

export function JobStatusBadge({ status }: { status: JobStatus }) {
  const live =
    status === "discovering" || status === "processing" || status === "packaging";
  return (
    <span className={`${base} ${TONE_CLASSES[JOB_STATUS_TONE[status]]}`}>
      {live && (
        <span
          aria-hidden
          className="h-1.5 w-1.5 animate-pulse rounded-full bg-current"
        />
      )}
      {JOB_STATUS_LABEL[status]}
    </span>
  );
}

const CHAPTER_ICON: Record<ChapterStatus, string> = {
  success: "✓",
  failed: "✕",
  partial: "◐",
  processing: "●",
  pending: "○",
  skipped: "↷",
};

export function ChapterStatusBadge({ status }: { status: ChapterStatus }) {
  return (
    <span className={`${base} ${TONE_CLASSES[CHAPTER_STATUS_TONE[status]]}`}>
      <span aria-hidden>{CHAPTER_ICON[status]}</span>
      {CHAPTER_STATUS_LABEL[status]}
    </span>
  );
}
