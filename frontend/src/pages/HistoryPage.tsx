import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, listJobs, retryJob } from "../api/client";
import { Card, Notice, buttonClass } from "../components/Card";
import { DownloadButton } from "../components/DownloadButton";
import { JobStatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../lib/format";
import { canResume, isRunning } from "../types";
import type { JobSummary } from "../types";

export function HistoryPage() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resuming, setResuming] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setJobs(await listJobs());
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't load your history.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Keep running jobs fresh.
  const anyRunning = jobs?.some((j) => isRunning(j.status)) ?? false;
  useEffect(() => {
    if (!anyRunning) return;
    const id = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(id);
  }, [anyRunning, load]);

  async function resume(job: JobSummary) {
    setResuming(job.job_id);
    try {
      await retryJob(job.job_id);
      navigate(`/jobs/${job.job_id}`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't resume this job.");
      setResuming(null);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-white">Extraction history</h1>
          <p className="mt-1 text-sm text-slate-400">Extractions from this browser session. They and their files are removed after you close the app.</p>
        </div>
        <button className={buttonClass.secondary} onClick={() => void load()}>Refresh</button>
      </div>

      {error && <Notice tone="danger" title="Something went wrong">{error}</Notice>}

      {jobs === null && !error && <p className="text-slate-400" role="status">Loading…</p>}

      {jobs?.length === 0 && (
        <Card className="text-center">
          <p className="text-slate-300">No extractions yet.</p>
          <Link to="/" className={`${buttonClass.primary} mt-4`}>Start your first extraction</Link>
        </Card>
      )}

      {jobs && jobs.length > 0 && (
        <ul className="space-y-3">
          {jobs.map((job) => (
            <li key={job.job_id}>
              <Card className="!p-4 sm:!p-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <Link
                      to={`/jobs/${job.job_id}`}
                      className="block truncate text-base font-semibold text-white hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
                    >
                      {job.output_name}
                    </Link>
                    <p className="mt-0.5 text-xs text-slate-500">{formatDateTime(job.created_at)}</p>
                  </div>
                  <JobStatusBadge status={job.status} />
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm text-slate-400">
                  <span>
                    <span className="font-medium tabular-nums text-slate-200">{job.completed_chapters}</span> /{" "}
                    <span className="tabular-nums">{job.total_chapters}</span> chapters extracted
                  </span>
                  {job.failed_chapters + job.partial_chapters > 0 && (
                    <span className="text-amber-300">
                      {job.failed_chapters + job.partial_chapters} failed or partial
                    </span>
                  )}
                  <span className={job.download_ready ? "text-emerald-300" : "text-slate-500"}>
                    {job.download_ready ? "ZIP ready" : "No ZIP yet"}
                  </span>
                </div>

                <div className="mt-4 flex flex-wrap items-center gap-3">
                  <Link to={`/jobs/${job.job_id}`} className={buttonClass.secondary}>Open</Link>
                  {job.download_ready && (
                    <DownloadButton jobId={job.job_id} label="Download ZIP" className={buttonClass.success} />
                  )}
                  {canResume(job.status) && (
                    <button
                      className={buttonClass.primary}
                      disabled={resuming === job.job_id}
                      onClick={() => void resume(job)}
                    >
                      {resuming === job.job_id ? "Resuming…" : "Resume"}
                    </button>
                  )}
                </div>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
