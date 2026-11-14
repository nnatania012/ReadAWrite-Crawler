import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, cancelJob, retryJob } from "../api/client";
import { Card, Notice, buttonClass } from "../components/Card";
import { ChapterList } from "../components/ChapterList";
import { DownloadButton } from "../components/DownloadButton";
import { ProgressBar } from "../components/ProgressBar";
import { JobStatusBadge } from "../components/StatusBadge";
import { useAutoDownload } from "../hooks/useAutoDownload";
import { useJobPolling } from "../hooks/useJobPolling";
import { formatDateTime } from "../lib/format";
import { clearActiveJobId } from "../lib/storage";
import { canCancel, canResume, isTerminal } from "../types";
import type { JobDetail } from "../types";

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/60 px-4 py-3">
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className={`mt-1 text-2xl font-semibold tabular-nums ${tone ?? "text-white"}`}>{value}</dd>
    </div>
  );
}

function StatusMessage({ job }: { job: JobDetail }) {
  const p = job.progress;
  const problems = p.failed + p.partial;

  switch (job.status) {
    case "queued":
      return <Notice tone="info" title="Queued">Waiting for earlier extractions to finish.</Notice>;
    case "discovering":
      return <Notice tone="info" title="Finding chapters">Opening the story page and collecting every chapter link…</Notice>;
    case "packaging":
      return <Notice tone="info" title="Creating your ZIP">All chapters are processed. Packaging the files…</Notice>;
    case "cancelling":
      return <Notice tone="warning" title="Cancelling">The chapter being processed will finish first. Chapters already extracted are kept.</Notice>;
    case "completed":
      return (
        <Notice tone="success" title="Extraction complete">
          All {p.completed} chapters were extracted successfully.
        </Notice>
      );
    case "completed_with_errors":
      return (
        <Notice tone="warning" title="Completed with errors">
          {p.completed} chapter{p.completed === 1 ? "" : "s"} extracted, {problems} failed or partial. The ZIP
          contains the extracted chapters and an extraction report. Use Resume to retry the others.
        </Notice>
      );
    case "failed":
      return (
        <Notice tone="danger" title="Extraction failed">
          {job.error ?? "The extraction could not produce usable output."}
        </Notice>
      );
    case "cancelled":
      return (
        <Notice tone="warning" title="Cancelled">
          {p.completed} chapter{p.completed === 1 ? " was" : "s were"} extracted before cancelling. Resume to continue
          where it stopped.
        </Notice>
      );
    case "interrupted":
      return (
        <Notice tone="warning" title="Interrupted">
          {job.error ?? "The extraction stopped before finishing."} Resume to continue where it left off.
        </Notice>
      );
    default:
      return null;
  }
}

export function JobPage() {
  const { jobId = "" } = useParams();
  const { job, chapters, loading, notFound, error, connectionIssue, refresh } = useJobPolling(jobId);
  const [busy, setBusy] = useState<"cancel" | "retry" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [downloadWarning, setDownloadWarning] = useState<string | null>(null);

  useAutoDownload(job, setDownloadWarning);

  useEffect(() => {
    if (job && isTerminal(job.status)) clearActiveJobId(job.job_id);
  }, [job]);

  async function act(kind: "cancel" | "retry") {
    if (kind === "cancel" && !window.confirm("Cancel this extraction? Finished chapters are kept.")) return;
    setBusy(kind);
    setActionError(null);
    setDownloadWarning(null);
    try {
      await (kind === "cancel" ? cancelJob(jobId) : retryJob(jobId));
      refresh();
    } catch (caught) {
      setActionError(caught instanceof ApiError ? caught.message : "That didn't work. Please try again.");
    } finally {
      setBusy(null);
    }
  }

  if (loading && !job) {
    return <p className="text-slate-400" role="status">Loading extraction…</p>;
  }

  if (notFound) {
    return (
      <Card className="mx-auto max-w-xl text-center">
        <h1 className="text-xl font-semibold text-white">Extraction not found</h1>
        <p className="mt-2 text-slate-400">This job doesn't exist or is no longer available.</p>
        <div className="mt-5 flex justify-center gap-3">
          <Link to="/" className={buttonClass.primary}>Start a new extraction</Link>
          <Link to="/jobs" className={buttonClass.secondary}>View history</Link>
        </div>
      </Card>
    );
  }

  if (!job) {
    return (
      <Notice tone="danger" title="Couldn't load this extraction">
        {error?.message ?? "Please check your connection and reload the page."}
      </Notice>
    );
  }

  const p = job.progress;
  const remaining = p.pending + p.processing;

  return (
    <div className="space-y-5">
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="truncate text-2xl font-semibold tracking-tight text-white">{job.output_name}</h1>
            <p className="mt-1 break-all text-sm text-slate-400">{job.url}</p>
          </div>
          <JobStatusBadge status={job.status} />
        </div>
        <dl className="mt-4 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-slate-500 sm:grid-cols-2">
          <div>Started: <span className="text-slate-300">{formatDateTime(job.created_at)}</span></div>
          <div>Job ID: <span className="font-mono text-slate-300">{job.job_id}</span></div>
        </dl>
      </Card>

      {connectionIssue && (
        <Notice tone="warning" title="Connection problem">
          Can't reach the server right now. Your extraction is not affected — retrying automatically.
        </Notice>
      )}

      <StatusMessage job={job} />

      {downloadWarning && <Notice tone="warning" title="Download">{downloadWarning}</Notice>}
      {actionError && <Notice tone="danger" title="Action failed">{actionError}</Notice>}

      <Card>
        <ProgressBar progress={p} />
        <dl className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Stat label="Total" value={p.total} />
          <Stat label="Extracted" value={p.completed} tone="text-emerald-300" />
          <Stat label="Failed" value={p.failed} tone={p.failed ? "text-rose-300" : undefined} />
          <Stat label="Partial" value={p.partial} tone={p.partial ? "text-amber-300" : undefined} />
          <Stat label="Remaining" value={remaining} />
        </dl>
        {job.current_chapter != null && !isTerminal(job.status) && (
          <p className="mt-4 text-sm text-sky-300" aria-live="polite">
            Now processing chapter {job.current_chapter}
          </p>
        )}

        <div className="mt-5 flex flex-wrap items-center gap-3">
          {job.download_ready && <DownloadButton jobId={job.job_id} />}
          {canResume(job.status) && (
            <button className={buttonClass.primary} disabled={busy !== null} onClick={() => act("retry")}>
              {busy === "retry" ? "Resuming…" : "Resume / retry failed chapters"}
            </button>
          )}
          {canCancel(job.status) && (
            <button className={buttonClass.danger} disabled={busy !== null} onClick={() => act("cancel")}>
              {busy === "cancel" ? "Cancelling…" : "Cancel extraction"}
            </button>
          )}
          <Link to="/jobs" className="ml-auto text-sm text-slate-400 hover:text-white">History →</Link>
        </div>
        {job.download_ready && (
          <p className="mt-3 text-xs text-slate-500">
            {p.failed + p.partial > 0
              ? "The ZIP contains the extracted chapters and an extraction report."
              : "The ZIP contains one numbered text file per chapter."}
          </p>
        )}
      </Card>

      <Card>
        <h2 className="mb-4 text-lg font-semibold text-white">Chapter activity</h2>
        <ChapterList jobId={job.job_id} jobStatus={job.status} chapters={chapters} />
      </Card>
    </div>
  );
}
