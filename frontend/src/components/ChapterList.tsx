import { useMemo, useState } from "react";
import { formatBytes } from "../lib/format";
import { isTerminal } from "../types";
import type { ChapterInfo, ChapterStatus, JobStatus } from "../types";
import { CopyButton } from "./CopyButton";
import { ChapterStatusBadge } from "./StatusBadge";

type Filter = "all" | "problems" | "success" | "active";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "problems", label: "Failed / partial" },
  { id: "success", label: "Extracted" },
  { id: "active", label: "Pending / processing" },
];

const MATCH: Record<Filter, (s: ChapterStatus) => boolean> = {
  all: () => true,
  problems: (s) => s === "failed" || s === "partial",
  success: (s) => s === "success" || s === "skipped",
  active: (s) => s === "pending" || s === "processing",
};

const PAGE = 100;

/** Why a chapter's Copy button is disabled (shown as a tooltip). */
function disabledReason(chapter: ChapterInfo): string {
  if (chapter.status === "pending" || chapter.status === "processing") {
    return "Available as soon as this chapter has been extracted";
  }
  if (chapter.status !== "success") return "This chapter wasn't extracted";
  return "This chapter can't be copied right now";
}

/** "Unavailable": the chapter failed, or the job is over and it never succeeded. */
function isUnavailable(chapter: ChapterInfo, jobStatus: JobStatus): boolean {
  if (chapter.status === "failed" || chapter.status === "partial") return true;
  return isTerminal(jobStatus) && chapter.status !== "success";
}

export function ChapterList({
  jobId,
  jobStatus,
  chapters,
}: {
  jobId: string;
  jobStatus: JobStatus;
  chapters: ChapterInfo[];
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [visible, setVisible] = useState(PAGE);

  const counts = useMemo(() => {
    const result = {} as Record<Filter, number>;
    for (const f of FILTERS) {
      result[f.id] = chapters.filter((c) => MATCH[f.id](c.status)).length;
    }
    return result;
  }, [chapters]);

  const shown = useMemo(
    () => chapters.filter((c) => MATCH[filter](c.status)),
    [chapters, filter],
  );

  if (chapters.length === 0) {
    return (
      <p className="text-sm text-slate-400">
        Chapters will appear here once the chapter list has been loaded.
      </p>
    );
  }

  return (
    <div>
      <div role="tablist" aria-label="Filter chapters" className="mb-4 flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            role="tab"
            aria-selected={filter === f.id}
            onClick={() => {
              setFilter(f.id);
              setVisible(PAGE);
            }}
            className={`rounded-lg px-3 py-1.5 text-xs font-medium ring-1 ring-inset transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 ${
              filter === f.id
                ? "bg-indigo-500/20 text-indigo-200 ring-indigo-500/60"
                : "bg-slate-800 text-slate-300 ring-slate-700 hover:bg-slate-700"
            }`}
          >
            {f.label} <span className="tabular-nums opacity-70">({counts[f.id]})</span>
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        <p className="text-sm text-slate-400">No chapters in this view.</p>
      ) : (
        <ul className="divide-y divide-slate-800 overflow-hidden rounded-xl border border-slate-800">
          {shown.slice(0, visible).map((chapter) => (
            <li key={chapter.number} className="bg-slate-900/40 px-4 py-2.5">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                <span className="w-24 shrink-0 font-medium tabular-nums text-slate-200">
                  Chapter {chapter.number}
                </span>
                <ChapterStatusBadge status={chapter.status} />
                <span className="text-xs text-slate-500">
                  {chapter.format ?? ""}
                  {chapter.size_bytes ? ` · ${formatBytes(chapter.size_bytes)}` : ""}
                </span>
                {chapter.attempts ? (
                  <span className="text-xs text-slate-500">
                    {chapter.attempts} attempt{chapter.attempts === 1 ? "" : "s"}
                  </span>
                ) : null}
                <CopyButton
                  jobId={jobId}
                  chapterNumber={chapter.number}
                  enabled={chapter.copyable}
                  unavailable={isUnavailable(chapter, jobStatus)}
                  disabledReason={disabledReason(chapter)}
                />
              </div>
              {chapter.error && (
                <details className="mt-1.5 text-xs text-slate-400">
                  <summary className="cursor-pointer text-rose-300/90 hover:text-rose-200">
                    Why did this fail?
                  </summary>
                  <p className="mt-1 break-words rounded-lg bg-slate-950 p-2 font-mono text-[11px] leading-relaxed">
                    {chapter.error}
                  </p>
                </details>
              )}
            </li>
          ))}
        </ul>
      )}

      {shown.length > visible && (
        <button
          onClick={() => setVisible((v) => v + PAGE)}
          className="mt-3 rounded-lg px-3 py-1.5 text-sm text-indigo-300 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
        >
          Show more ({shown.length - visible} remaining)
        </button>
      )}
    </div>
  );
}
