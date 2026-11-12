import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getJob } from "../api/client";
import { clearActiveJobId, getActiveJobId } from "../lib/storage";
import { isRunning } from "../types";
import type { JobDetail } from "../types";
import { Notice } from "./Card";

/** If the stored job is still running, offer a way back to it. */
export function ActiveJobBanner() {
  const [job, setJob] = useState<JobDetail | null>(null);

  useEffect(() => {
    const jobId = getActiveJobId();
    if (!jobId) return;
    let cancelled = false;
    getJob(jobId)
      .then((found) => {
        if (cancelled) return;
        if (isRunning(found.status)) setJob(found);
        else clearActiveJobId(jobId);
      })
      .catch(() => {
        /* unavailable: just don't show the banner */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!job) return null;

  return (
    <Notice tone="info" title="An extraction is still running">
      <Link
        to={`/jobs/${job.job_id}`}
        className="underline decoration-dotted underline-offset-4 hover:text-white"
      >
        Open “{job.output_name}” ({job.progress.processed}/{job.progress.total} chapters)
      </Link>
    </Notice>
  );
}
